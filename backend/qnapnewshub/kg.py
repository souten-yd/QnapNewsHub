"""知識グラフの土台（v0.5）: 文書・根拠・関係・機密の区分の引き継ぎ・履歴と矛盾・一緒に出る集計。ARCHITECTURE 3・6 章。

統合ナレッジ基盤の正本の形を、ニュース専用にせず汎用に作る（ControlDeck の PLAN-integrated-knowledge 4 章）。ニュース以外の種類（paper・repo・model・research・note・patent・standard・other）は
表と API だけ。取り込み（論文・GitHub・特許の収集）は統合の段。今あるものを使い回し、同じものを書き直さない: 物は entities、主張は claims、記事は news_items、イベントは events。

- **文書（documents）**: 種類・正規化した識別子（`normalize_ident`）・機密の区分・AI_USE・出どころ・日時・版。ニュースの記事は `news_documents`（対応の表）で結ぶ。`sync_documents` は冪等。
- **機密の区分の引き継ぎ**（`inherit`）: 主張・関係・物・カードの区分は、根拠の文書のうち最も厳しいもの（confidential は public より、prohibited > local_only > allowed）。
  文書の既定は fail-closed（confidential・local_only）。ニュースの記事だけが public・allowed。読む API の既定は public だけで、confidential は管理者（ログイン）だけが読める（kg_router）。
- **関係（relationships）**: 型は REL_TYPES。LLM が作るもの（extract.REL_TYPES）は引用が原文にあれば `extracted`（確かさ 0.7）、無ければ `inferred`（推測。確かさは 0.35 を超えない）。
  一緒に出る（co_occurs）は集計（`rebuild_cooccur`）。人が根拠つきで入れたものだけ `confirmed`。確定を自動では作らない。
- **履歴と矛盾**（`history`）: 同じ物・同じ種類の主張を時系列に並べ、値が変わったら「変化」、同時期（CONTRADICT_DAYS 日以内）に別の文書で値が食い違えば「矛盾」と印を付ける。上書きも削除もしない（読むときに計算する）。
- 書き込みは Write Coordinator のまとめ書きの中（entities.apply_extractions）か writer.run の 1 トランザクション。ここの関数はコミットしない（呼ぶ側がコミットする）。
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from qnapnewshub import extract
from qnapnewshub.db import Claim, Document, Entity, Evidence, NewsDocument, NewsItem, NewsMeta, Relationship

_UTC = timezone.utc
DOC_KINDS = ("news", "paper", "repo", "model", "research", "note", "patent", "standard", "other")
SENSITIVITY = ("public", "confidential")
AI_USE = ("allowed", "local_only", "prohibited")
REL_TYPES = ("uses", "supports", "replaces", "competes", "part_of", "implements", "cites", "contradicts", "made_by", "develops", "co_occurs", "other")
SYMMETRIC = frozenset({"co_occurs", "competes", "contradicts"})      # 向きの無い型。subject_id < object_id にそろえる
STATUSES = ("extracted", "inferred", "statistical", "confirmed")
COOC_MIN_COUNT = 3
COOC_MIN_RATIO = 2.0
COOC_DAYS = 180
COOC_MAX_ENTITIES_PER_UNIT = 12
CONF_EXTRACTED, CONF_INFERRED, INFERRED_CAP, CONF_CONFIRMED = 0.7, 0.15, 0.35, 0.9
# 根拠の段階ごとの確かさ（一致＞言い換え＞無し）。関係・主張の根拠の confidence に入り、検索の辺の重み・主張の根拠の並び・カードの並びに効く
TIER_CONF = {"exact": 0.7, "paraphrase": 0.3, "none": 0.15}
TIER_RANK = {"exact": 2, "paraphrase": 1, "none": 0, "": 0}


def tier_of(grounded: bool, tier: str = "") -> str:
    """根拠の段階。tier が空の古い行は grounded（原文に照合できた）なら exact、そうでなければ none。"""
    return "exact" if grounded else (tier if tier in TIER_CONF else "none")
CONTRADICT_DAYS = 3
MAX_HISTORY_CLAIMS = 300
_DOC_HI = "kg_doc_hi"


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (value.astimezone(_UTC) if value.tzinfo else value).replace(tzinfo=None)


def iso(value: datetime | None) -> str:
    v = _naive(value)
    return v.strftime("%Y-%m-%dT%H:%M:%SZ") if v else ""


def _now() -> datetime:
    return datetime.now(_UTC).replace(tzinfo=None)


def _sql_dt(value: datetime) -> str:
    """text() の束縛に datetime をそのまま渡さない（Python 3.12 の sqlite3 の既定の変換は非推奨）。SQLAlchemy の DateTime 列と同じ書式の文字列で渡す（動作は同じ）。"""
    return _naive(value).strftime("%Y-%m-%d %H:%M:%S.%f")


# ---- 機密の区分 ----

def inherit(pairs) -> tuple[str, str]:
    """根拠の文書の（sensitivity, ai_use）の並びから、派生データの区分を決める（最も厳しいもの）。1 つも無ければ fail-closed（confidential・local_only）。"""
    sens, use = -1, -1
    for s, u in pairs:
        sens = max(sens, SENSITIVITY.index(s) if s in SENSITIVITY else 1)
        use = max(use, AI_USE.index(u) if u in AI_USE else 1)
    if sens < 0:
        return "confidential", "local_only"
    if SENSITIVITY[sens] == "confidential":
        use = max(use, AI_USE.index("local_only"))      # 機密の物は、少なくともローカル LLM だけ
    return SENSITIVITY[sens], AI_USE[use]


def scope_list(include_confidential: bool) -> list[str]:
    return ["public", "confidential"] if include_confidential else ["public"]


# ---- 識別子の正規化 ----

_DOI = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.I)
_ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:\s*)((?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[a-z]{2})?/\d{7}))(v\d+)?", re.I)
_GH = re.compile(r"github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?:/|$)", re.I)
_SHA = re.compile(r"\b([0-9a-f]{7,40})\b", re.I)
_PATENT = re.compile(r"^(?:patent:)?\s*([A-Za-z]{2})[\s\-]*([0-9][0-9\s\-/,.]*[0-9])\s*([A-Za-z]\d?)?$")
_STD = re.compile(r"^(?:standard:)?\s*(ISO/IEC|IEC|ISO|JIS|ASTM|IEEE|EN|DIN|BS|ANSI)\s*([A-Z]?\s*[\d][\d.\-/]*)\s*(?::\s*(\d{4}))?", re.I)


def normalize_ident(raw: str, *, sha: str = "") -> str:
    """文書の識別子を 1 つの形にそろえる。URL は url:（scheme・host の小文字・追跡の引数と # を除く）、DOI は doi:（小文字）、arXiv は arxiv:（版の v は除く）、
    GitHub のリポジトリ＋SHA は repo:github.com/owner/name@sha（sha を渡すか、"owner/name@sha"）、特許は patent:（国コード＋番号。空白・ハイフンを除く）、規格は standard:（団体＋番号＋年）。
    判別できない文字列は ValueError。"""
    value = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not value:
        raise ValueError("識別子が空です")
    low = value.lower()
    if low.startswith("repo:"):
        value, low = value[5:], low[5:]
    m = _ARXIV.search(value)
    if m:
        return f"arxiv:{m.group(1).lower()}"
    m = _DOI.search(value)
    if m and ("doi" in low or low.startswith("10.")):
        return "doi:" + m.group(1).rstrip(".,;)").lower()
    m = _GH.search(value) or re.match(r"^([\w.\-]+)/([\w.\-]+?)(?:@|$)", value)
    if m and (sha or "@" in value or "github.com" in low):
        s = sha or (value.split("@", 1)[1] if "@" in value else "")
        s = s.strip().lower()
        if not _SHA.fullmatch(s):
            raise ValueError("リポジトリの識別子には SHA（7〜40 桁の 16 進）が要ります")
        return f"repo:github.com/{m.group(1).lower()}/{m.group(2).lower()}@{s}"
    m = _STD.match(value)
    if m:
        body = re.sub(r"\s+", "", m.group(2)).upper()
        return f"standard:{m.group(1).upper()} {body}" + (f":{m.group(3)}" if m.group(3) else "")
    m = _PATENT.match(value)
    if m:
        return f"patent:{m.group(1).upper()}{re.sub(r'[^0-9]', '', m.group(2))}{(m.group(3) or '').upper()}"
    if re.match(r"^https?://", value, re.I):
        return "url:" + _canon_url(value)
    raise ValueError("識別子の形を判別できません（URL・DOI・arXiv ID・リポジトリ＋SHA・特許番号・規格番号）")


def _canon_url(url: str) -> str:
    from qnapnewshub.dedup import normalize_url

    return normalize_url(url) or urlsplit(url)._replace(fragment="").geturl()


# ---- 文書 ----

def part_ids(ids: list[int]) -> list[int]:
    return [int(x) for x in ids[:2000]]


def sync_documents(db: Session, ids: list[int] | None = None, *, full: bool = False) -> int:
    """ニュースの記事を documents へ対応づける（冪等。同じ識別子の記事は同じ文書を指す）。ids を渡せばその記事だけ、
    渡さなければ前回の続き（news_meta の kg_doc_hi より大きい id）、full なら全部（抜けの点検）。新しく対応づけた記事の数を返す。コミットは呼ぶ側。"""
    now = _now()
    ident = "'url:' || CASE WHEN n.canon_url <> '' THEN n.canon_url ELSE n.url END"
    ins_doc = (f"INSERT OR IGNORE INTO documents (kind, ident, title, url, source, published_at, version, sensitivity, ai_use, created_at) "
               f"SELECT 'news', {ident}, n.title, n.url, n.source, n.published_at, '', 'public', 'allowed', :now FROM news_items n WHERE n.kind <> 'event' AND {{cond}} ORDER BY n.id")
    ins_map = (f"INSERT OR IGNORE INTO news_documents (news_item_id, document_id) SELECT n.id, d.id FROM news_items n JOIN documents d ON d.ident = {ident} "
               f"WHERE n.kind <> 'event' AND {{cond}}")
    done = 0
    if ids is not None:
        for i in range(0, len(ids), 400):
            part = [int(x) for x in ids[i:i + 400]]
            cond = f"n.id IN ({','.join(str(x) for x in part)})"
            db.execute(text(ins_doc.format(cond=cond)), {"now": _sql_dt(now)})
            done += db.execute(text(ins_map.format(cond=cond))).rowcount or 0
        if done:      # 新しく対応づけた記事の文書を、いまの状態の段（L1〜L3）へ
            from qnapnewshub import docstore

            docstore.news_tiers(db, part_ids(ids))
        return done
    hi_row = db.get(NewsMeta, _DOC_HI)
    hi = 0 if full or hi_row is None else int(hi_row.value or 0)
    top = int(db.scalar(select(func.max(NewsItem.id))) or 0)
    if top <= hi and not full:
        return 0
    cond = f"n.id > {hi}"
    db.execute(text(ins_doc.format(cond=cond)), {"now": _sql_dt(now)})
    done = db.execute(text(ins_map.format(cond=cond))).rowcount or 0
    if hi_row is None:
        db.add(NewsMeta(key=_DOC_HI, value=str(top)))
    else:
        hi_row.value = str(top)
    if done or full:      # 新しい文書（と、前回までの段の取りこぼし）を、いまの状態の段へ（冪等）
        from qnapnewshub import docstore

        docstore.news_tiers(db)
    return done


def docs_of_articles(db: Session, article_ids: list[int]) -> dict[int, tuple[int, str, str]]:
    """{記事の id: (文書の id, sensitivity, ai_use)}。対応が無い記事は含めない。"""
    out: dict[int, tuple[int, str, str]] = {}
    for i in range(0, len(article_ids), 400):
        for aid, did, sens, use in db.execute(select(NewsDocument.news_item_id, Document.id, Document.sensitivity, Document.ai_use)
                                              .join(Document, Document.id == NewsDocument.document_id).where(NewsDocument.news_item_id.in_(article_ids[i:i + 400]))):
            out[aid] = (did, sens, use)
    return out


def upsert_document(db: Session, *, kind: str, ident: str, title: str = "", url: str = "", source: str = "", published_at: datetime | None = None,
                    version: str = "", sensitivity: str = "confidential", ai_use: str = "local_only") -> Document:
    """文書を識別子で 1 つにして入れる（API 用）。すでにあれば題などを補い、区分は**厳しい方**へだけ動かす（緩めない）。"""
    if kind not in DOC_KINDS or sensitivity not in SENSITIVITY or ai_use not in AI_USE:
        raise ValueError("種類・機密の区分・AI_USE が正しくありません")
    ident = normalize_ident(ident)
    doc = db.scalar(select(Document).where(Document.ident == ident))
    if doc is None:
        sens, use = inherit([(sensitivity, ai_use)])
        doc = Document(kind=kind, ident=ident, title=title[:400], url=url[:1000], source=source[:120], published_at=published_at, version=version[:64],
                       sensitivity=sens, ai_use=use)
        db.add(doc)
    else:
        doc.title = doc.title or title[:400]
        doc.url = doc.url or url[:1000]
        doc.source = doc.source or source[:120]
        doc.published_at = doc.published_at or published_at
        doc.version = version[:64] or doc.version
        doc.sensitivity, doc.ai_use = inherit([(doc.sensitivity, doc.ai_use), (sensitivity, ai_use)])
    db.flush()
    return doc


def doc_dict(d: Document) -> dict:
    return {"id": d.id, "kind": d.kind, "ident": d.ident, "title": d.title, "url": d.url, "source": d.source, "published_at": iso(d.published_at), "version": d.version,
            "sensitivity": d.sensitivity, "ai_use": d.ai_use}


# ---- 根拠・主張・関係を入れる ----

def evidence_row(doc_id: int, *, claim_id: int | None = None, relationship_id: int | None = None, quote: str = "", start: int | None = None, end: int | None = None,
                 page: int | None = None, grounded: bool = False, confidence: float = 0.0, tier: str = "") -> Evidence:
    quote = quote.strip()[:300]
    return Evidence(document_id=doc_id, claim_id=claim_id, relationship_id=relationship_id, start=start, end=end, page=page, quote=quote,
                    quote_hash=hashlib.sha256(quote.encode()).hexdigest() if quote else "", grounded=grounded, confidence=confidence, tier=tier)


def entity_for(db: Session, name: str, kind: str, when: datetime, sens: str, use: str) -> Entity | None:
    """名前から物を探し、無ければ作る（entities と同じ正規化のキー）。物の区分は、公開の文書に出たことがあれば public のまま（緩めない・厳しくもしない）。"""
    from qnapnewshub.entities import _get_or_create

    ent = _get_or_create(db, name, kind, when, {})
    if ent is None:
        return None
    if ent.article_count == 0 and not db.scalar(select(Claim.id).where(Claim.entity_id == ent.id, Claim.sensitivity == "public").limit(1)) and sens == "confidential":
        ent.sensitivity, ent.ai_use = inherit([(sens, use)])      # 機密の文書だけから作った物は機密（公開の記事に出ていれば public のまま）
    return ent


def add_claim(db: Session, *, document: Document, entity: Entity | None, kind: str, text_: str, value: str = "", evidences: list[dict] | None = None,
              model: str = "", version: str = "", ai_extracted: bool = False) -> Claim:
    """ニュース以外の文書の主張（根拠は複数）。区分は文書から引き継ぐ。evidences は [{quote, start, end, page}]。"""
    sens, use = inherit([(document.sensitivity, document.ai_use)])
    claim = Claim(article_id=None, document_id=document.id, entity_id=entity.id if entity else None, kind=kind if kind in extract.FACT_KINDS else "other", text=text_[:2000],
                  value=value[:120], model=model[:200], prompt_version=version[:16], ai_extracted=ai_extracted, sensitivity=sens, ai_use=use,
                  created_at=datetime.now(_UTC))
    db.add(claim)
    db.flush()
    for ev in evidences or [{}]:
        db.add(evidence_row(document.id, claim_id=claim.id, quote=str(ev.get("quote", "")), start=ev.get("start"), end=ev.get("end"), page=ev.get("page"),
                            grounded=bool(ev.get("quote")), confidence=CONF_CONFIRMED if ev.get("quote") else 0.0))
    return claim


def pair_ids(a: int, b: int, rel_type: str) -> tuple[int, int, bool]:
    directed = rel_type not in SYMMETRIC
    return (a, b, directed) if directed or a < b else (b, a, directed)


def get_relationship(db: Session, subject_id: int, object_id: int, rel_type: str) -> Relationship:
    s, o, directed = pair_ids(subject_id, object_id, rel_type)
    rel = db.scalar(select(Relationship).where(Relationship.subject_id == s, Relationship.object_id == o, Relationship.type == rel_type))
    if rel is None:
        rel = Relationship(subject_id=s, object_id=o, type=rel_type, directed=directed, status="inferred", confidence=0.0, sensitivity="confidential", ai_use="local_only")
        db.add(rel)
        db.flush()
    return rel


def add_relationship(db: Session, *, subject: Entity, object_: Entity, rel_type: str, document: Document, quote: str = "", start: int | None = None, end: int | None = None,
                     page: int | None = None, grounded: bool = False, model: str = "", version: str = "", by: str = "", tier: str = "") -> Relationship:
    """関係に根拠を 1 つ足す（同じ関係には根拠が複数付く）。確かさ・状態・区分は recompute_relationships が根拠から決める。"""
    if rel_type not in REL_TYPES or rel_type == "co_occurs":
        raise ValueError("関係の型が正しくありません（一緒に出る co_occurs は集計だけが作ります）")
    if subject.id == object_.id:
        raise ValueError("主語と目的語が同じです")
    rel = get_relationship(db, subject.id, object_.id, rel_type)
    t = tier_of(grounded, tier) if not by else "exact"
    conf = CONF_CONFIRMED if by and quote else TIER_CONF[t]
    db.add(evidence_row(document.id, relationship_id=rel.id, quote=quote if (grounded or by) else "", start=start, end=end, page=page, grounded=grounded or bool(by and quote),
                        confidence=conf, tier=t))
    rel.activity_model, rel.activity_version = model[:200] or rel.activity_model, version[:16] or rel.activity_version
    if by:
        rel.activity_by = by[:80]
    db.flush()
    return rel


def recompute_relationships(db: Session, rel_ids) -> None:
    """型つきの関係（co_occurs 以外）の状態・確かさ・根拠の数・日時・機密の区分を、いまの根拠から数え直す。根拠が 0 になった関係は消す。"""
    ids = [i for i in dict.fromkeys(rel_ids) if i]
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        rows: dict[int, list] = {}
        for rid, doc_id, grounded, conf, sens, use, pub in db.execute(
                select(Evidence.relationship_id, Evidence.document_id, Evidence.grounded, Evidence.confidence, Document.sensitivity, Document.ai_use, Document.published_at)
                .join(Document, Document.id == Evidence.document_id).where(Evidence.relationship_id.in_(part))):
            rows.setdefault(rid, []).append((doc_id, grounded, conf, sens, use, pub))
        for rel in db.scalars(select(Relationship).where(Relationship.id.in_(part))):
            evs = rows.get(rel.id, [])
            if rel.type == "co_occurs":
                continue
            if not evs:
                db.delete(rel)
                continue
            docs = {e[0] for e in evs}
            best = max(e[2] for e in evs)
            conf = min(CONF_CONFIRMED, best + 0.1 * (len(docs) - 1))
            if rel.activity_by and any(e[1] for e in evs):
                rel.status = "confirmed"
            elif any(e[1] for e in evs):
                rel.status = "extracted"
            else:
                rel.status = "inferred"
                conf = min(conf, INFERRED_CAP)
            rel.confidence = round(conf, 3)
            rel.evidence_count = len(evs)
            rel.sensitivity, rel.ai_use = inherit((e[3], e[4]) for e in evs)
            times = [_naive(e[5]) for e in evs if e[5] is not None]
            rel.first_at, rel.last_at = (min(times), max(times)) if times else (rel.first_at, rel.last_at)


def apply_article_relations(db: Session, work: list[tuple], docs: dict[int, tuple[int, str, str]], touched_old: set[int]) -> None:
    """記事から取り出した型つきの関係（entities.apply_extractions が集めた (記事 id, 関係, 主語の物, 目的語の物, モデル, 版)）を入れる。
    再取り出しの前の根拠は呼ぶ側が消してあり、その関係の id が touched_old。区分は記事の文書から引き継ぐ。"""
    touched = set(touched_old)
    for article_id, rel, subj, obj, model, version in work:
        doc = docs.get(article_id)
        if doc is None or rel["type"] not in REL_TYPES or rel["type"] == "co_occurs":
            continue
        document = db.get(Document, doc[0])
        r = add_relationship(db, subject=subj, object_=obj, rel_type=rel["type"], document=document, quote=rel.get("quote", ""), grounded=bool(rel.get("grounded")),
                             model=model, version=version, tier=str(rel.get("tier") or ""))
        touched.add(r.id)
    recompute_relationships(db, touched)


def drop_article_evidence(db: Session, document_ids: list[int]) -> set[int]:
    """文書の、AI の取り出しによる関係の根拠を消す（再取り出しの前）。影響した関係の id を返す。人が入れた根拠（関係の activity_by がある）は消さない。"""
    out: set[int] = set()
    for i in range(0, len(document_ids), 400):
        part = document_ids[i:i + 400]
        rows = db.execute(select(Evidence.id, Evidence.relationship_id).join(Relationship, Relationship.id == Evidence.relationship_id)
                          .where(Evidence.document_id.in_(part), Relationship.activity_by == "")).all()
        out.update(r[1] for r in rows)
        db.execute(delete(Evidence).where(Evidence.id.in_([r[0] for r in rows])))
    return out


# ---- 一緒に出る（co_occurs）の集計 ----

def rebuild_cooccur(db: Session, now: datetime | None = None, *, days: int = COOC_DAYS) -> dict:
    """物の組が同じ「単位」に出た回数を数え、偶然より多い組（期待値の比 COOC_MIN_RATIO 以上・回数 COOC_MIN_COUNT 以上）だけを co_occurs の関係に入れ直す。
    単位 = 同じイベントの記事は 1 つ（イベントに入っていない記事は 1 記事で 1 つ。重複の記事は数えない）。期待値 = 物 a の単位の数 × 物 b の単位の数 ÷ 全体の単位の数。
    集計だけなので LLM は使わない。コミットは呼ぶ側。{"units", "pairs", "kept"} を返す。"""
    since = (now or _now()) - timedelta(days=days)
    units: dict[tuple, list] = {}      # 単位 -> [物の集合, 最初の日時, 最後の日時]
    rows = db.execute(text("SELECT ae.article_id, ae.entity_id, n.published_at, ea.event_id FROM article_entities ae JOIN news_items n ON n.id = ae.article_id "
                           "LEFT JOIN event_articles ea ON ea.article_id = ae.article_id WHERE n.published_at >= :since AND n.dup_of IS NULL AND n.kind <> 'event'"), {"since": _sql_dt(since)})
    for aid, eid, published, event_id in rows:
        key = ("e", event_id) if event_id else ("a", aid)
        when = published if isinstance(published, datetime) else datetime.fromisoformat(str(published))
        u = units.get(key)
        if u is None:
            units[key] = [{eid}, when, when]
        else:
            u[0].add(eid)
            u[1], u[2] = min(u[1], when), max(u[2], when)
    total = len(units)
    df: Counter = Counter()
    for u in units.values():
        df.update(u[0])
    frequent = {e for e, n in df.items() if n >= COOC_MIN_COUNT}
    pairs: Counter = Counter()
    for u in units.values():
        ents = sorted(u[0] & frequent)[:COOC_MAX_ENTITIES_PER_UNIT]
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                pairs[(ents[i], ents[j])] += 1
    keep: dict[tuple, float] = {}
    for (a, b), c in pairs.items():
        if c >= COOC_MIN_COUNT:
            ratio = c * total / (df[a] * df[b])
            if ratio >= COOC_MIN_RATIO:
                keep[(a, b)] = ratio
    span: dict[tuple, list] = {}
    if keep:
        for u in units.values():
            ents = sorted(u[0] & frequent)[:COOC_MAX_ENTITIES_PER_UNIT]
            for i in range(len(ents)):
                for j in range(i + 1, len(ents)):
                    p = (ents[i], ents[j])
                    if p in keep:
                        s = span.setdefault(p, [u[1], u[2]])
                        s[0], s[1] = min(s[0], u[1]), max(s[1], u[2])
    db.execute(delete(Relationship).where(Relationship.type == "co_occurs"))
    out = [{"subject_id": a, "object_id": b, "type": "co_occurs", "directed": False, "status": "statistical", "evidence_count": pairs[(a, b)], "stat_n": pairs[(a, b)],
            "stat_ratio": round(ratio, 3), "confidence": round(min(0.8, 0.3 + 0.15 * math.log2(ratio)), 3), "first_at": span[(a, b)][0], "last_at": span[(a, b)][1],
            "activity_model": "", "activity_version": "", "activity_by": "", "sensitivity": "public", "ai_use": "allowed"} for (a, b), ratio in keep.items()]
    for i in range(0, len(out), 2000):
        db.execute(insert(Relationship), out[i:i + 2000])
    return {"units": total, "pairs": len(pairs), "kept": len(out)}


# ---- 履歴と矛盾 ----

def _norm_value(value: str) -> str:
    v = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    v = re.sub(r"\s+", "", v)
    return re.sub(r"^v(?=\d)", "", v)


def mark_series(entries: list[dict]) -> list[dict]:
    """同じ物・同じ種類の主張（value を持つもの）を時系列に並べ、印を付ける（元の主張は変えない）。
    mark: first（最初の値）・same（直前の確定した値と同じ）・change（時間を置いて値が変わった。from に前の値）・contradiction（CONTRADICT_DAYS 日以内に別の文書が別の値を述べている）。
    entries は {"t": datetime, "value", "doc_id", ...}。t の昇順に並べて返す。"""
    rows = sorted((dict(e) for e in entries), key=lambda e: (e["t"], e.get("claim_id", 0)))
    window = timedelta(days=CONTRADICT_DAYS)
    for e in rows:
        e["_v"] = _norm_value(e["value"])
    for e in rows:
        e["mark"] = ""
        if any(o["_v"] != e["_v"] and o["doc_id"] != e["doc_id"] and abs(o["t"] - e["t"]) <= window for o in rows):
            e["mark"] = "contradiction"
    cur = None
    for e in rows:
        if e["mark"] == "contradiction":
            continue
        if cur is None:
            e["mark"] = "first"
        elif e["_v"] == cur["_v"]:
            e["mark"] = "same"
        else:
            e["mark"], e["from"] = "change", cur["value"]
        cur = e
    for e in rows:
        e.pop("_v", None)
    return rows


def history(db: Session, entity_id: int, scopes: list[str] | None = None) -> dict:
    """物の主張の履歴: 種類（kind）ごとの時系列と、変化・矛盾の数。value の無い主張は比べられないので入れない。
    時刻は記事の発表日時（ニュース以外は文書の日時、無ければ主張を作った時刻）。scopes に無い区分の主張は読まない。"""
    scopes = scopes or ["public"]
    rows = db.execute(select(Claim, NewsItem.published_at, NewsItem.title, NewsItem.url, Document.id, Document.title, Document.url, Document.published_at)
                      .outerjoin(NewsItem, NewsItem.id == Claim.article_id).outerjoin(NewsDocument, NewsDocument.news_item_id == Claim.article_id)
                      .outerjoin(Document, Document.id == func.coalesce(Claim.document_id, NewsDocument.document_id))
                      .where(Claim.entity_id == entity_id, Claim.value != "", Claim.sensitivity.in_(scopes)).order_by(Claim.id.desc()).limit(MAX_HISTORY_CLAIMS)).all()
    groups: dict[str, list[dict]] = {}
    for c, n_pub, n_title, n_url, d_id, d_title, d_url, d_pub in rows:
        when = _naive(n_pub or d_pub or c.created_at)
        groups.setdefault(c.kind, []).append({"claim_id": c.id, "t": when, "value": c.value, "text": c.text, "doc_id": d_id or -(c.article_id or 0), "title": n_title or d_title or "",
                                              "url": n_url or d_url or "", "ai_extracted": bool(c.ai_extracted)})
    out, changes, contradictions = [], 0, 0
    for kind, entries in groups.items():
        series = mark_series(entries)
        changes += sum(1 for e in series if e["mark"] == "change")
        contradictions += sum(1 for e in series if e["mark"] == "contradiction")
        out.append({"kind": kind, "entries": [{**e, "t": iso(e["t"])} for e in series]})
    out.sort(key=lambda g: g["kind"])
    return {"series": out, "changes": changes, "contradictions": contradictions}


def sweep_relations(db: Session, limit: int = 2000) -> dict:
    """夜: AI が取り出した関係（人が確認していない・co_occurs 以外）のうち、型の定義域・値域に合わない・型 other（内容の空な関係。測定で正しく具体的なものは 0 件）・端がプラットフォームのものを捨てる（根拠ごと）。
    {"dropped", "checked"}。確定（confirmed）は触らない。コミットは呼ぶ側。"""
    from qnapnewshub import thing_filter

    types = {i: (t, n) for i, t, n in db.execute(select(Entity.id, Entity.type, Entity.name))}
    drop: list[int] = []
    checked = 0
    for r in db.scalars(select(Relationship).where(Relationship.type != "co_occurs", Relationship.activity_by == "").order_by(Relationship.id)):
        checked += 1
        s_, o_ = types.get(r.subject_id), types.get(r.object_id)
        if s_ is None or o_ is None:
            continue
        if (not extract.relation_ok(r.type, s_[0], o_[0]) or r.type == "other"
                or (r.type in ("uses", "cites", "supports", "part_of") and (thing_filter.is_platform(s_[1]) or thing_filter.is_platform(o_[1])))
                or thing_filter.is_media(s_[1]) or thing_filter.is_media(o_[1])):
            drop.append(r.id)
            if len(drop) >= limit:
                break
    for i in range(0, len(drop), 400):
        part = drop[i:i + 400]
        db.execute(delete(Evidence).where(Evidence.relationship_id.in_(part)))
        db.execute(delete(Relationship).where(Relationship.id.in_(part)))
    return {"dropped": len(drop), "checked": checked}
