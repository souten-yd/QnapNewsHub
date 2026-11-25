"""文書の保存の段（L0〜L4）・昇格・論文の同定・Metric の時系列・L0 の検索（v0.7 前半。ARCHITECTURE 10 章の「実装と違った点」）。

- **段**（documents.tier）: 0 発見（URL・題・日付・ID だけ）／1 概要・書誌・語／2 本文／3 物・主張・数値・関係・根拠／4 詳しい分析。
  今のニュースの記事は 1〜3 に当てはめる（本文を取り出せた→2、要素の取り出しが済んだ→3、それ以外 1。`news_tiers`・`migrate`。何度流しても同じ）。
  **上げるだけで下げない**。上げるたびに document_promotions へ（理由・時刻）。documents には最後の 1 回（tier_reason・tier_at）。
- **昇格の規則**（`apply_rules`。L0・L1 → L2）: チャンネルの語に合う／ホットトピックの語を含む／監視の物（監視のリポジトリ・モデル・利用者が足した語）を含む／
  スターの急増／監視のモデルの派生版。規則は connector の設定の promotion で 1 つずつ切れる。L3・L4 へは自動では上げない（人が API で、または知識化が済んだとき）。
  L2 に上がった文書の本文の取り出しは、ニュースの記事は既存の bodies が行い、ニュース以外はこの版では**段の印だけ**（本文の取り出しは次の段）。
- **論文の同定**: DOI・arXiv ID・OpenAlex ID・Semantic Scholar ID はすべて同じ論文を指しうるので、documents.ident（1 つ）＋ document_aliases（別名）で 1 件にまとめる。
  DOI が 10.48550/arXiv.<ID>（arXiv の DOI）なら arXiv ID として扱う。後から別の識別子が分かったら別名に足し、題・要旨・引用数を補う（段は上げるだけ）。
- **書き込み**は Write Coordinator のまとめ書きの中（`apply`）。ここの関数はコミットしない。
- **L0 の検索**: ニュース以外の文書の題・要旨を `doc_fts`（trigram・中身を持たない）に入れ、`search`（3 文字未満は LIKE）。ニュースの記事は従来の索引（searchindex）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from qnapnewshub.db import Document, DocumentAlias, DocumentLink, DocumentPromotion, MetricPoint

_UTC = timezone.utc
TIERS = (0, 1, 2, 3, 4)
TIER_LABELS = {0: "L0 発見", 1: "L1 概要", 2: "L2 本文", 3: "L3 知識", 4: "L4 分析"}
DOC_FTS = "doc_fts"
MIGRATION_KEY = "tier_mig_v1"
META_MAX = 4000
ABSTRACT_MAX = 6000
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
_REF = re.compile(r"[a-z0-9_]{1,20}:\S{1,300}")
# Metric の subject の名前空間（物の種類。v0.8）。RWA トークン（rwa_token）とデジタル証券（digital_security）は別の種類として持つ（混ぜない）。
SUBJECT_KINDS = {"repo": "GitHub のリポジトリ", "indicator": "指標（日銀の系列など）", "stat": "政府統計の統計表", "chain": "チェーン", "protocol": "プロトコル", "stablecoin": "ステーブルコイン",
                 "rwa_token": "RWA トークン（トークン化された現実資産）", "digital_security": "デジタル証券（有価証券としてのトークン。RWA トークンとは別）", "asset": "暗号資産（価格）"}
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(v\d+)?$", re.I)


def _now() -> datetime:
    return datetime.now(_UTC).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (value.astimezone(_UTC) if value.tzinfo else value).replace(tzinfo=None)


# ---- 識別子 ----

def norm_doi(value: str) -> str:
    v = unicodedata.normalize("NFKC", str(value or "")).strip()
    v = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", v, flags=re.I).strip().rstrip(".,;)")
    return v.lower() if re.match(r"^10\.\d{4,9}/\S+$", v) else ""


def norm_arxiv(value: str) -> str:
    v = unicodedata.normalize("NFKC", str(value or "")).strip()
    v = re.sub(r"^(?:https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", v, flags=re.I).removesuffix(".pdf").strip()
    m = _ARXIV_ID.match(v)
    return m.group(1).lower() if m else ""


def paper_idents(ids: dict) -> list[str]:
    """論文の識別子の一覧（優先順: arxiv・doi・openalex・s2・url）。arXiv の DOI は arXiv ID にそろえる。1 つも無ければ空。"""
    out: list[str] = []
    doi = norm_doi(ids.get("doi", ""))
    m = _ARXIV_DOI.match(doi) if doi else None
    arxiv = norm_arxiv(ids.get("arxiv", "")) or (norm_arxiv(m.group(1)) if m else "")
    if arxiv:
        out.append(f"arxiv:{arxiv}")
    if doi and not m:
        out.append(f"doi:{doi}")
    oa = re.sub(r"^https?://openalex\.org/", "", str(ids.get("openalex", "") or "")).strip().upper()
    if re.fullmatch(r"W\d{3,12}", oa):
        out.append(f"openalex:{oa}")
    s2 = str(ids.get("s2", "") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", s2):
        out.append(f"s2:{s2}")
    ref = str(ids.get("ref", "") or "").strip()
    if _REF.fullmatch(ref):      # 名前空間つきの識別子（v0.8: kokkai:・egov:・edinet:・pypi:・npm:・osv:）。同じ物は 1 件
        out.append("ref:" + ref)
    url = str(ids.get("url", "") or "").strip()
    if url.startswith(("http://", "https://")):
        out.append("url:" + _url_key(url))
    return list(dict.fromkeys(out))


def _url_key(url: str) -> str:
    from qnapnewshub.kg import _canon_url

    return _canon_url(url)


# ---- 検索の索引（ニュース以外の文書） ----

def ensure(conn) -> None:
    """開くときに 1 回（冪等）。ニュース以外の文書の題・要旨の索引（trigram）とトリガー。作れない SQLite では何もしない（検索は LIKE）。"""
    if conn.dialect.name != "sqlite":
        return
    try:
        conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {DOC_FTS} USING fts5(title, abstract, content='', contentless_delete=1, tokenize='trigram')"))
        ins = f"INSERT INTO {DOC_FTS}(rowid, title, abstract) VALUES (new.id, new.title, new.abstract);"
        dele = f"DELETE FROM {DOC_FTS} WHERE rowid = old.id;"
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS documents_fts_ai AFTER INSERT ON documents WHEN new.kind <> 'news' BEGIN {ins} END"))
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS documents_fts_ad AFTER DELETE ON documents WHEN old.kind <> 'news' BEGIN {dele} END"))
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS documents_fts_au AFTER UPDATE OF title, abstract ON documents WHEN new.kind <> 'news' BEGIN {dele} {ins} END"))
        if conn.execute(text("SELECT 1 FROM news_meta WHERE key = 'doc_fts_v1'")).first() is None:      # 既にある行（この版より前に入れた文書）を 1 回だけ入れる
            conn.execute(text(f"INSERT OR REPLACE INTO {DOC_FTS}(rowid, title, abstract) SELECT id, title, abstract FROM documents WHERE kind <> 'news'"))
            conn.execute(text("INSERT INTO news_meta(key, value) VALUES ('doc_fts_v1', '1')"))
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("qnapnewshub.docstore").warning("文書の検索の索引を用意できません（LIKE に落ちます）")


def search(db: Session, q: str, *, kind: str = "", tier: int | None = None, limit: int = 30) -> list[Document]:
    """ニュース以外の文書を題・要旨で探す（L0 も対象）。3 文字以上は trigram の索引、短いときは LIKE。"""
    q = " ".join(str(q or "").split())[:200]
    if not q:
        return []
    limit = max(1, min(int(limit), 100))
    cond = select(Document).where(Document.kind != "news")
    if kind:
        cond = cond.where(Document.kind == kind)
    if tier is not None:
        cond = cond.where(Document.tier >= tier)
    ids: list[int] = []
    if len(q) >= 3:
        phrase = '"' + q.replace('"', '""') + '"'
        try:
            rows = db.execute(text(f"SELECT rowid FROM {DOC_FTS} WHERE {DOC_FTS} MATCH :q ORDER BY bm25({DOC_FTS}, 4.0, 1.0) LIMIT 400"), {"q": phrase}).all()
            ids = [r[0] for r in rows]
        except Exception:  # noqa: BLE001 - 索引が無い（FTS5 なし）: LIKE へ
            ids = []
        if ids:
            got = {d.id: d for d in db.scalars(cond.where(Document.id.in_(ids)))}
            return [got[i] for i in ids if i in got][:limit]
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return list(db.scalars(cond.where(Document.title.like(like, escape="\\") | Document.abstract.like(like, escape="\\")).order_by(Document.id.desc()).limit(limit)))


def search_any(db: Session, terms: list[str], *, limit: int = 100) -> list[tuple[int, float]]:
    """問いの文から取った語（3 文字以上）の、どれかを題・要旨に含む（OR）ニュース以外の文書 [(文書の id, 点数)]（点数の大きい順。点数は BM25 の符号を反転）。
    v0.6 の種の検索（retrieval.seeds）が使う。索引（doc_fts）が無いときは空（呼ぶ側が題の LIKE で補う）。"""
    terms = [t for t in terms if len(t) >= 3]
    if not terms:
        return []
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    try:
        rows = db.execute(text(f"SELECT rowid, bm25({DOC_FTS}, 4.0, 1.0) AS r FROM {DOC_FTS} WHERE {DOC_FTS} MATCH :q ORDER BY r LIMIT :k"), {"q": match, "k": limit}).all()
    except Exception:  # noqa: BLE001 - 索引が無い
        return []
    return [(int(i), -float(r)) for i, r in rows]


# ---- 段・昇格 ----

def promote(db: Session, doc: Document, to_tier: int, reason: str, now: datetime | None = None) -> bool:
    """段を上げる（下げない・同じ段なら何もしない）。上げたら True。コミットは呼ぶ側。"""
    if to_tier not in TIERS:
        raise ValueError("段は 0〜4 です")
    if to_tier <= (doc.tier or 0):
        return False
    now = now or _now()
    db.add(DocumentPromotion(document_id=doc.id, from_tier=doc.tier or 0, to_tier=to_tier, reason=reason[:160], at=now))
    doc.tier, doc.tier_reason, doc.tier_at = to_tier, reason[:160], now
    return True


def promotions(db: Session, doc_id: int, limit: int = 20) -> list[dict]:
    rows = db.scalars(select(DocumentPromotion).where(DocumentPromotion.document_id == doc_id).order_by(DocumentPromotion.id.desc()).limit(limit))
    return [{"from": r.from_tier, "to": r.to_tier, "reason": r.reason, "at": r.at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.at else ""} for r in rows]


def news_tiers(db: Session, article_ids: list[int] | None = None) -> int:
    """ニュースの文書を、いまの状態の段へ上げる（本文を取り出せた→L2・要素の取り出しが済んだ→L3）。冪等。article_ids を渡せばその記事だけ。上げた文書の数を返す。コミットは呼ぶ側。"""
    now = _now().strftime("%Y-%m-%d %H:%M:%S.%f")      # text() の束縛に datetime を直接渡さない（Python 3.12 の非推奨）
    scope = ""
    if article_ids is not None:
        if not article_ids:
            return 0
        scope = f" AND n.id IN ({','.join(str(int(i)) for i in article_ids[:2000])})"
    done = 0
    for to, cond, reason in ((3, "n.extract_v > 0", "要素の取り出しが済んだ（L3）"), (2, "n.body_state = 1", "本文を取り出せた（L2）")):
        base = (f"FROM documents d WHERE d.kind = 'news' AND d.tier < {to} AND d.id IN "
                f"(SELECT nd.document_id FROM news_documents nd JOIN news_items n ON n.id = nd.news_item_id WHERE {cond}{scope})")
        db.execute(text(f"INSERT INTO document_promotions (document_id, from_tier, to_tier, reason, at) SELECT d.id, d.tier, {to}, :r, :now {base}"), {"r": reason, "now": now})
        done += db.execute(text(f"UPDATE documents SET tier = {to}, tier_reason = :r, tier_at = :now WHERE kind = 'news' AND tier < {to} AND id IN "
                                f"(SELECT nd.document_id FROM news_documents nd JOIN news_items n ON n.id = nd.news_item_id WHERE {cond}{scope})"),
                           {"r": reason, "now": now}).rowcount or 0
    return done


def migrate(db: Session) -> int:
    """既存のニュースの文書を L1〜L3 に当てはめる移行（冪等。何度流しても同じ）。先に文書の対応を作ってから段を決める。"""
    from qnapnewshub import kg

    kg.sync_documents(db)
    return news_tiers(db)


def tier_counts(db: Session) -> dict[str, dict[str, int]]:
    """種類×段の文書の数 {kind: {"0": n, ...}}（画面・診断用）。"""
    out: dict[str, dict[str, int]] = {}
    for kind, tier, n in db.execute(select(Document.kind, Document.tier, func.count()).group_by(Document.kind, Document.tier)):
        out.setdefault(kind, {})[str(tier)] = int(n)
    return out


# ---- 昇格の規則 ----

def rules_context(db: Session, promo: dict) -> dict:
    """規則に使う語（チャンネル・ホットトピック・監視の物）を集める。規則を切っていれば集めない。"""
    from qnapnewshub import channels, connectors, sources

    ctx: dict = {"channels": [], "hot": [], "watch": [], "surge": float(promo.get("surge_stars", 100))}
    if promo.get("channel"):
        ctx["channels"] = [c for c in channels.load()["channels"] if c.get("include")]
    if promo.get("hot"):
        try:
            from qnapnewshub import analytics

            top = analytics.hot_topics(db).get("top", [])
            ctx["hot"] = [str(r.get("label") or r.get("term")) for r in top[:10] if len(str(r.get("label") or r.get("term"))) >= 3]
        except Exception:  # noqa: BLE001 - 集計が無くても昇格の他の規則は動く
            ctx["hot"] = []
    if promo.get("watch"):
        cfg = sources.load_config()
        names = [r.split("/", 1)[1] for r in cfg.get("github", []) if "/" in r]
        names += [m.split("/")[-1] for m in connectors.watched_models()]
        names += connectors.watched_terms()
        ctx["watch"] = [n for n in dict.fromkeys(names) if len(n) >= 3]
    return ctx


def apply_rules(db: Session, docs: list[Document], promo: dict, ctx: dict | None = None) -> int:
    """L0・L1 の文書を L2 へ上げる規則（理由つき）。上げた数を返す。規則は上から見て最初に合ったもの。"""
    from qnapnewshub import channels

    ctx = ctx or rules_context(db, promo)
    done = 0
    for doc in docs:
        if (doc.tier or 0) >= 2 or doc.kind == "news":
            continue
        body = f"{doc.title}\n{doc.abstract}"
        meta = _loads(doc.meta)
        reason = ""
        if promo.get("surge") and float(meta.get("stars_delta") or 0) >= ctx["surge"]:
            reason = f"スターの急増（1 日で +{int(meta['stars_delta'])}）"
        elif promo.get("derivative") and meta.get("derivative_of"):
            reason = f"監視のモデル {meta['derivative_of']} の派生版（{meta.get('relation', '')}）"
        if not reason:
            for ch in ctx["channels"]:
                if ch.get("exclude") and channels.has_terms(body, ch["exclude"]):
                    continue
                if channels.has_terms(body, ch["include"]):
                    reason = f"チャンネル「{ch['name']}」に合う"
                    break
        if not reason and ctx["hot"]:
            hit = next((t for t in ctx["hot"] if t.lower() in body.lower()), "")
            reason = f"ホットトピック「{hit}」を含む" if hit else ""
        if not reason and ctx["watch"]:
            hit = next((t for t in ctx["watch"] if t.lower() in body.lower()), "")
            reason = f"監視の物「{hit}」を含む" if hit else ""
        if reason and promote(db, doc, 2, reason):
            done += 1
    return done


# ---- 文書の入れ方 ----

def _loads(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _find(db: Session, idents: list[str]) -> Document | None:
    if not idents:
        return None
    doc = db.scalar(select(Document).where(Document.ident.in_(idents)).order_by(Document.id).limit(1))
    if doc is not None:
        return doc
    alias = db.scalar(select(DocumentAlias).where(DocumentAlias.ident.in_(idents)).limit(1))
    return db.get(Document, alias.document_id) if alias else None


def upsert(db: Session, rec: dict, now: datetime | None = None) -> tuple[Document | None, bool]:
    """connector が見つけた 1 件を文書にする（識別子で同定。同じものは 1 件）。rec: {kind, ids{doi,arxiv,openalex,s2,url}, title, url, source, published_at, version,
    tier(0|1), abstract, meta{}, connector, reason}。{doc, 新しく作ったか}。識別子が 1 つも無ければ (None, False)。区分は public・allowed（公開の情報源だけ）。"""
    now = now or _now()
    idents = paper_idents(rec.get("ids") or {})
    if not idents:
        return None, False
    tier = int(rec.get("tier", 1))
    meta_in = {k: v for k, v in (rec.get("meta") or {}).items() if v not in (None, "", [])}
    doc = _find(db, idents)
    created = doc is None
    if created:
        doc = Document(kind=rec["kind"], ident=idents[0], title=str(rec.get("title") or "")[:400], url=str(rec.get("url") or "")[:1000],
                       source=str(rec.get("source") or "")[:120], published_at=_naive(rec.get("published_at")), version=str(rec.get("version") or "")[:64],
                       sensitivity="public", ai_use=rec.get("ai_use", "allowed"), abstract=str(rec.get("abstract") or "")[:ABSTRACT_MAX],
                       meta=json.dumps(meta_in, ensure_ascii=False)[:META_MAX] if meta_in else "", connector=str(rec.get("connector") or "")[:40],
                       tier=tier, tier_reason=str(rec.get("reason") or "取得時の段")[:160], tier_at=now)
        db.add(doc)
        db.flush()
    else:
        doc.title = doc.title or str(rec.get("title") or "")[:400]
        doc.url = doc.url or str(rec.get("url") or "")[:1000]
        doc.source = doc.source or str(rec.get("source") or "")[:120]
        doc.published_at = doc.published_at or _naive(rec.get("published_at"))
        if rec.get("version"):
            doc.version = str(rec["version"])[:64]
        doc.abstract = doc.abstract or str(rec.get("abstract") or "")[:ABSTRACT_MAX]
        if meta_in:
            merged = {**_loads(doc.meta), **meta_in}
            doc.meta = json.dumps(merged, ensure_ascii=False)[:META_MAX]
        if tier > (doc.tier or 0):
            promote(db, doc, tier, str(rec.get("reason") or "connector が詳しい情報を得た"), now)
    have = {doc.ident, *[a for a in db.scalars(select(DocumentAlias.ident).where(DocumentAlias.document_id == doc.id))]}
    for ident in idents:
        if ident not in have and db.get(DocumentAlias, ident) is None and db.scalar(select(Document.id).where(Document.ident == ident)) is None:
            db.add(DocumentAlias(ident=ident, document_id=doc.id))
    db.flush()
    return doc, created


def apply(db: Session, batches: list[dict]) -> dict:
    """まとめ書きの 1 トランザクションの中で、connector の結果（文書・辺・Metric）を入れ、昇格の規則を当てる。
    batch: {connector, docs: [rec...], links: [{src: rec, dst: rec, type}], metrics: [{subject, metric, value, unit, at, source, cond}]}。コミットは呼ぶ側。"""
    from qnapnewshub import connectors

    stats = {"created": 0, "updated": 0, "promoted": 0, "metrics": 0, "links": 0}
    touched: dict[int, Document] = {}
    now = _now()
    for batch in batches:
        cid = batch.get("connector", "")
        for rec in batch.get("docs", []):
            rec = {**rec, "connector": rec.get("connector") or cid}
            doc, created = upsert(db, rec, now)
            if doc is not None:
                stats["created" if created else "updated"] += 1
                touched[doc.id] = doc
        for link in batch.get("links", []):
            a, _ = upsert(db, {**link["src"], "connector": cid}, now)
            b, _ = upsert(db, {**link["dst"], "connector": cid}, now)
            if a is not None and b is not None and a.id != b.id:
                touched[a.id], touched[b.id] = a, b
                if db.scalar(select(DocumentLink.id).where(DocumentLink.src_id == a.id, DocumentLink.dst_id == b.id, DocumentLink.type == link.get("type", "cites"))) is None:
                    db.add(DocumentLink(src_id=a.id, dst_id=b.id, type=link.get("type", "cites"), source=cid, at=now))
                    stats["links"] += 1
        for m in batch.get("metrics", []):
            at = _naive(m["at"])
            exists = db.scalar(select(MetricPoint.id).where(MetricPoint.subject == m["subject"][:300], MetricPoint.metric == m["metric"][:60], MetricPoint.at == at,
                                                            MetricPoint.source == m.get("source", cid)[:40], MetricPoint.cond == m.get("cond", "")[:200]))
            if exists is None:
                db.add(MetricPoint(subject=m["subject"][:300], metric=m["metric"][:60], value=float(m["value"]), unit=m.get("unit", "")[:20], at=at,
                                   source=m.get("source", cid)[:40], cond=m.get("cond", "")[:200], document_id=m.get("document_id")))
                stats["metrics"] += 1
                if m["metric"] == "stars" and m["subject"].startswith("repo:"):
                    _stars_delta(db, m, at, touched, stats)
    if touched:
        promo = connectors.promotion_config()
        stats["promoted"] = apply_rules(db, list(touched.values()), promo)
    return stats


def _stars_delta(db: Session, m: dict, at: datetime, touched: dict, stats: dict) -> None:
    """スター数の点が入ったら、前の点との 1 日あたりの差を Metric（stars_delta_1d）にし、文書の meta にも入れる（昇格の規則「スターの急増」が読む）。前の点が無ければ何もしない。"""
    from datetime import timedelta

    prev = db.scalar(select(MetricPoint).where(MetricPoint.subject == m["subject"][:300], MetricPoint.metric == "stars", MetricPoint.at < at).order_by(MetricPoint.at.desc()).limit(1))
    if prev is None:
        return
    days = max(1, round((at - prev.at) / timedelta(days=1)))
    delta = (float(m["value"]) - prev.value) / days
    cond = f"vs {prev.at.strftime('%Y-%m-%d')}"
    db.add(MetricPoint(subject=m["subject"][:300], metric="stars_delta_1d", value=delta, unit="count/day", at=at, source=m.get("source", "")[:40], cond=cond))
    stats["metrics"] += 1
    doc = _find(db, paper_idents({"url": f"https://github.com/{m['subject'][5:]}"}))
    if doc is not None:
        meta = _loads(doc.meta)
        meta.update(stars=int(m["value"]), stars_delta=delta)
        doc.meta = json.dumps(meta, ensure_ascii=False)[:META_MAX]
        touched[doc.id] = doc


# ---- Metric の読み ----

def series(db: Session, subject: str, metric: str, days: int = 30) -> list[dict]:
    from datetime import timedelta

    since = _now() - timedelta(days=max(1, min(days, 400)))
    rows = db.scalars(select(MetricPoint).where(MetricPoint.subject == subject, MetricPoint.metric == metric, MetricPoint.at >= since).order_by(MetricPoint.at))
    return [{"at": r.at.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": r.value, "unit": r.unit, "source": r.source, "cond": r.cond} for r in rows]


def latest_before(db: Session, subject: str, metric: str, before: datetime) -> MetricPoint | None:
    return db.scalar(select(MetricPoint).where(MetricPoint.subject == subject, MetricPoint.metric == metric, MetricPoint.at < _naive(before)).order_by(MetricPoint.at.desc()).limit(1))


def doc_dict(d: Document) -> dict:
    from qnapnewshub import kg

    out = kg.doc_dict(d)
    out.update(tier=d.tier, tier_label=TIER_LABELS.get(d.tier, ""), tier_reason=d.tier_reason, tier_at=kg.iso(d.tier_at), abstract=d.abstract, meta=_loads(d.meta), connector=d.connector)
    return out


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
