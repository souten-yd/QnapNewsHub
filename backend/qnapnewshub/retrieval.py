"""ControlDeck の MPKR が使う検索の道具（v0.6）: 種の検索・局所 PageRank・根拠のまとめ取り・調査の記録の読み取り。MPKR 本体（計画・融合・reranker）は ControlDeck 側。
ここは軽い道具だけ。既存を使い回す: 全文は searchindex（trigram＋英語の BM25）、物は entities・entity_aliases、イベントは events、辺は kg_view.edge_rows、区分は kg の scopes。

- すべて読むだけ（書かない）。区分（scopes）の外の文書・物・辺・根拠は、存在も出さない（見つからない扱い）。
- 時間（Budget）・件数の上限つき。上限で切ったら truncated と理由（reason）を返す。
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from qnapnewshub import docstore, kg, kg_view, searchindex, things
from qnapnewshub.db import Claim, Document, Entity, EntityAlias, Event, EventArticle, Evidence, NewsDocument, NewsItem, Relationship, ResearchRecord
from qnapnewshub.entities import normalize_key
from qnapnewshub.rcache import Budget
from qnapnewshub.store import visible

SEED_KINDS = ("document", "entity", "event", "claim")
TERMS_MAX = 12
STOP_ASCII = frozenset("the a an of and or is are was were what how why who when where which to in on for with about vs between".split())
STOP_JA = frozenset(["違い", "方法", "最近", "説明", "概要", "教え", "場合", "今回", "現在", "以前", "最新"])
_ASCII = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]*[A-Za-z0-9+]|[A-Za-z0-9]")
_KATA = re.compile(r"[ァ-ヿㇰ-ㇿー]{2,}")
_KANJI = re.compile(r"[㐀-䶿一-鿿々〆]{2,}")


def query_terms(q: str) -> list[str]:
    """問いの文から検索の語を取る（形態素解析なし）。ASCII の語・カタカナの連なり・漢字の連なりを取り、ひらがな（助詞・語尾）で切る。
    4 文字を超える連なりは、連なりそのもの＋3 文字の窓（最大 6）に分ける（trigram の索引で、問いの言い回しが少し違っても当たるように）。語は最大 TERMS_MAX。"""
    text = unicodedata.normalize("NFKC", str(q or ""))
    out: list[str] = []

    def add(t: str) -> None:
        if t and t.casefold() not in {x.casefold() for x in out}:
            out.append(t)

    for m in _ASCII.finditer(text):
        t = m.group(0).strip(".-_")
        if t.lower() not in STOP_ASCII and len(t) >= 2 or t.isdigit() and len(t) >= 2:
            add(t)
    runs = [m.group(0) for rx in (_KATA, _KANJI) for m in rx.finditer(text) if m.group(0) not in STOP_JA]
    for t in runs:
        add(t)
    for t in runs:      # 窓は、連なりそのものを全部入れたあとに（窓で語の枠を使い切らない）
        if len(t) > 4:
            for i in range(min(len(t) - 2, 6)):
                add(t[i:i + 3])
    return out[:TERMS_MAX]


def _split(terms: list[str]) -> tuple[list[str], list[str], list[str]]:
    """全文の索引で引ける語に分ける: (trigram＝ASCII 以外で 3 文字以上, 英語＝ASCII, 短い語＝ASCII 以外で 2 文字)。"""
    tri = [t for t in terms if not t.isascii() and len(t) >= 3]
    word = [t for t in terms if t.isascii() and re.search(r"[A-Za-z0-9]", t)]
    short = [t for t in terms if not t.isascii() and len(t) < 3]
    return tri, word, short


def _find(text: str, terms: list[str]) -> tuple[int, int, str] | None:
    low = text.casefold()
    best = None
    for t in terms:
        i = low.find(t.casefold())
        if i >= 0 and (best is None or i < best[0]):
            best = (i, i + len(t), t)
    return best


def _position(item: NewsItem, terms: list[str]) -> dict:
    """根拠の位置: 語が最初に出る場所（題・概要・抜粋）と、その前後の抜粋。"""
    for field, value in (("title", item.title), ("summary_ja", item.summary_ja), ("summary", item.summary)):
        found = _find(value or "", terms)
        if found:
            a, b, t = found
            return {"field": field, "start": a, "end": b, "term": t, "snippet": (value or "")[max(0, a - 40):b + 80]}
    return {"field": "index", "start": None, "end": None, "term": "", "snippet": ""}


def _doc_seeds(db: Session, q: str, terms: list[str], *, scopes: list[str], doc_kinds: list[str], since: datetime | None, until: datetime | None, limit: int,
               budget: Budget) -> list[dict]:
    out: list[dict] = []
    tri, word, short = _split(terms)
    want_news = (not doc_kinds or "news" in doc_kinds) and "public" in scopes
    if want_news and (tri or word):
        hits = searchindex.search_any(db.connection(), tri, word, limit=max(limit * 4, 100))
        if hits:
            top = hits[0][1] or 1.0
            ids = [i for i, _ in hits]
            conds = [NewsItem.id.in_(ids), visible(0), NewsItem.kind != "event"]
            if since:
                conds.append(NewsItem.published_at >= kg._naive(since))
            if until:
                conds.append(NewsItem.published_at <= kg._naive(until))
            rows = {n.id: n for n in db.scalars(select(NewsItem).where(*conds))}
            mapped = dict(db.execute(select(NewsDocument.news_item_id, NewsDocument.document_id).where(NewsDocument.news_item_id.in_(list(rows)))).all()) if rows else {}
            docs = {d.id: d for d in db.scalars(select(Document).where(Document.id.in_(set(mapped.values())), Document.sensitivity.in_(scopes)))} if mapped else {}
            for aid, score in hits:
                n = rows.get(aid)
                if n is None:
                    continue
                d = docs.get(mapped.get(aid, 0))
                if mapped.get(aid) and d is None:
                    continue      # 区分の範囲の外
                out.append({"kind": "document", "id": d.id if d else None, "article_id": n.id, "doc_kind": "news", "title": n.title, "url": n.url, "source": n.source,
                            "published_at": kg.iso(n.published_at), "sensitivity": d.sensitivity if d else "public", "score": round(score / top, 4), "bm25": round(score, 3),
                            "match": {"via": "fulltext", "terms": [t for t in terms if t.casefold() in f"{n.title} {n.summary_ja} {n.summary}".casefold()][:6]},
                            "position": _position(n, terms)})
                if len(out) >= limit:
                    break
    elif want_news and short and not budget.expired():      # 短い語だけ: 新しい題から LIKE（時間の上限つき）
        conds = [visible(0), NewsItem.kind != "event", *[NewsItem.title.contains(t, autoescape=True) for t in short]]
        if since:
            conds.append(NewsItem.published_at >= kg._naive(since))
        if until:
            conds.append(NewsItem.published_at <= kg._naive(until))
        for n in db.scalars(select(NewsItem).where(*conds).order_by(NewsItem.published_at.desc()).limit(limit)):
            did = db.scalar(select(NewsDocument.document_id).where(NewsDocument.news_item_id == n.id))
            out.append({"kind": "document", "id": did, "article_id": n.id, "doc_kind": "news", "title": n.title, "url": n.url, "source": n.source, "published_at": kg.iso(n.published_at),
                        "sensitivity": "public", "score": 0.3, "bm25": 0.0, "match": {"via": "title_like", "terms": short}, "position": _position(n, short)})
    # ニュース以外の文書（論文・リポジトリ・特許…）: 題の部分一致（語の数が多い順）
    others = [k for k in (doc_kinds or kg.DOC_KINDS) if k != "news"]
    if others and terms and not budget.expired():
        conds = [Document.kind.in_(others), Document.sensitivity.in_(scopes), or_(*[Document.title.contains(t, autoescape=True) for t in terms])]
        if since:
            conds.append(Document.published_at >= kg._naive(since))
        if until:
            conds.append(Document.published_at <= kg._naive(until))
        scored: dict[int, tuple[float, Document, list[str], str]] = {}
        for d in db.scalars(select(Document).where(*conds).limit(500)):      # 題の部分一致（短い語・索引が無いとき）
            hit = [t for t in terms if t.casefold() in d.title.casefold()]
            scored[d.id] = (0.8 * len(hit) / len(terms), d, hit, "title")
        fts = docstore.search_any(db, terms, limit=max(limit * 4, 100))      # 題・要旨の全文（doc_fts。BM25）
        if fts:
            top = fts[0][1] or 1.0
            sc = dict(fts)
            base = [Document.id.in_(list(sc)), Document.kind.in_(others), Document.sensitivity.in_(scopes)]
            if since:
                base.append(Document.published_at >= kg._naive(since))
            if until:
                base.append(Document.published_at <= kg._naive(until))
            for d in db.scalars(select(Document).where(*base)):
                hit = [t for t in terms if t.casefold() in f"{d.title} {d.abstract}".casefold()]
                v = 0.9 * sc[d.id] / top
                if d.id not in scored or scored[d.id][0] < v:
                    scored[d.id] = (v, d, hit, "fulltext")
        for sc_, d, hit, via in sorted(scored.values(), key=lambda x: (-x[0], -x[1].id))[:limit]:
            f = _find(d.title, hit)
            out.append({"kind": "document", "id": d.id, "article_id": None, "doc_kind": d.kind, "title": d.title, "url": d.url, "source": d.source, "published_at": kg.iso(d.published_at),
                        "sensitivity": d.sensitivity, "score": round(sc_, 4), "bm25": 0.0, "match": {"via": via, "terms": hit}, "tier": d.tier,
                        "position": {"field": "title", "start": f[0], "end": f[1], "term": f[2], "snippet": d.title[:120]} if f else {"field": "abstract", "start": None, "end": None, "term": "", "snippet": ""}})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


CLAIM_MIN_RATIO = 0.8      # 主張の種は、最上位の BM25 のこの割合以上だけ


def _claim_seeds(db: Session, terms: list[str], *, scopes: list[str], since: datetime | None, until: datetime | None, limit: int, budget: Budget) -> list[dict]:
    """主張（claims）を直接引く: 主張の本文・値と、主語の物の名前・別名の全文索引（trigram＋英語の BM25。記事の索引と同じ作り）を、問いの語の OR で。
    各主張に、主語の物と根拠の文書（evidences → documents。区分の範囲のものだけ。最大 3）を付ける（主張から根拠の文書へ辿れる）。点数は最上位を 1 とした割合。"""
    tri, word, _ = _split(terms)
    if not (tri or word) or budget.expired():
        return []
    hits = searchindex.search_claims(db.connection(), tri, word, limit=max(limit * 4, 60))
    if not hits:
        return []
    top = hits[0][1] or 1.0
    rows = {c.id: c for c in db.scalars(select(Claim).where(Claim.id.in_([i for i, _ in hits]), Claim.sensitivity.in_(scopes)))}
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_({c.entity_id for c in rows.values() if c.entity_id}), Entity.sensitivity.in_(scopes)))} if rows else {}
    docs: dict[int, list[dict]] = {}
    if rows:
        for claim_id, d in db.execute(select(Evidence.claim_id, Document).join(Document, Document.id == Evidence.document_id)
                                      .where(Evidence.claim_id.in_(list(rows)), Document.sensitivity.in_(scopes)).order_by(Evidence.confidence.desc(), Evidence.id)):
            lst = docs.setdefault(claim_id, [])
            if len(lst) < 3 and all(x["id"] != d.id for x in lst):
                lst.append({"id": d.id, "title": d.title, "source": d.source, "published_at": kg.iso(d.published_at), "label": things.doc_label(d.source, kg.iso(d.published_at), d.id)})
    out: list[dict] = []
    for cid, score in hits:
        if score < top * CLAIM_MIN_RATIO:
            break      # 最上位の点数の一定の割合に満たない主張は出さない（短い文なので、語 1 つだけ当たった主張が大量に並んで文書の種を押し出す）
        c = rows.get(cid)
        if c is None or (c.entity_id and c.entity_id not in ents):
            continue
        ds = docs.get(cid, [])
        when = ds[0]["published_at"] if ds else kg.iso(c.created_at)
        if (since and when and when < kg.iso(since)) or (until and when and when > kg.iso(until)):
            continue
        e = ents.get(c.entity_id)
        blob = f"{c.text} {c.value} {e.name if e else ''}".casefold()
        out.append({"kind": "claim", "id": cid, "text": c.text, "value": c.value, "claim_kind": c.kind, "entity": {"id": e.id, "name": e.name, "type": e.type} if e else None,
                    "article_id": c.article_id, "documents": ds, "published_at": when, "sensitivity": c.sensitivity, "ai_extracted": bool(c.ai_extracted),
                    "score": round(score / top, 4), "bm25": round(score, 3), "match": {"via": "fulltext", "terms": [t for t in terms if t.casefold() in blob][:6]}})
        if len(out) >= limit:
            break
    return out


def _entity_seeds(db: Session, q: str, terms: list[str], *, scopes: list[str], since: datetime | None, until: datetime | None, limit: int, budget: Budget) -> list[dict]:
    """物の名前・別名: 完全一致（正規化のキー）を最優先、問いの文に名前が含まれる、名前・別名に語が含まれる（部分）の順。同じ質なら記事の多い物。"""
    qn = unicodedata.normalize("NFKC", q).casefold()
    cands: dict[int, tuple[float, str, str]] = {}      # id -> (質, via, 語)

    def put(eid: int, quality: float, via: str, term: str) -> None:
        if eid not in cands or cands[eid][0] < quality:
            cands[eid] = (quality, via, term)

    pieces = unicodedata.normalize("NFKC", q).split()
    names = list(dict.fromkeys([*terms, *[" ".join(pieces[i:i + n]) for n in (2, 3) for i in range(len(pieces) - n + 1)]]))      # 語＋続く 2〜3 語（"Tongyi Qianwen" のような名前）
    keys = {normalize_key(t): t for t in names if normalize_key(t)}
    if keys:
        for e in db.scalars(select(Entity).where(Entity.key.in_(list(keys)), Entity.sensitivity.in_(scopes))):
            put(e.id, 1.0, "name_exact", keys.get(e.key, e.name))
        for eid, alias in db.execute(select(EntityAlias.entity_id, EntityAlias.alias).where(func.lower(EntityAlias.alias).in_([t.casefold() for t in names]))):
            put(eid, 0.9, "alias_exact", alias)
    for t in terms:
        if budget.expired() or len(t) < 2:
            break
        pat = Entity.name.ilike(f"%{t.replace(chr(92), chr(92) * 2).replace('%', chr(92) + '%').replace('_', chr(92) + '_')}%", escape="\\")
        for e in db.scalars(select(Entity).where(pat, Entity.sensitivity.in_(scopes)).order_by(Entity.article_count.desc()).limit(30)):
            nm = unicodedata.normalize("NFKC", e.name).casefold()
            if len(nm) >= 2 and nm in qn:
                put(e.id, 0.85, "in_query", e.name)      # 名前の全体が問いの文に出る
            else:
                put(e.id, 0.4 + 0.3 * min(1.0, len(t) / max(1, len(nm))), "partial", t)
    if not cands:
        return []
    rows = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(list(cands)), Entity.sensitivity.in_(scopes)))}
    scored = []
    for eid, (quality, via, term) in cands.items():
        e = rows.get(eid)
        if e is None:
            continue
        if since and e.last_seen and kg._naive(e.last_seen) < kg._naive(since):
            continue
        if until and e.first_seen and kg._naive(e.first_seen) > kg._naive(until):
            continue
        pop = min(1.0, __import__("math").log10(1 + int(e.article_count or 0)) / 4)
        scored.append((quality * (0.7 + 0.3 * pop), e, via, term))
    out = []
    for sc, e, via, term in sorted(scored, key=lambda x: (-x[0], -int(x[1].article_count or 0), x[1].id))[:limit]:
        f = _find(unicodedata.normalize("NFKC", q), [e.name, term])
        out.append({"kind": "entity", "id": e.id, "name": e.name, "type": e.type, "article_count": int(e.article_count or 0), "first_seen": kg.iso(e.first_seen),
                    "last_seen": kg.iso(e.last_seen), "sensitivity": e.sensitivity, "score": round(sc, 4), "match": {"via": via, "terms": [term]},
                    "position": {"field": "query", "start": f[0], "end": f[1], "term": f[2]} if f else {}})
    return out


def _event_seeds(db: Session, terms: list[str], entity_hits: list[dict], *, since: datetime | None, until: datetime | None, limit: int, budget: Budget) -> list[dict]:
    """イベント（同じ出来事の記事のまとまり）: 題に語を含むもの（語の数の割合）と、強く一致した物（上位 3）に関わるもの。"""
    found: dict[int, tuple[float, Event, str, list[str]]] = {}
    base = [Event.n_articles >= 2]
    if since:
        base.append(Event.last_at >= kg._naive(since))
    if until:
        base.append(Event.first_at <= kg._naive(until))
    if terms and not budget.expired():
        for ev in db.scalars(select(Event).where(*base, or_(*[Event.title.contains(t, autoescape=True) for t in terms])).order_by(Event.last_at.desc()).limit(200)):
            hit = [t for t in terms if t.casefold() in ev.title.casefold()]
            found[ev.id] = (0.9 * len(hit) / len(terms), ev, "title", hit)
    from qnapnewshub.db import ArticleEntity

    for eh in [e for e in entity_hits if e["score"] >= 0.6][:3]:
        if budget.expired():
            break
        sub = select(EventArticle.event_id).join(ArticleEntity, ArticleEntity.article_id == EventArticle.article_id).where(ArticleEntity.entity_id == eh["id"])
        for ev in db.scalars(select(Event).where(*base, Event.id.in_(sub)).order_by(Event.last_at.desc()).limit(10)):
            sc = 0.5 * eh["score"]
            if ev.id not in found or found[ev.id][0] < sc:
                found[ev.id] = (sc, ev, "entity", [eh["name"]])
    out = []
    for sc, ev, via, hit in sorted(found.values(), key=lambda x: (-x[0], -kg._naive(x[1].last_at).timestamp()))[:limit]:
        f = _find(ev.title, hit)
        out.append({"kind": "event", "id": ev.id, "title": ev.title, "n_articles": ev.n_articles, "n_sources": ev.n_sources, "has_primary": bool(ev.has_primary),
                    "first_at": kg.iso(ev.first_at), "last_at": kg.iso(ev.last_at), "published_at": kg.iso(ev.first_at), "sensitivity": "public", "score": round(sc, 4),
                    "match": {"via": via, "terms": hit}, "position": {"field": "title", "start": f[0], "end": f[1], "term": f[2]} if f else {}})
    return out


def seeds(db: Session, q: str, *, kinds: list[str] | None = None, doc_kinds: list[str] | None = None, scopes: list[str], since: datetime | None = None,
          until: datetime | None = None, limit: int = 20, budget: Budget) -> dict:
    """問いの文から種を探す（文書・物・イベント・主張）。{"terms", "items": [種類つきの候補（点数の大きい順）], "counts", "truncated", "reason"}。limit は種類ごとの上限。"""
    want = [k for k in (kinds or SEED_KINDS) if k in SEED_KINDS]
    terms = query_terms(q)
    items: list[dict] = []
    ents: list[dict] = []
    if "entity" in want or "event" in want:
        ents = _entity_seeds(db, q, terms, scopes=scopes, since=since, until=until, limit=limit, budget=budget)
    if "document" in want:
        items += _doc_seeds(db, q, terms, scopes=scopes, doc_kinds=doc_kinds or [], since=since, until=until, limit=limit, budget=budget)
    if "entity" in want:
        items += ents
    if "claim" in want:
        items += _claim_seeds(db, terms, scopes=scopes, since=since, until=until, limit=limit, budget=budget)
    if "event" in want and "public" in scopes:
        items += _event_seeds(db, terms, ents, since=since, until=until, limit=limit, budget=budget)
    items.sort(key=lambda r: (-r["score"], r["kind"], r["id"] or 0))
    rank: dict[str, int] = {}
    for it in items:
        rank[it["kind"]] = rank.get(it["kind"], 0) + 1
        it["rank"] = rank[it["kind"]]
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in SEED_KINDS}
    return {"query": q, "terms": terms, "items": items, "counts": counts, "truncated": budget.hit, "reason": "time" if budget.hit else "",
            "unsearched_short_terms": [t for t in terms if not t.isascii() and len(t) < 3 and (_split(terms)[0] or _split(terms)[1])]}


# ---- 周り（neighbors）の点数 ----

NEIGHBOR_DECAY = 0.95      # 周りの点数は、辿ってきた元（種）の点数のこの割合が上限（< 1 なので、周りは元の種より必ず低い）
NEIGHBOR_QUERY_MISS = 0.2      # 問いの語に合わない相手の係数（合う相手は 1.0）
NEIGHBOR_EVIDENCE_FULL = 3      # 根拠がこの数あれば根拠の係数は最大（根拠 0 件は 0.5）


REL_CUES = {      # 辺の型 → 問いの文にあれば「その関係を尋ねている」語（一部・実装・対応…）。含まれる語で足りる（形態素解析なし）
    "uses": ("使っ", "使う", "使用", "利用", " uses", " use "), "supports": ("対応", "サポート", "support"), "replaces": ("置き換", "代替", "replace"),
    "competes": ("競合", "競う", "比べ", "比較", "違い", "vs", "compare"), "part_of": ("一部", "含む", "含まれ", "part of"), "implements": ("実装", "implement"),
    "cites": ("引用", "出典", "cite", "cites"), "contradicts": ("矛盾", "反論", "食い違", "contradict")}


def score_neighbors(out: dict, seed_ids: list[int], seed_scores: list[float], q: str) -> dict:
    """kg_view.neighbors の結果の辺と物に score を足す（その場で書き換えて返す）。
    辺の score = 元の点数（入り口の種の点数。2 段目以降は辿ってきた物の score）× NEIGHBOR_DECAY × 関係の確かさ（confidence。下限 0.2）× 根拠の係数
    × 問いの係数。根拠の係数 = 0.5 + 0.5 × min(1, 根拠の数 / NEIGHBOR_EVIDENCE_FULL)。問いの係数 = 相手の名前が問いの語に合う、または辺の型を尋ねる語（REL_CUES。「一部」「実装」など）が問いにあれば 1.0・合わなければ NEIGHBOR_QUERY_MISS。
    物の score は、その物へ来る辺の score の最大。どの係数も 1 以下で NEIGHBOR_DECAY < 1 なので、周りの点数は必ず元の種より低い（周りが種を押しのけて上位を占めない）。
    ハブの抑制は kg_view.neighbors の hub_k（物 1 つにつき読む辺の数）が既に効いている。種の点数は seed_scores（seed_ids と同じ並び。足りなければ 0.5）。"""
    base = {int(i): float(seed_scores[n]) if n < len(seed_scores) else 0.5 for n, i in enumerate(seed_ids)}
    qn = unicodedata.normalize("NFKC", q or "").casefold()
    terms = [t.casefold() for t in query_terms(q)]
    names = {n["id"]: n["name"] for lv in out["levels"] for n in lv["nodes"]} | {n["id"]: n["name"] for n in out["seeds"]}
    score = dict(base)
    for lv in out["levels"]:
        for e in lv["edges"]:
            parent = e.get("via")
            other = e["target"] if e["source"] == parent else e["source"]
            nm = unicodedata.normalize("NFKC", names.get(other, "")).casefold()
            hit = (bool(nm) and (nm in qn or any(t in nm for t in terms))) or any(c in qn for c in REL_CUES.get(e.get("type"), ()))
            w = (0.6 + 0.4 * float(e.get("confidence") or 0)) * (0.7 + 0.3 * min(1.0, int(e.get("evidence_count") or 0) / NEIGHBOR_EVIDENCE_FULL)) * (1.0 if hit else NEIGHBOR_QUERY_MISS)
            e["score"] = round(score.get(parent, 0.5) * NEIGHBOR_DECAY * w, 4)
            if other not in base and e["score"] > score.get(other, 0.0):
                score[other] = e["score"]
        for n in lv["nodes"]:
            n["score"] = score.get(n["id"], 0.0)
    for n in out["seeds"]:
        n["score"] = base.get(n["id"], 0.0)
    return out


# ---- 会合（meet）と条件つきの深さ（2026-11-25。測定: 評価の置き場の「深さと会合とカードの形」） ----
# 深さだけを上げても点数は動かない（深い段の点は減衰で必ず下になる）。上位の種から**別々に**展開し、2 つ以上の種から届いた物・辺へ加点（会合）して初めて深さが効く。
# 既定の深さは 1（普通の問いは深くすると雑音が増える）。問いが 2 つ以上の物を結ぶときだけ、最大 MEET_DEPTH_MAX まで。

MEET_SEEDS = 3        # 別々に展開する種の物の数（上位）
MEET_BONUS = 0.3      # 2 つ以上の種から届いた物・辺へ足す点数（上限 0.99）
MEET_DEPTH_MAX = 5
MEET_LIMIT = 60       # 種 1 つの展開が読む辺の上限（段をまたいだ合計）
MEET_HUB_K = 10       # 物 1 つにつき読む辺の数（ハブの抑制）
LEGACY_LIMIT = 40     # 普通の問い（展開しない）の、辺の上限（今までの /graph/neighbors の使い方と同じ）
MEET_NODES_MAX, MEET_EDGES_MAX = 80, 120      # 返す候補の上限（点数の大きい順）
MEET_EXTRA = 1        # 最初の会合の段に足す段数（測定で決めた値。0＝会合した段で止める）
CONNECT_CUES = ("関係", "つなが", "繋が", "つなぐ", "繋ぐ", "経由", "橋渡し", "間の", "between", "connect", "relation", "relate", "via ", "through", "link between")      # 高レベル（主題・関係）の語: 5 段まで
COMPARE_CUES = ("違い", "比べ", "比較", "対比", "どちらが", " vs", "versus", "compare", "difference", "変わった", "変化")      # 2 段まで（比較・変化は 2 段が最良）
DEPTH_CAP_CONNECT, DEPTH_CAP_COMPARE = 5, 2


def _fold_q(q: str) -> str:
    return unicodedata.normalize("NFKC", q or "").casefold()


def depth_plan(q: str, named: int, n_seeds: int) -> dict:
    """問いの種類から、展開する深さの上限を決める（規則だけ。LLM なし）。{"cap": 1〜5, "kind": "default|compare|connect", "named": 問いに名前が出た種の物の数, "cues": 当たった語}。
    種の物が 2 つ未満なら会合が起きないので常に 1（深さは無益）。高レベル（関係・経由・経緯・between など）の語があれば 5、
    比較・変化の語（並べただけの「A と B」は深くしない。複数の文書の問いが下がるため）なら 2、それ以外は 1（既定）。誤判定は既定の深さ 1 に戻るだけ（安全側）。"""
    f = _fold_q(q)
    if n_seeds < 2:
        return {"cap": 1, "kind": "default", "named": named, "cues": []}
    connect = [c for c in CONNECT_CUES if c in f]
    if connect:
        return {"cap": DEPTH_CAP_CONNECT, "kind": "connect", "named": named, "cues": connect}
    compare = [c for c in COMPARE_CUES if c in f]
    if compare:
        return {"cap": DEPTH_CAP_COMPARE, "kind": "compare", "named": named, "cues": compare}
    return {"cap": 1, "kind": "default", "named": named, "cues": []}


def expand_meet(db: Session, ids: list[int], seed_scores: list[float], q: str, *, scopes: list[str], depth: int | None = None, meet: bool = True,
                min_confidence: float = 0.0, budget: Budget) -> dict:
    """上位 MEET_SEEDS の種の物から**別々に**展開（kg_view.neighbors を種ごと）し、2 つ以上の種から届いた物・辺へ MEET_BONUS を足して 1 つの候補にする。
    depth=None（自動）: depth_plan の上限まで展開して、種の間の最初の会合の段（＝距離の見込みの半分）で止める（会合が無ければ上限まで）。数字ならその段に固定。
    点数は score_neighbors と同じ（元の種より必ず低い）に、会合の加点（上限 0.99）。種自身も他の種から届けば会合の物。
    時間（budget）・候補の上限を守る（深い段は期限で止まる）。返り値: {"plan", "depth", "seeds", "nodes", "edges", "meet": {"nodes", "edges"}, "truncated", "reason"}。"""
    ids = list(dict.fromkeys(int(i) for i in ids))[:MEET_SEEDS]
    scores = {i: (float(seed_scores[n]) if n < len(seed_scores) else 0.5) for n, i in enumerate(ids)}
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(ids), Entity.sensitivity.in_(scopes)))} if ids else {}
    ids = [i for i in ids if i in ents]
    f = _fold_q(q)
    named = sum(1 for i in ids if len(nm := _fold_q(ents[i].name)) >= 2 and nm in f)
    plan = depth_plan(q, named, len(ids))
    cap = plan["cap"] if depth is None else max(1, min(int(depth), MEET_DEPTH_MAX))
    if depth is None and plan["kind"] == "default":      # 普通の問い: 今までどおり（まとめて 1 段・加点なし）。測定で、別々に展開すると nDCG が下がる問いがあったため
        out = kg_view.neighbors(db, ids, depth=1, limit=LEGACY_LIMIT, scopes=scopes, min_confidence=min_confidence, order="weight", hub_k=MEET_HUB_K, deadline=budget.deadline)
        score_neighbors(out, ids, [scores[i] for i in ids], q)
        lv = [x for lv in out["levels"] for x in (lv["nodes"], lv["edges"])]
        nodes = [{**n, "depth": 1, "seeds": [], "meet": False} for n in (lv[0] if lv else [])]
        edges = [{**e, "depth": 1, "seeds": [], "meet": False} for e in (lv[1] if lv else [])]
        nodes.sort(key=lambda x: (-x["score"], x["id"]))
        edges.sort(key=lambda x: (-x["score"], x["id"]))
        return {"plan": plan, "depth": 1, "seeds": ids, "nodes": nodes[:MEET_NODES_MAX], "edges": edges[:MEET_EDGES_MAX], "meet": {"nodes": 0, "edges": 0},
                "truncated": bool(out["truncated"]) or budget.expired(), "reason": "time" if budget.hit else ""}
    per: dict[int, dict] = {}
    truncated = False
    for sid in ids:
        out = kg_view.neighbors(db, [sid], depth=cap, limit=MEET_LIMIT, scopes=scopes, min_confidence=min_confidence, order="weight", hub_k=MEET_HUB_K, deadline=budget.deadline)
        score_neighbors(out, [sid], [scores[sid]], q)
        truncated = truncated or bool(out["truncated"])
        per[sid] = out
    first_seen: dict[int, list[int]] = {}      # 物 → それに届いた種ごとの段（種自身は 0）
    for sid, out in per.items():
        first_seen.setdefault(sid, []).append(0)
        for lv in out["levels"]:
            for n in lv["nodes"]:
                first_seen.setdefault(n["id"], []).append(lv["depth"])
    meeting = [sorted(ds)[1] for ds in first_seen.values() if len(ds) >= 2]      # 2 つ目の種が届いた段＝会合の成り立つ段
    used = cap
    if depth is None and meeting:
        used = min(cap, max(1, min(meeting)) + MEET_EXTRA)
    node_score: dict[int, float] = {}
    node_info: dict[int, dict] = {}
    node_seeds: dict[int, set[int]] = {}
    edge_score: dict[int, float] = {}
    edge_info: dict[int, dict] = {}
    edge_seeds: dict[int, set[int]] = {}
    for sid, out in per.items():
        for lv in out["levels"]:
            if lv["depth"] > used:
                continue
            for n in lv["nodes"]:
                node_seeds.setdefault(n["id"], set()).add(sid)
                if n.get("score", 0.0) > node_score.get(n["id"], -1.0):
                    node_score[n["id"]], node_info[n["id"]] = n.get("score", 0.0), {**n, "depth": lv["depth"]}
            for e in lv["edges"]:
                edge_seeds.setdefault(e["id"], set()).add(sid)
                if e.get("score", 0.0) > edge_score.get(e["id"], -1.0):
                    edge_score[e["id"]], edge_info[e["id"]] = e.get("score", 0.0), {**e, "depth": lv["depth"]}
    for sid in ids:      # 種自身が他の種から届いたときは、種自身も届いた 1 つに数える（会合の物）
        if sid in node_seeds:
            node_seeds[sid].add(sid)
    meets_n = meets_e = 0
    if meet and len(ids) >= 2:
        for nid, ss in node_seeds.items():
            if len(ss) >= 2:
                node_score[nid] = min(0.99, node_score[nid] + MEET_BONUS)
                meets_n += 1
        for eid, ss in edge_seeds.items():
            if len(ss) >= 2:
                edge_score[eid] = min(0.99, edge_score[eid] + MEET_BONUS)
                meets_e += 1
    nodes = [{**node_info[n], "score": round(node_score[n], 4), "seeds": sorted(node_seeds[n]), "meet": len(node_seeds[n]) >= 2} for n in node_info]
    edges = [{**edge_info[e], "score": round(edge_score[e], 4), "seeds": sorted(edge_seeds[e]), "meet": len(edge_seeds[e]) >= 2} for e in edge_info]
    nodes.sort(key=lambda x: (-x["score"], x["id"]))
    edges.sort(key=lambda x: (-x["score"], x["id"]))
    cut = len(nodes) > MEET_NODES_MAX or len(edges) > MEET_EDGES_MAX
    return {"plan": plan, "depth": used, "seeds": ids, "nodes": nodes[:MEET_NODES_MAX], "edges": edges[:MEET_EDGES_MAX], "meet": {"nodes": meets_n, "edges": meets_e},
            "truncated": truncated or cut or budget.expired(), "reason": "time" if budget.hit else ""}


# ---- 局所 Personalized PageRank（push 法） ----

PPR_NODES_MAX, PPR_TOP_MAX, PPR_SEEDS_MAX = 20000, 500, 50


def ppr(db: Session, seeds_: dict[int, float], *, scopes: list[str], alpha: float = 0.15, eps: float = 1e-4, max_nodes: int = 2000, top: int = 50,
        types: list[str] | None = None, direction: str = "both", type_directions: dict[str, str] | None = None, min_confidence: float = 0.0, weight: str = "confidence",
        hub_k: int | None = 100, since: datetime | None = None, until: datetime | None = None, include_seeds: bool = False, budget: Budget) -> dict:
    """種の集合から**局所**の Personalized PageRank（Andersen–Chung–Lang の push 法）。全体の再計算はしない: 触るのは種の近くの物だけ。
    p[u] += α·r[u]、残り (1−α)·r[u] を辺の重みに比例して隣へ配る。r[u] > ε·(重みの合計 d(u)) の物がある間だけ繰り返す（誤差は ε で抑えられる）。
    辺の重み: confidence（既定。下限 0.05）か uniform（1）。向きは direction（both は無向として扱う）。辺の無い物は残りを自分に戻す（質量は保存）。
    物の辺は hub_k 本まで（重みの高い順。ハブの抑制。d(u) は読んだ辺の重みの合計）。時間（budget）・触った物の数（max_nodes）で止めたら truncated と理由。
    戻り: {"items": [{id, name, type, score, via, degree}（score の大きい順・種は既定で除く）], "stats": {pushes, rounds, touched, residual_max, residual_total, epsilon, alpha}}。"""
    seeds_ = {k: v for k, v in seeds_.items() if v > 0}
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(list(seeds_)), Entity.sensitivity.in_(scopes)))} if seeds_ else {}
    seeds_ = {k: v for k, v in seeds_.items() if k in ents}
    total = sum(seeds_.values()) or 1.0
    r: dict[int, float] = {k: v / total for k, v in seeds_.items()}
    p: dict[int, float] = {}
    via: dict[int, tuple[int, float]] = {}      # 物 -> (いちばん多く質量を送った物, その量)
    adj: dict[int, list[tuple[int, float]]] = {}      # 読んだ辺（物 -> [(隣, 重み)]）
    wdeg: dict[int, float] = {}
    pushes = rounds = 0
    reason = ""

    def w_of(conf: float) -> float:
        return 1.0 if weight == "uniform" else max(0.05, float(conf or 0))

    def load(ids: list[int]) -> None:
        got = kg_view.adjacency(db, ids, scopes=scopes, k=hub_k, types=types, direction=direction, type_directions=type_directions, min_confidence=min_confidence, since=since,
                                until=until)
        for owner, lst in got.items():
            adj[owner] = [(o, w_of(c)) for o, c in lst if o != owner]
        for i in ids:
            wdeg[i] = sum(w for _, w in adj[i])

    hidden: set[int] = set()      # 区分の範囲の外の物（質量を送らない）
    while True:
        active = [u for u, x in r.items() if x > eps * max(wdeg.get(u, 1.0), 1e-9)]      # 辺をまだ読んでいない物は次数 1 とみなす（読んでから数え直す）
        if not active:
            break
        if budget.expired():
            reason = "time"
            break
        if len(set(r) | set(p)) > max_nodes:
            reason = "max_nodes"
            break
        need = [u for u in active if u not in adj]
        if need:
            load(need)
            # 隣が区分の範囲の物かを一度に確かめる
            nb = {o for u in need for o, _ in adj[u]} - set(ents) - hidden
            if nb:
                ok = {i for (i,) in db.execute(select(Entity.id).where(Entity.id.in_(list(nb)), Entity.sensitivity.in_(scopes)))}
                hidden |= nb - ok
                ents.update({i: None for i in ok})
            for u in need:
                adj[u] = [(o, w) for o, w in adj[u] if o not in hidden]
                wdeg[u] = sum(w for _, w in adj[u])
        rounds += 1
        for u in active:
            x = r.pop(u, 0.0)
            if x <= 0:
                continue
            pushes += 1
            nbrs = adj.get(u) or []
            d = wdeg.get(u, 0.0)
            if not nbrs or d <= 0:
                p[u] = p.get(u, 0.0) + x      # 行き先が無い: 質量は自分に残す
                continue
            p[u] = p.get(u, 0.0) + alpha * x
            send = (1 - alpha) * x
            for o, w in nbrs:
                m = send * w / d
                r[o] = r.get(o, 0.0) + m
                if o not in via or via[o][1] < m:
                    via[o] = (u, m)
        if pushes > 2_000_000:
            reason = "pushes"
            break
    scored = [(v, u) for u, v in p.items() if include_seeds or u not in seeds_]
    scored.sort(key=lambda t: (-t[0], t[1]))
    pick = [u for _, u in scored[:max(1, min(top, PPR_TOP_MAX))]]
    names = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(pick)))} if pick else {}
    deg = kg_view.degrees(db, pick, scopes) if pick else {}
    items = [{"id": u, "name": names[u].name, "type": names[u].type, "score": round(p[u], 6), "via": via.get(u, (None, 0))[0], "degree": deg.get(u, (0, ""))[0],
              "last_edge_at": deg.get(u, (0, ""))[1], "is_seed": u in seeds_} for u in pick if u in names]
    return {"seeds": [{"id": k, "weight": round(v / total, 4)} for k, v in sorted(seeds_.items())], "items": items, "truncated": bool(reason), "reason": reason,
            "stats": {"pushes": pushes, "rounds": rounds, "touched": len(set(p) | set(r)), "residual_total": round(sum(r.values()), 6), "residual_max": round(max(r.values(), default=0.0), 6),
                      "epsilon": eps, "alpha": alpha, "mass_p": round(sum(p.values()), 6)}}


# ---- 根拠のまとめ取り ----

EVIDENCE_IDS_MAX, EVIDENCE_PER_ITEM_MAX, EVIDENCE_TOTAL_MAX = 200, 20, 1500


def _ev_dict(ev: Evidence, d: Document, compact: bool = False) -> dict:
    """根拠 1 件。document.label は「出どころ 日付 d<id>」（LLM に渡す短い印）。compact なら url を省く（Google ニュースの転送 URL が文脈のトークンの大半を占めるため。url は id から取れる）。"""
    doc = {"id": d.id, "kind": d.kind, "title": d.title, "url": d.url, "source": d.source, "published_at": kg.iso(d.published_at), "label": things.doc_label(d.source, kg.iso(d.published_at), d.id),
           "sensitivity": d.sensitivity, "ai_use": d.ai_use, "version": d.version}
    if compact:
        doc.pop("url")
    return {"id": ev.id, "quote": ev.quote, "start": ev.start, "end": ev.end, "page": ev.page, "quote_hash": ev.quote_hash, "grounded": bool(ev.grounded),
            "confidence": round(float(ev.confidence or 0), 3), "document": doc}


def evidence(db: Session, *, entities: list[int], claims: list[int], relationships: list[int], scopes: list[str], per_item: int = 5, compact: bool = False) -> dict:
    """物・主張・関係の id の一覧から、根拠（引用・位置・文書の題・URL・日時・区分）をまとめて返す（id ごと 1 回の問い合わせにしない: 種類ごとに IN で）。
    - 主張・関係: その根拠（evidences）を確かさの高い順に per_item 件まで。
    - 物: その物の主張（区分の範囲の中）の根拠を、新しい主張から per_item 件まで。
    区分の範囲の外の主張・関係・文書、および存在しない id は、同じく missing に入れる（存在を区別して漏らさない）。"""
    per_item = max(1, min(per_item, EVIDENCE_PER_ITEM_MAX))
    out: dict = {"claims": {}, "relationships": {}, "entities": {}, "missing": {"claims": [], "relationships": [], "entities": []}, "truncated": False}
    budget_left = EVIDENCE_TOTAL_MAX

    def collect(col, ids: list[int]) -> dict[int, list[dict]]:
        nonlocal budget_left
        res: dict[int, list[dict]] = {}
        if not ids:
            return res
        rows = db.execute(select(Evidence, Document).join(Document, Document.id == Evidence.document_id)
                          .where(col.in_(ids), Document.sensitivity.in_(scopes)).order_by(Evidence.confidence.desc(), Evidence.grounded.desc(), Evidence.id))
        for ev, d in rows:
            key = getattr(ev, col.key)
            lst = res.setdefault(key, [])
            if len(lst) < per_item and budget_left > 0:
                lst.append(_ev_dict(ev, d, compact))
                budget_left -= 1
            elif len(lst) < per_item:
                out["truncated"] = True
        return res

    cl_ids = list(dict.fromkeys(claims))[:EVIDENCE_IDS_MAX]
    visible_claims = {c.id: c for c in db.scalars(select(Claim).where(Claim.id.in_(cl_ids), Claim.sensitivity.in_(scopes)))} if cl_ids else {}
    got = collect(Evidence.claim_id, list(visible_claims))
    for i in cl_ids:
        c = visible_claims.get(i)
        if c is None:
            out["missing"]["claims"].append(i)
        else:
            out["claims"][str(i)] = {"kind": c.kind, "text": c.text, "value": c.value, "entity_id": c.entity_id, "ai_extracted": bool(c.ai_extracted), "sensitivity": c.sensitivity,
                                     "evidences": got.get(i, [])}
    rl_ids = list(dict.fromkeys(relationships))[:EVIDENCE_IDS_MAX]
    visible_rels = {x.id: x for x in db.scalars(select(Relationship).where(Relationship.id.in_(rl_ids), Relationship.sensitivity.in_(scopes)))} if rl_ids else {}
    got = collect(Evidence.relationship_id, list(visible_rels))
    for i in rl_ids:
        x = visible_rels.get(i)
        if x is None:
            out["missing"]["relationships"].append(i)
        else:
            out["relationships"][str(i)] = {"type": x.type, "source": x.subject_id, "target": x.object_id, "status": x.status, "confidence": round(float(x.confidence or 0), 3),
                                            "sensitivity": x.sensitivity, "evidences": got.get(i, [])}
    en_ids = list(dict.fromkeys(entities))[:EVIDENCE_IDS_MAX]
    vis_ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(en_ids), Entity.sensitivity.in_(scopes)))} if en_ids else {}
    by_ent: dict[int, list[Claim]] = {}
    if vis_ents:
        for c in db.scalars(select(Claim).where(Claim.entity_id.in_(list(vis_ents)), Claim.sensitivity.in_(scopes)).order_by(Claim.id.desc()).limit(EVIDENCE_IDS_MAX * per_item)):
            if len(by_ent.setdefault(c.entity_id, [])) < per_item:
                by_ent[c.entity_id].append(c)
    allc = [c for lst in by_ent.values() for c in lst]
    got = collect(Evidence.claim_id, [c.id for c in allc])
    for i in en_ids:
        e = vis_ents.get(i)
        if e is None:
            out["missing"]["entities"].append(i)
            continue
        items = []
        for c in by_ent.get(i, []):
            items.append({"claim_id": c.id, "kind": c.kind, "text": c.text, "value": c.value, "sensitivity": c.sensitivity, "evidences": got.get(c.id, [])})
        out["entities"][str(i)] = {"name": e.name, "type": e.type, "claims": items}
    return out


# ---- 調査の記録 ----

def _grams(text: str) -> set[str]:
    t = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text).casefold())
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


def research(db: Session, topic: str, *, scopes: list[str], limit: int = 10, min_similarity: float = 0.35, full: bool = False) -> dict:
    """調査の記録（POST /research が保存したもの）を、テーマで読む。同じテーマ（正規化して一致・包含）と、類似のテーマ（2 文字の組の Jaccard が min_similarity 以上）を
    新しい順に返す。last_at は同じテーマの最後の記録の日時（差分調査の「前回」）。full でなければ結論は 600 文字まで。"""
    topic = (topic or "").strip()
    rows = db.execute(select(ResearchRecord.id, ResearchRecord.theme, ResearchRecord.created_at).where(ResearchRecord.sensitivity.in_(scopes))
                      .order_by(ResearchRecord.id.desc()).limit(5000)).all()
    q = _grams(topic)
    norm = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", topic).casefold())
    exact, similar = [], []
    for rid, theme, at in rows:
        tn = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", theme).casefold())
        g = _grams(theme)
        sim = len(q & g) / len(q | g) if q and g else 0.0
        if norm and (tn == norm or (min(len(tn), len(norm)) >= 4 and (norm in tn or tn in norm))):
            exact.append((max(sim, 0.5), rid))
        elif sim >= min_similarity:
            similar.append((sim, rid))
    exact.sort(key=lambda x: -x[1])
    similar.sort(key=lambda x: (-x[0], -x[1]))
    pick = {rid: s for s, rid in exact[:limit]} | {rid: s for s, rid in similar[:limit]}
    recs = {x.id: x for x in db.scalars(select(ResearchRecord).where(ResearchRecord.id.in_(list(pick))))} if pick else {}

    def shape(rid: int, sim: float) -> dict:
        x = recs[rid]
        return {"id": x.id, "theme": x.theme, "similarity": round(sim, 3), "created_at": kg.iso(x.created_at), "sensitivity": x.sensitivity,
                "conclusion": x.conclusion if full else x.conclusion[:600], "facts": json.loads(x.facts or "[]"), "sources": json.loads(x.sources or "[]")}

    ex = [shape(rid, s) for s, rid in exact[:limit] if rid in recs]
    si = [shape(rid, s) for s, rid in similar[:limit] if rid in recs]
    last = max([r["created_at"] for r in ex], default="")
    return {"topic": topic, "exact": ex, "similar": si, "last_at": last, "previous": ex[0] if ex else None,
            "last_any_at": max([r["created_at"] for r in ex + si], default=""), "scanned": len(rows)}
