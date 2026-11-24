"""イベントへの粗いまとめ（ARCHITECTURE 4 章）: 48 時間以内の記事を、同じ出来事ごとにまとめる。

- **イベント**は 2 件以上の記事のつながり（連結成分）。1 件だけの記事は行にしない。表は events・event_articles（db.py）。
- 2 つの記事（公開の時刻が 48 時間以内）を「同じ出来事」とみなす条件（どれか 1 つ。`link()`。しきい値の根拠は docs/DESIGN.md 11 章）:
  1. 近い重複: 取り込みで付いた dup_of、または題＋抜粋の SimHash のハミング距離が HAMMING 以下（別の取得先どうし）
  2. 題の語の重なり（Jaccard）: 同じ言語どうしで、題の語（英数字の語・カタカナ・漢字の 2 文字の組）の Jaccard が JACCARD 以上、重なりが JACCARD_SHARED 語以上
  3. 言語をまたぐ: 片方だけが日本語の題のとき、英数字の語の重なりが CROSS_SHARED 語以上で、少ない方の語の CROSS_COVER 以上（「GPT-6.2」「OpenAI」のような名前は言語に依らない）
  4. 取り出した物（article_entities）の重なり: 種類が model/software/product/game/policy（または名前に数字を含む）の物は重み 1・それ以外は 0.5。
     重みの合計が ENTITY_SUM 以上、または ENTITY_SUM_WITH_TITLE 以上で題の語も TITLE_SHARED 語以上重なる
- イベントの大きさの上限は MAX_EVENT（つなぎすぎて 1 つの巨大なイベントにしない）。上限を超えるつなぎは捨てる。
- **増分**: Write Coordinator のまとめ書きの 1 トランザクションの中で、新しく保存した記事と、物の取り出しが保存された記事（`update`）だけを、前後 48 時間の記事と見比べる。
  **作り直し**（`rebuild`）は、最近 KEEP_DAYS 日の記事を id の小さい順に全部見比べる。関係は 2 記事だけで決まる（記事の数・物の数に依らない）ので、
  同じ入力なら増分と作り直しの分け方は同じ（イベントの id・要約は別。要約は作り直しでも引き継ぐ）。
- 読み（API）はここの関数を使う。書き込みは writer のまとめ書きだけ（`update`・`rebuild_in`・`apply_summaries`・`cleanup`）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, not_, select
from sqlalchemy.orm import Session

from qnapnewshub import sources as sources_mod
from qnapnewshub.db import ArticleEntity, Entity, Event, EventArticle, NewsItem

_UTC = timezone.utc
WINDOW_HOURS = 48
KEEP_DAYS = 30
MAX_EVENT = 60
HAMMING = 3
JACCARD = 0.5
JACCARD_SHARED = 2
CROSS_SHARED = 2
CROSS_COVER = 0.6
ENTITY_SUM = 1.5
ENTITY_SUM_WITH_TITLE = 1.0
TITLE_SHARED = 3
POSTING_CAP = 150          # 前後 48 時間の中で、これより多くの記事に出る語・物は、つなぐ手がかりにしない（「AI」「OpenAI」のような一般的な語）
STRONG_TYPES = frozenset({"model", "software", "product", "game", "policy"})
TOP_ENTITIES = 5
SUMMARY_MIN_ARTICLES = 3
SUMMARY_MIN_SOURCES = 2
SUMMARY_MAX_TRIES = 3

_LATIN = re.compile(r"[a-z0-9]+(?:[.+#_-][a-z0-9]+)*")
_CJK_RUN = re.compile(r"[぀-ヿ㐀-鿿ー]+")
_KANJI_KATA = re.compile(r"[ァ-ヴー㐀-鿿]+")
_HAS_CJK = re.compile(r"[぀-ヿ㐀-鿿]")


def _naive(value: datetime) -> datetime:
    return (value.astimezone(_UTC) if value.tzinfo else value).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return _naive(value).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- 題の語 ----

def _stop() -> frozenset:
    from qnapnewshub.analytics import _STOP_EN

    return _STOP_EN


def title_tokens(title: str) -> tuple[frozenset, frozenset, bool]:
    """(題の語の全部, そのうち英数字の語, 日本語の文字を含むか)。英数字の語は記号を除いた形（"GPT-6.2" → "gpt62" と "gpt"）。
    日本語は、漢字・カタカナの連なりの 2 文字の組と、3 文字以上のカタカナの連なり全体（ひらがなは助詞が多いので使わない）。"""
    text = unicodedata.normalize("NFKC", title or "").lower()
    stop = _stop()
    latin: set[str] = set()
    for m in _LATIN.finditer(text):
        raw = m.group(0)
        if raw in stop:
            continue
        whole = re.sub(r"[^a-z0-9]", "", raw)
        if len(whole) >= 3 or (len(whole) == 2 and not whole.isdigit()):
            latin.add(whole)
        for part in re.split(r"[.+#_-]", raw):
            if len(part) >= 3 and not part.isdigit() and part not in stop:
                latin.add(part)
    for m in re.finditer(r"([a-z]{2,})[ -]?(\d{1,3}(?:\.\d+)*)(?![a-z0-9])", text):      # 「qwen 4」「iphone 18」「GPT 6.2」のように語と版が離れた書き方も 1 つの語にする
        if m.group(1) not in stop:
            latin.add(m.group(1) + re.sub(r"[^0-9]", "", m.group(2)))
    cjk: set[str] = set()
    for run in _KANJI_KATA.findall(text):
        for i in range(len(run) - 1):
            cjk.add(run[i:i + 2])
        if re.fullmatch(r"[ァ-ヴー]{3,}", run):
            cjk.add(run)
    return frozenset(latin | cjk), frozenset(latin), bool(_HAS_CJK.search(text))


@dataclass
class _Art:
    id: int
    title: str
    ts: float
    source_key: str
    kind: str
    dup_of: int | None
    simhash: int | None
    toks: frozenset
    latin: frozenset
    cjk: bool
    ents: dict[int, float] = field(default_factory=dict)


def link(a: _Art, b: _Art) -> str:
    """2 つの記事が同じ出来事か。つながる理由（"dup"・"simhash"・"title"・"cross"・"entity"）か、空文字。a・b を入れ替えても同じ。"""
    if abs(a.ts - b.ts) > WINDOW_HOURS * 3600:
        return ""
    if a.dup_of == b.id or b.dup_of == a.id or (a.dup_of is not None and a.dup_of == b.dup_of):
        return "dup"
    if a.simhash is not None and b.simhash is not None and a.source_key != b.source_key and ((a.simhash ^ b.simhash) & 0xFFFFFFFFFFFFFFFF).bit_count() <= HAMMING:
        return "simhash"
    shared = a.toks & b.toks
    union = len(a.toks | b.toks)
    if a.cjk == b.cjk:
        if len(shared) >= JACCARD_SHARED and union and len(shared) / union >= JACCARD:
            return "title"
    else:
        ls = a.latin & b.latin
        small = min(len(a.latin), len(b.latin))
        if small and len(ls) / small >= CROSS_COVER and (len(ls) >= CROSS_SHARED + 1 or (len(ls) >= CROSS_SHARED and len(ls) == small and small >= 3)):
            return "cross"
    common = a.ents.keys() & b.ents.keys()
    if common:
        total = sum(min(a.ents[i], b.ents[i]) for i in common)
        if total >= ENTITY_SUM or (total >= ENTITY_SUM_WITH_TITLE and len(shared) >= TITLE_SHARED):
            return "entity"
    return ""


def entity_weight(kind: str, key: str) -> float:
    return 1.0 if kind in STRONG_TYPES or re.search(r"\d", key or "") else 0.5


# ---- 読み込みと索引 ----

def _eligible():
    """イベントにする記事: 出来事（利用者宛て）でなく、全員宛てで、隠すソースでない。集計（analytics）と同じ。"""
    conds = [NewsItem.kind != "event", NewsItem.user_id.is_(None)]
    for p in sources_mod.HIDDEN_SOURCE_PREFIXES:
        escaped = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(not_(NewsItem.source_key.like(f"{escaped}%", escape="\\")))
    return and_(*conds)


def _load(db: Session, since: datetime, until: datetime, ids: set[int] | None = None) -> dict[int, _Art]:
    """公開の時刻が [since, until] の記事（と、ids に指定した記事）を、物つきで読む。"""
    cond = (NewsItem.published_at >= _naive(since)) & (NewsItem.published_at <= _naive(until))
    q = select(NewsItem.id, NewsItem.title, NewsItem.published_at, NewsItem.source_key, NewsItem.kind, NewsItem.dup_of, NewsItem.simhash).where(_eligible(), cond)
    rows = list(db.execute(q))
    out: dict[int, _Art] = {}
    for i, title, pub, src, kind, dup, sh in rows:
        toks, latin, cjk = title_tokens(title)
        out[i] = _Art(i, title, _naive(pub).replace(tzinfo=_UTC).timestamp(), src, kind, dup, sh, toks, latin, cjk)
    erows = db.execute(select(ArticleEntity.article_id, ArticleEntity.entity_id, Entity.type, Entity.key).join(Entity, Entity.id == ArticleEntity.entity_id)
                       .join(NewsItem, NewsItem.id == ArticleEntity.article_id).where(cond))
    for aid, eid, kind, key in erows:
        if aid in out:
            out[aid].ents[eid] = entity_weight(kind, key)
    return out


class _Index:
    """時刻順の転置索引（語・物・SimHash の 16 ビットずつ・dup_of）。前後 48 時間の範囲だけを二分探索で引く。"""

    def __init__(self, pool: dict[int, _Art]) -> None:
        self.pool = pool
        self.tok: dict[str, list] = defaultdict(list)
        self.ent: dict[int, list] = defaultdict(list)
        self.sim: dict[tuple[int, int], list] = defaultdict(list)
        self.dup: dict[int, list[int]] = defaultdict(list)
        for a in pool.values():
            for t in a.toks:
                self.tok[t].append((a.ts, a.id))
            for e in a.ents:
                self.ent[e].append((a.ts, a.id))
            if a.simhash is not None:
                v = a.simhash & 0xFFFFFFFFFFFFFFFF
                for part in range(4):
                    self.sim[(part, (v >> (16 * part)) & 0xFFFF)].append((a.ts, a.id))
            if a.dup_of is not None:
                self.dup[a.dup_of].append(a.id)
        for table in (self.tok, self.ent, self.sim):
            for rows in table.values():
                rows.sort()

    @staticmethod
    def _range(rows: list, ts: float) -> list[int]:
        lo, hi = ts - WINDOW_HOURS * 3600, ts + WINDOW_HOURS * 3600
        a = bisect_left(rows, (lo, -1))
        b = bisect_right(rows, (hi, 1 << 62))
        return [i for _, i in rows[a:b]] if b - a <= POSTING_CAP else []

    def candidates(self, x: _Art) -> set[int]:
        out: set[int] = set()
        if x.dup_of is not None and x.dup_of in self.pool:
            out.add(x.dup_of)
            out.update(self.dup.get(x.dup_of, ()))
        out.update(self.dup.get(x.id, ()))
        for t in x.toks:
            if t in self.tok:
                out.update(self._range(self.tok[t], x.ts))
        for e in x.ents:
            if e in self.ent:
                out.update(self._range(self.ent[e], x.ts))
        if x.simhash is not None:
            v = x.simhash & 0xFFFFFFFFFFFFFFFF
            for part in range(4):
                rows = self.sim.get((part, (v >> (16 * part)) & 0xFFFF))
                if rows:
                    out.update(self._range(rows, x.ts))
        out.discard(x.id)
        return out


def find_links(pool: dict[int, _Art], touched: set[int]) -> list[tuple[int, int, str]]:
    idx = _Index(pool)
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, str]] = []
    for xid in sorted(touched):
        x = pool.get(xid)
        if x is None:
            continue
        for yid in sorted(idx.candidates(x)):
            key = (min(xid, yid), max(xid, yid))
            if key in seen:
                continue
            seen.add(key)
            why = link(x, pool[yid])
            if why:
                out.append((key[0], key[1], why))
    out.sort()
    return out


# ---- つなぐ（連結成分） ----

class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}
        self.size: dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.size[x] = 1

    def find(self, x: int) -> int:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int, *, cap: int = 0) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if cap and self.size[ra] + self.size[rb] > cap:
            return False
        if ra > rb:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return out


def _chunks(items: list, n: int = 400):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _origin_of(res, kind: str, source_key: str) -> str:
    return res.origin(kind, source_key)


def _stats(db: Session, members: list[int], res) -> dict:
    """イベントの数字: 代表・記事の数・出どころの数・一次情報・初報と最終・物の上位。"""
    rows: list = []
    for part in _chunks(members):
        rows += list(db.execute(select(NewsItem.id, NewsItem.title, NewsItem.published_at, NewsItem.source_key, NewsItem.kind).where(NewsItem.id.in_(part))))
    rows = [r for r in rows]
    primary = [r for r in rows if _origin_of(res, r.kind, r.source_key) == "official"]
    rep = min(primary or rows, key=lambda r: (_naive(r.published_at), r.id))
    counts: Counter = Counter()
    for part in _chunks([r.id for r in rows]):
        counts.update(eid for (eid,) in db.execute(select(ArticleEntity.entity_id).where(ArticleEntity.article_id.in_(part))))
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_ENTITIES]
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_([i for i, _ in top])))} if top else {}
    return {"rep_id": rep.id, "title": rep.title[:400], "n_articles": len(rows), "n_sources": len({r.source_key for r in rows}), "has_primary": bool(primary),
            "first_at": min(_naive(r.published_at) for r in rows), "last_at": max(_naive(r.published_at) for r in rows),
            "entities": json.dumps([{"id": i, "name": ents[i].name, "type": ents[i].type, "n": n} for i, n in top if i in ents], ensure_ascii=False)}


def _merge(db: Session, links: list[tuple[int, int, str]], touched: set[int], res, carry: dict[int, dict] | None = None) -> dict:
    """つなぎを、いまあるイベントの上に重ねて、イベントを作る・合わせる。コミットは呼ぶ側。"""
    nodes = {i for a, b, _ in links for i in (a, b)} | touched
    ev_of: dict[int, int] = {}
    for part in _chunks(sorted(nodes)):
        ev_of.update(dict(db.execute(select(EventArticle.article_id, EventArticle.event_id).where(EventArticle.article_id.in_(part))).all()))
    involved = sorted(set(ev_of.values()))
    members: dict[int, list[int]] = defaultdict(list)
    for part in _chunks(involved):
        for aid, eid in db.execute(select(EventArticle.article_id, EventArticle.event_id).where(EventArticle.event_id.in_(part))):
            members[eid].append(aid)
    uf = _Union()
    for eid, ids in members.items():
        for other in ids[1:]:
            uf.union(ids[0], other)
        uf.add(ids[0])
    for a, b, _ in links:
        uf.union(a, b, cap=MAX_EVENT)
    prior_event: dict[int, int] = {}
    for eid, ids in members.items():
        for aid in ids:
            prior_event[aid] = eid
    made = merged = 0
    for root, ids in uf.groups().items():
        if len(ids) < 2 or not (set(ids) & (nodes)):
            continue
        old_ids = sorted({prior_event[i] for i in ids if i in prior_event})
        keep = old_ids[0] if old_ids else None
        stats = _stats(db, ids, res)
        summary = {}
        for eid in old_ids:
            ev = db.get(Event, eid)
            if ev is not None and ev.summary and not summary:
                summary = {"summary": ev.summary, "summary_model": ev.summary_model, "summary_at": ev.summary_at, "summary_tries": ev.summary_tries}
        if not summary and carry:
            for i in ids:
                if i in carry:
                    summary = carry[i]
                    break
        for part in _chunks(sorted(ids)):
            db.execute(delete(EventArticle).where(EventArticle.article_id.in_(part)))
        for eid in old_ids[1:]:
            db.execute(delete(EventArticle).where(EventArticle.event_id == eid))
            db.execute(delete(Event).where(Event.id == eid))
            merged += 1
        ev = db.get(Event, keep) if keep is not None else None
        if ev is None:
            ev = Event(**stats, **summary)
            db.add(ev)
            made += 1
        else:
            for k, v in {**stats, **({} if not summary else summary)}.items():
                setattr(ev, k, v)
        db.flush()
        db.add_all([EventArticle(article_id=i, event_id=ev.id) for i in sorted(ids)])
        db.flush()
    return {"made": made, "merged": merged, "links": len(links)}


# ---- 書く（まとめ書きの中から） ----

def update(db: Session, article_ids, res=None) -> dict:
    """新しく保存した記事・物の取り出しが保存された記事を、前後 48 時間の記事と見比べて、イベントへ入れる・合わせる。コミットは呼ぶ側。"""
    ids = {int(i) for i in article_ids}
    if not ids:
        return {"made": 0, "merged": 0, "links": 0}
    if res is None:
        from qnapnewshub.analytics import Resolver

        res = Resolver()
    rows = list(db.execute(select(NewsItem.id, NewsItem.published_at).where(NewsItem.id.in_(sorted(ids)), _eligible())))
    if not rows:
        return {"made": 0, "merged": 0, "links": 0}
    touched = {r.id for r in rows}
    times = [_naive(r.published_at) for r in rows]
    pool = _load(db, min(times) - timedelta(hours=WINDOW_HOURS), max(times) + timedelta(hours=WINDOW_HOURS))
    return _merge(db, find_links(pool, touched & set(pool)), touched & set(pool), res)


def rebuild_in(db: Session, res=None, now: datetime | None = None) -> dict:
    """最近 KEEP_DAYS 日の記事から、イベントを全部作り直す（呼ぶ側のトランザクションの中）。要約は、同じ記事を持つ新しいイベントへ引き継ぐ。"""
    from qnapnewshub.analytics import Resolver

    res = res or Resolver()
    now = now or datetime.now(_UTC)
    carry: dict[int, dict] = {}
    for ev in db.scalars(select(Event).where(Event.summary != "")):
        data = {"summary": ev.summary, "summary_model": ev.summary_model, "summary_at": ev.summary_at, "summary_tries": ev.summary_tries}
        for (aid,) in db.execute(select(EventArticle.article_id).where(EventArticle.event_id == ev.id)):
            carry[aid] = data
    db.execute(delete(EventArticle))
    db.execute(delete(Event))
    db.flush()
    pool = _load(db, now - timedelta(days=KEEP_DAYS), now + timedelta(hours=1))
    info = _merge(db, find_links(pool, set(pool)), set(pool), res, carry)
    info["articles"] = len(pool)
    return info


def cleanup(db: Session, now: datetime | None = None) -> int:
    """KEEP_DAYS 日より前に終わったイベント・記事が 2 件未満になったイベントを消す。"""
    now = now or datetime.now(_UTC)
    cut = _naive(now - timedelta(days=KEEP_DAYS))
    old = list(db.scalars(select(Event.id).where(Event.last_at < cut)))
    few = list(db.scalars(select(Event.id).where(select(func.count()).select_from(EventArticle).where(EventArticle.event_id == Event.id).scalar_subquery() < 2)))
    gone = sorted(set(old) | set(few))
    for part in _chunks(gone):
        db.execute(delete(EventArticle).where(EventArticle.event_id.in_(part)))
        db.execute(delete(Event).where(Event.id.in_(part)))
    return len(gone)


def apply_summaries(db: Session, staged: dict[int, dict]) -> int:
    """{イベントの id: {"summary", "model", "at"} または {"tries": 1}}（失敗）を保存する。消えたイベントは飛ばす。"""
    done = 0
    for eid, data in staged.items():
        ev = db.get(Event, eid)
        if ev is None:
            continue
        if data.get("summary"):
            ev.summary, ev.summary_model, ev.summary_at = data["summary"], str(data.get("model", ""))[:200], data.get("at") or datetime.now(_UTC)
            done += 1
        ev.summary_tries = int(ev.summary_tries or 0) + int(data.get("tries", 0))
    return done


# ---- 読む ----

def _entities_of(raw: str) -> list[dict]:
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and "id" in r]


def _summary_of(ev: Event) -> tuple[str, bool]:
    """(要約, 保存した要約か)。まとめ書きを待っている要約があれば、それを出す。"""
    from qnapnewshub import writer

    pending = writer.get().pending_event_summary(ev.id)
    if pending and pending.get("summary"):
        return str(pending["summary"]), True
    return ev.summary or "", bool(ev.summary)


def present(ev: Event) -> dict:
    summary, _ = _summary_of(ev)
    return {"id": ev.id, "rep_id": ev.rep_id, "title": ev.title, "n_articles": ev.n_articles, "n_sources": ev.n_sources, "has_primary": bool(ev.has_primary),
            "first_at": _iso(ev.first_at), "last_at": _iso(ev.last_at), "entities": _entities_of(ev.entities), "summary": summary,
            "summary_model": ev.summary_model if ev.summary else ""}


def list_events(db: Session, *, sort: str = "recent", min_articles: int = 2, primary: bool = False, days: int = 7, entity_id: int = 0, limit: int = 30, offset: int = 0,
                now: datetime | None = None) -> dict:
    """イベントの一覧。sort は recent（最終の記事が新しい順）・size（記事の数）・first（初報が新しい順）。days は最終の記事がその日数以内。"""
    limit = max(1, min(int(limit), 100))
    conds = [Event.n_articles >= max(2, int(min_articles))]
    if days > 0:
        conds.append(Event.last_at >= _naive((now or datetime.now(_UTC)) - timedelta(days=days)))
    if primary:
        conds.append(Event.has_primary.is_(True))
    if entity_id:
        conds.append(Event.id.in_(select(EventArticle.event_id).join(ArticleEntity, ArticleEntity.article_id == EventArticle.article_id).where(ArticleEntity.entity_id == entity_id)))
    order = {"size": [Event.n_articles.desc(), Event.n_sources.desc(), Event.last_at.desc()], "first": [Event.first_at.desc(), Event.id.desc()]}.get(
        sort, [Event.last_at.desc(), Event.id.desc()])
    total = int(db.scalar(select(func.count(Event.id)).where(*conds)) or 0)
    rows = db.scalars(select(Event).where(*conds).order_by(*order).limit(limit).offset(max(0, offset))).all()
    return {"items": [present(e) for e in rows], "total": total}


def detail(db: Session, event_id: int, res=None) -> dict | None:
    """イベントのページ: 関わる記事を時刻順に・出どころの種類（origin）ごとの数。"""
    ev = db.get(Event, event_id)
    if ev is None:
        return None
    if res is None:
        from qnapnewshub.analytics import Resolver

        res = Resolver()
    from qnapnewshub import store

    arts = db.execute(select(NewsItem).join(EventArticle, EventArticle.article_id == NewsItem.id).where(EventArticle.event_id == ev.id)
                      .order_by(NewsItem.published_at.asc(), NewsItem.id.asc())).scalars().all()
    labels = dict(sources_mod.ORIGIN_LABELS)
    items, by_origin = [], Counter()
    for a in arts:
        origin = res.origin(a.kind, a.source_key)
        by_origin[origin] += 1
        items.append({"id": a.id, "title": a.title, "url": store.safe_url(a.url), "source": a.source, "source_key": a.source_key, "kind": a.kind, "origin": origin,
                      "origin_label": labels.get(origin, origin), "published_at": _iso(a.published_at), "summary": (a.summary_ja or a.summary or "")[:300],
                      "is_rep": a.id == ev.rep_id, "is_dup": bool(a.dup_of)})
    out = present(ev)
    out["articles"] = items
    out["origins"] = [{"origin": k, "label": labels.get(k, k), "n": n} for k, n in sorted(by_origin.items(), key=lambda kv: (-kv[1], kv[0]))]
    out["hours_since_first"] = round((datetime.now(_UTC).replace(tzinfo=None) - _naive(ev.first_at)).total_seconds() / 3600, 1)
    return out


def collapse(db: Session, rows: list[dict], *, enabled: bool = True) -> list[dict]:
    """ニュースの一覧（present した行）で、同じイベントの記事を 1 つにまとめる。代表の記事があればそれ、無ければ先に出た記事を残し、
    event: {id, others（ほかの記事の数＝イベントの記事の数 − 1）, sources} を付ける。ページをまたいだ重複までは見ない。"""
    ids = [r["id"] for r in rows]
    if not ids or not enabled:
        return rows
    found: dict[int, int] = {}
    for part in _chunks(ids):
        found.update(dict(db.execute(select(EventArticle.article_id, EventArticle.event_id).where(EventArticle.article_id.in_(part))).all()))
    if not found:
        return rows
    evs = {e.id: e for e in db.scalars(select(Event).where(Event.id.in_(set(found.values()))))}
    keep: dict[int, int] = {}      # イベント → 残す記事の id
    for r in rows:
        eid = found.get(r["id"])
        if eid is None or eid not in evs:
            continue
        if eid not in keep or (evs[eid].rep_id == r["id"]):
            keep[eid] = r["id"]
    out = []
    for r in rows:
        eid = found.get(r["id"])
        if eid is None or eid not in evs:
            out.append(r)
        elif keep[eid] == r["id"]:
            ev = evs[eid]
            r["event"] = {"id": ev.id, "others": max(0, ev.n_articles - 1), "sources": ev.n_sources, "primary": bool(ev.has_primary)}
            out.append(r)
    return out


def counts(db: Session, since: datetime, until: datetime | None = None) -> dict:
    """独立したイベントの数（記事の公開が since〜until の間）: イベント（2 件以上のまとまり）の数 ＋ どのイベントにも入らない記事の数。
    events は、その間に公開された記事を 1 件以上含むイベント。members は、イベントに入っている記事（その間に公開されたもの）の数。"""
    lo = _naive(since)
    hi = _naive(until) if until else None
    cond = [NewsItem.published_at >= lo, _eligible()] + ([NewsItem.published_at < hi] if hi else [])
    total = int(db.scalar(select(func.count(NewsItem.id)).where(*cond)) or 0)
    j = select(func.count(EventArticle.article_id), func.count(func.distinct(EventArticle.event_id))).join(NewsItem, NewsItem.id == EventArticle.article_id).where(*cond)
    members, events = db.execute(j).one()
    singles = max(0, total - int(members or 0))
    return {"articles": total, "events": int(events or 0), "singles": singles, "independent": int(events or 0) + singles, "members": int(members or 0)}


def summary_targets(db: Session, limit: int = 40) -> list[Event]:
    """要約の対象: 記事が 3 件以上・出どころが 2 以上で、まだ要約が無く、失敗が 3 回未満のイベント（新しい順）。まとめ書きを待っている要約は除く。"""
    from qnapnewshub import writer

    rows = db.scalars(select(Event).where(Event.n_articles >= SUMMARY_MIN_ARTICLES, Event.n_sources >= SUMMARY_MIN_SOURCES, Event.summary == "",
                                          Event.summary_tries < SUMMARY_MAX_TRIES).order_by(Event.last_at.desc(), Event.id.desc()).limit(limit + 40)).all()
    coord = writer.get()
    out = []
    for ev in rows:
        pending = coord.pending_event_summary(ev.id)
        if pending and (pending.get("summary") or int(pending.get("tries", 0)) + int(ev.summary_tries or 0) >= SUMMARY_MAX_TRIES):
            continue
        out.append(ev)
    return out[:limit]


def summary_inputs(db: Session, ev: Event, *, max_articles: int = 8) -> list[dict]:
    """要約の材料: イベントの記事（代表が先・古い順）の題・出どころ・概要（LLM の訳があればそれ）。"""
    rows = db.execute(select(NewsItem).join(EventArticle, EventArticle.article_id == NewsItem.id).where(EventArticle.event_id == ev.id)
                      .order_by(NewsItem.published_at.asc(), NewsItem.id.asc())).scalars().all()
    rows.sort(key=lambda a: a.id != ev.rep_id)
    return [{"id": a.id, "title": a.title, "source": a.source, "summary": a.summary_ja or a.summary or ""} for a in rows[:max_articles]]


# ---- 作り直しの入口 ----

META_KEY = "events_v"
VERSION = "1"


def rebuild(reason: str = "manual") -> dict:
    """書き手のスレッドで作り直す（管理者の操作・初回）。"""
    from qnapnewshub import analytics, db as news_db, writer

    def do() -> dict:
        with news_db.session() as db:
            info = rebuild_in(db)
            analytics._set_meta(db, META_KEY, VERSION)  # noqa: SLF001
            db.commit()
        return info

    return writer.run(f"news.events:{reason}", do)


def ensure(db: Session) -> None:
    """イベントの表がまだ作られていない（既存のデータ・初回）なら、既存の記事から作る。読みの前に。休止中は作らない。"""
    from qnapnewshub import analytics, rest

    if analytics._meta(db, META_KEY) == VERSION or rest.active():  # noqa: SLF001
        return
    rebuild("auto")


def diag(db: Session) -> dict:
    """診断: イベントの数・要約の済んだ数・要約待ち（対象で未）・要約を諦めた数・「今日の傾向」の最新の日。"""
    from qnapnewshub import trend

    total = int(db.scalar(select(func.count(Event.id))) or 0)
    done = int(db.scalar(select(func.count(Event.id)).where(Event.summary != "")) or 0)
    eligible = [Event.n_articles >= SUMMARY_MIN_ARTICLES, Event.n_sources >= SUMMARY_MIN_SOURCES, Event.summary == ""]
    waiting = int(db.scalar(select(func.count(Event.id)).where(*eligible, Event.summary_tries < SUMMARY_MAX_TRIES)) or 0)
    gave_up = int(db.scalar(select(func.count(Event.id)).where(*eligible, Event.summary_tries >= SUMMARY_MAX_TRIES)) or 0)
    note = trend.latest(db)
    return {"events": total, "summarized": done, "summary_waiting": waiting, "summary_gave_up": gave_up, "trend_note_day": note["day"] if note else "", "trend_note_slot": note["slot"] if note else None}
