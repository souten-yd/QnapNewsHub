"""ニュースの保存と検索（記事の更新・保存期間・出来事の記録・一覧・アーカイブ・話題の動き）。

- 同じ url は 1 行（題・抜粋・画像・話題だけ更新。published_at は最初の値を保つ＝アーカイブの並びが動かない）。
- 保存期間: アーカイブなので消さない。ただし MAX_ITEMS を超えたら古い順に消す（star された記事は残す）。
- 全文検索は SQLite なら FTS5 の 2 つの索引（日本語は trigram で 3 文字以上の部分一致・英語は porter）を BM25 で合わせて順位づける（searchindex.py）。短い語・FTS5 が無いときは LIKE。
- 日の区切りは、サーバーの地域の時刻（日本なら日本時間の 0 時）。DB には UTC で持つ。
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, func, not_, or_, select, text
from sqlalchemy.orm import Session

from qnapnewshub import channels as channels_mod
from qnapnewshub import db as news_db
from qnapnewshub.db import NewsAction, NewsItem
from qnapnewshub import prefs, profile, searchindex, writer, ztext
from qnapnewshub import sources as sources_mod
from qnapnewshub.sources import plain, safe_url
from qnapnewshub.topics import extract_topics

logger = logging.getLogger("qnapnewshub.store")

MAX_ITEMS = 50_000
ACTIONS = ("open", "like", "dislike", "hide", "star")
RECOMMEND_WINDOW_DAYS = 21
RECOMMEND_CANDIDATES = 600
PAGE_DEFAULT, PAGE_MAX = 24, 60
_UTC = timezone.utc


def utc_naive(value: datetime) -> datetime:
    return (value.astimezone(_UTC) if value.tzinfo else value).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=_UTC) if value.tzinfo is None else value.astimezone(_UTC)


def iso(value: datetime) -> str:
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_tz():
    return datetime.now().astimezone().tzinfo


def local_day(value: datetime) -> str:
    return as_utc(value).astimezone(local_tz()).strftime("%Y-%m-%d")


def day_range(day: str) -> tuple[datetime, datetime]:
    """YYYY-MM-DD（地域の日）を [開始, 終了) の UTC にする。形が違えば ValueError。"""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=local_tz())
    return utc_naive(start), utc_naive(start + timedelta(days=1))


# ---- 保存 ----

def topics_of(item: NewsItem) -> list[str]:
    try:
        value = json.loads(item.topics or "[]")
    except ValueError:
        return []
    return [t for t in value if isinstance(t, str)] if isinstance(value, list) else []


def item_dict(item: NewsItem) -> dict:
    """RAG・LLM へ渡す記事の形（出来事は呼ぶ側で除く）。"""
    return {"id": item.id, "kind": item.kind, "source": item.source, "source_key": item.source_key, "title": item.title, "url": item.url,
            "summary": item.summary or "", "summary_ja": item.summary_ja or "", "published_at": iso(item.published_at), "topics": topics_of(item),
            "gist": item.gist or ""}


def upsert_items(db: Session, items: list[dict]) -> dict:
    """記事を保存する。同じ url は更新だけ。{"new": 件数, "updated": 件数, "changed": 新しい・題か抜粋が変わった記事の dict}。"""
    now = datetime.now(_UTC)
    by_url = {item["url"]: item for item in items}   # 同じ回の重複は後勝ち
    existing: dict[str, NewsItem] = {}
    urls = list(by_url)
    for i in range(0, len(urls), 400):
        for row in db.execute(select(NewsItem).where(NewsItem.url.in_(urls[i:i + 400]))).scalars():
            existing[row.url] = row
    new = updated = 0
    touched: list[NewsItem] = []
    for url, data in by_url.items():
        topics = json.dumps(extract_topics(data["title"], data["summary"], data.get("extra_topics", ())), ensure_ascii=False)
        row = existing.get(url)
        if row is None:
            row = NewsItem(kind=data["kind"], source=data["source"], source_key=data["source_key"], title=data["title"], url=url,
                           summary=data["summary"], image_url=data.get("image_url"), published_at=data["published_at"],
                           fetched_at=now, topics=topics, user_id=data.get("user_id"))
            db.add(row)
            touched.append(row)
            new += 1
        else:
            if (row.title, row.summary) != (data["title"], data["summary"]):
                touched.append(row)
            row.kind, row.source, row.source_key = data["kind"], data["source"], data["source_key"]
            if row.gist:      # LLM が付けた話題の語は、取り直しで消さない
                topics = json.dumps(list(dict.fromkeys(topics_of(row) + json.loads(topics))), ensure_ascii=False)
            if row.summary != data["summary"]:
                row.summary_ja, row.prep_tries = "", 0      # 抜粋が変わったら、古い訳は捨てて下ごしらえし直す
            row.title, row.summary, row.topics, row.fetched_at = data["title"], data["summary"], topics, now
            row.image_url = data.get("image_url") or row.image_url
            updated += 1
    db.commit()
    return {"new": new, "updated": updated, "changed": [item_dict(r) for r in touched if r.kind != "event"],
            "new_ids": [r.id for r in touched if r.id is not None and r.url not in existing],
            "new_urls": [u for u in by_url if u not in existing]}


def _fields_of(data: dict, cur, now: datetime) -> dict:
    """取り直した data を cur（いまの記事）へ重ねたときの、列の更新。抜粋が変わったら古い訳は捨てる。"""
    topics = json.dumps(extract_topics(data["title"], data["summary"], data.get("extra_topics", ())), ensure_ascii=False)
    if cur.gist:      # LLM が付けた話題の語は、取り直しで消さない
        topics = json.dumps(list(dict.fromkeys(topics_of(cur) + json.loads(topics))), ensure_ascii=False)
    out = {"kind": data["kind"], "source": data["source"], "source_key": data["source_key"], "title": data["title"], "summary": data["summary"],
           "topics": topics, "fetched_at": now, "image_url": data.get("image_url") or cur.image_url}
    if cur.summary != data["summary"]:
        out.update(summary_ja="", prep_tries=0)
    return out


def stage_items(items: list[dict]) -> dict:
    """記事をバッファへ積む（ここでは DB へ書かない。Write Coordinator が後でまとめて書く）。upsert_items と同じ形の結果。
    新しい記事には、先に id を割り当てる（未保存のまま「操作」を受けても同じ id で書ける）。"""
    coord = writer.get()
    now = datetime.now(_UTC)
    by_url = {item["url"]: item for item in items}
    existing: dict[str, NewsItem] = {}
    urls = list(by_url)
    with news_db.session() as db:
        for i in range(0, len(urls), 400):
            for row in db.execute(select(NewsItem).where(NewsItem.url.in_(urls[i:i + 400]))).scalars():
                existing[row.url] = writer.clone(row)
    new = updated = 0
    changed: list[dict] = []
    new_ids: list[int] = []
    new_urls: list[str] = []
    added = 0
    id_by_url: dict[str, int] = {}
    for url, data in by_url.items():
        with coord.lock:
            pending = {r.url: r for r in (*coord.inflight.new.values(), *coord.buf.new.values())}
            in_buf = next((r for r in coord.buf.new.values() if r.url == url), None)
        if in_buf is not None:                    # まだ書いていない新しい記事の取り直し: そのまま上書き
            touched = (in_buf.title, in_buf.summary) != (data["title"], data["summary"])
            for k, v in _fields_of(data, in_buf, now).items():
                setattr(in_buf, k, v)
            updated += 1
            if touched and in_buf.kind != "event":
                changed.append(item_dict(in_buf))
            continue
        base = pending.get(url) or existing.get(url)
        if base is not None and getattr(base, "cold", 0):      # 1 年より古い（抜粋を圧縮した）記事の取り直しは何もしない
            continue
        if base is None:
            item_id = coord.allocate_id()
            rec = NewsItem(id=item_id, kind=data["kind"], source=data["source"], source_key=data["source_key"], title=data["title"], url=url,
                           summary=data["summary"], image_url=data.get("image_url"), published_at=data["published_at"], fetched_at=now,
                           topics=json.dumps(extract_topics(data["title"], data["summary"], data.get("extra_topics", ())), ensure_ascii=False),
                           user_id=data.get("user_id"), gist="", image_tried=False, summary_ja="", img_prompt="", prep_tries=0,
                           canon_url=data.get("canon_url", ""), guid=data.get("guid", ""), simhash=data.get("simhash"),
                           dup_of=data.get("dup_of") or id_by_url.get(data.get("dup_of_url", "")))
            rec._unsaved = True
            with coord.lock:
                coord.buf.new[item_id] = rec
            id_by_url[url] = item_id
            added += len(rec.title) + len(rec.summary) + len(url) + 460
            new += 1
            new_urls.append(url)
            if rec.dup_of is None:      # 別の出どころ（重複）は、一言要約・下ごしらえ・画像の仕事に回さない（代表の記事がやる）
                new_ids.append(item_id)
                if rec.kind != "event":
                    changed.append(item_dict(rec))
            continue
        cur = writer.clone(base)
        coord._overlay(cur)  # noqa: SLF001
        touched = (cur.title, cur.summary) != (data["title"], data["summary"])
        fields = _fields_of(data, cur, now)
        with coord.lock:
            coord.buf.updates[url] = {**coord.buf.updates.get(url, {}), **fields}
            coord.buf.update_ids[url] = base.id
            if "summary_ja" in fields and base.id in coord.buf.patches:      # 抜粋が変わったので、古い下ごしらえの結果は捨てる
                for k in ("summary_ja", "prep_tries"):
                    coord.buf.patches[base.id].get("set", {}).pop(k, None)
                coord.buf.patches[base.id].pop("tries", None)
        added += len(data["title"]) + len(data["summary"]) + 200
        for k, v in fields.items():
            setattr(cur, k, v)
        updated += 1
        if touched and cur.kind != "event":
            changed.append(item_dict(cur))
    coord._touch(added)  # noqa: SLF001
    return {"new": new, "updated": updated, "changed": changed, "new_ids": new_ids, "new_urls": [u for u in by_url if u in new_urls]}


def apply_staged(db: Session, new: list, updates: dict[str, dict], patches: dict[int, dict]) -> list[NewsItem]:
    """バッファを DB へ（コミットは呼ぶ側。1 トランザクション）。Write Coordinator の保存から呼ぶ。
    返すのは、この保存で初めて画像が付いた既存の記事（分析の集計の「画像のある数」を足すため）。"""
    gained: list[NewsItem] = []
    for rec in new:
        db.add(writer.clone(rec))
    db.flush()
    urls = list(updates)
    for i in range(0, len(urls), 400):
        for row in db.execute(select(NewsItem).where(NewsItem.url.in_(urls[i:i + 400]))).scalars():
            had = bool(row.image_url)
            for k, v in updates[row.url].items():
                setattr(row, k, v)
            if not had and row.image_url:
                gained.append(row)
    ids = list(patches)
    for i in range(0, len(ids), 400):
        for row in db.execute(select(NewsItem).where(NewsItem.id.in_(ids[i:i + 400]))).scalars():
            had = bool(row.image_url)
            writer.apply_patch(row, patches[row.id])
            if not had and row.image_url:
                gained.append(row)
    db.flush()
    return gained


def changes_persisted(db: Session, after_id: int = 0, limit: int = 500) -> list[dict]:
    """ControlDeck への差分に出す記事: 保存済み（DB にある）ものだけ。バッファの未保存の分は出さない（保存されてから出る）。"""
    rows = db.scalars(select(NewsItem).where(NewsItem.id > after_id, NewsItem.kind != "event", NewsItem.dup_of.is_(None)).order_by(NewsItem.id.asc()).limit(max(1, min(limit, 2000)))).all()
    return [{**item_dict(r), "persisted": True} for r in rows]


def _pending_match(item: NewsItem, user_id: int, *, kinds, source: str, topic: str, q: str, day: str, mute: bool) -> bool:
    """バッファの記事が、一覧の条件（filters と同じ意味）に合うか。"""
    if item.user_id is not None and item.user_id != user_id:
        return False
    if getattr(item, "dup_of", None):
        return False
    if any(item.source_key.startswith(p) for p in sources_mod.HIDDEN_SOURCE_PREFIXES):
        return False
    if mute:
        taste = prefs.taste(user_id)
        text_ = f"{item.title} {item.summary}"
        if any(t in text_ for t in taste["mute_terms"]) or item.source in taste["mute_sources"]:
            return False
    if kinds and item.kind not in kinds:
        return False
    if source and source not in (item.source_key, item.source):
        return False
    if topic and topic not in topics_of(item):
        return False
    if q.strip():
        hay = f"{item.title} {item.summary} {item.source}".lower()
        if not all(t.lower() in hay for t in q.split()[:8]):
            return False
    if day:
        start, end = day_range(day)
        pub = utc_naive(item.published_at)
        if not start <= pub < end:
            return False
    return True


def pending_for(user_id: int, **kw) -> list[NewsItem]:
    return [p for p in writer.get().pending_items() if _pending_match(p, user_id, **kw)]


def prune(db: Session, limit: int | None = None, removed_urls: list[str] | None = None, commit: bool = True) -> int:
    """残す件数（設定のアーカイブ。既定 MAX_ITEMS）を超えた分を古い順に消す（star は残す）。消した件数。
    removed_urls を渡すと、消した記事の URL をそこへ足す（RAG から一緒に消す設定のとき）。"""
    cap = MAX_ITEMS if limit is None else limit
    total = db.scalar(select(func.count(NewsItem.id))) or 0
    if total <= cap:
        return 0
    starred = select(NewsAction.item_id).where(NewsAction.action == "star")
    ids = list(db.scalars(select(NewsItem.id).where(NewsItem.id.not_in(starred))
                          .order_by(NewsItem.published_at.asc(), NewsItem.id.asc()).limit(total - cap)))
    if removed_urls is not None:
        for i in range(0, len(ids), 500):
            removed_urls += list(db.scalars(select(NewsItem.url).where(NewsItem.id.in_(ids[i:i + 500]), NewsItem.kind != "event")))
    for i in range(0, len(ids), 500):
        db.execute(delete(news_db.NewsBody).where(news_db.NewsBody.item_id.in_(ids[i:i + 500])))
        db.execute(delete(news_db.NewsCold).where(news_db.NewsCold.item_id.in_(ids[i:i + 500])))
        db.execute(delete(NewsItem).where(NewsItem.id.in_(ids[i:i + 500])))
    if commit:
        db.commit()
    return len(ids)


def _internal_path(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


# ---- 一覧 ----

def visible(user_id: int):
    """その人に見える記事（全員宛て＋自分宛ての出来事）。自分のリポジトリ（HIDDEN_SOURCE_PREFIXES）の記事は、貯まっていても出さない。"""
    return and_(or_(NewsItem.user_id.is_(None), NewsItem.user_id == user_id), NewsItem.dup_of.is_(None),
                *[not_(NewsItem.source_key.like(f"{_like_escape(p)}%", escape="\\")) for p in sources_mod.HIDDEN_SOURCE_PREFIXES])


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SearchPlan:
    """検索の計画: clause は記事の条件、order は {記事の id: 順位（0 が最良）}（索引で順位づけできたときだけ。短い語だけ・索引が無いときは None）。"""

    def __init__(self, clause, order: dict[int, int] | None):
        self.clause, self.order = clause, order


def _like_all(terms: list[str]):
    return and_(*[or_(NewsItem.title.like(f"%{_like_escape(t)}%", escape="\\"), NewsItem.summary_ja.like(f"%{_like_escape(t)}%", escape="\\"),
                      NewsItem.summary.like(f"%{_like_escape(t)}%", escape="\\"), NewsItem.source.like(f"%{_like_escape(t)}%", escape="\\")) for t in terms])


def search_plan(db: Session, q: str) -> SearchPlan | None:
    """検索の計画。語はすべて含む（AND）。索引で探せる語（ASCII の語・3 文字以上の語）は FTS5 の BM25、短い語は LIKE。
    作り直し中（古い DB の移行）は、未索引の範囲を LIKE で補う。"""
    terms = [t for t in q.split() if t][:8]
    if not terms:
        return None
    res = searchindex.search(db.connection(), q) if db.get_bind().dialect.name == "sqlite" else None
    if res is None:
        return SearchPlan(_like_all(terms), None)
    ids = [i for i, _ in res["hits"]]
    clause = NewsItem.id.in_(ids) if ids else NewsItem.id.is_(None)
    if res["short"]:
        clause = and_(clause, _like_all(res["short"]))
    prog = searchindex.progress(db.connection())
    if prog["building"]:      # まだ索引に入れていない範囲は LIKE で（移行の間だけ）
        clause = or_(clause, and_(NewsItem.id > prog["cursor"], NewsItem.id <= prog["hi"], _like_all(terms)))
    return SearchPlan(clause, {i: n for n, i in enumerate(ids)})


def search_clause(db: Session, q: str):
    plan = search_plan(db, q)
    return plan.clause if plan else None


def hydrate(db: Session, rows: list[NewsItem]) -> list[NewsItem]:
    """古い記事（cold）の抜粋を news_cold から展開して重ねたコピーを返す（DB の行は変えない）。cold が無ければそのまま。"""
    ids = [r.id for r in rows if getattr(r, "cold", 0)]
    if not ids:
        return rows
    zs = dict(db.execute(select(news_db.NewsCold.item_id, news_db.NewsCold.z).where(news_db.NewsCold.item_id.in_(ids))).all())
    out = []
    for r in rows:
        if r.id in zs:
            c = writer.clone(r)
            try:
                c.summary = ztext.unpack(zs[r.id])
            except ValueError:
                logger.warning("圧縮した抜粋を展開できません: id=%s", r.id)
            if getattr(r, "_unsaved", False):
                c._unsaved = True
            r = c
        out.append(r)
    return out


def action_exists(user_id: int, action: str):
    return exists().where(NewsAction.item_id == NewsItem.id, NewsAction.user_id == user_id, NewsAction.action == action)


def mute_conds(user_id: int) -> list:
    """設定のミュートの語（題・抜粋）とソース（出どころの名前）に合う記事を除く条件。"""
    taste = prefs.taste(user_id)
    conds = []
    for term in taste["mute_terms"]:
        like = f"%{_like_escape(term)}%"
        conds.append(not_(or_(NewsItem.title.like(like, escape="\\"), NewsItem.summary.like(like, escape="\\"))))
    if taste["mute_sources"]:
        conds.append(NewsItem.source.not_in(taste["mute_sources"]))
    return conds


def filters(db: Session, user_id: int, *, kinds: list[str] | None = None, source: str = "", topic: str = "", q: str = "",
            day: str = "", mute: bool = False, search: SearchPlan | None = None) -> list:
    conds = [visible(user_id)] + (mute_conds(user_id) if mute else [])
    if kinds:
        conds.append(NewsItem.kind.in_(kinds))
    if source:
        conds.append(or_(NewsItem.source_key == source, NewsItem.source == source))
    if topic:
        conds.append(NewsItem.topics.like(f'%"{_like_escape(topic)}"%', escape="\\"))
    plan = search if search is not None else (search_plan(db, q.strip()) if q.strip() else None)
    if plan is not None:
        conds.append(plan.clause)
    if day:
        start, end = day_range(day)
        conds += [NewsItem.published_at >= start, NewsItem.published_at < end]
    return conds


def states(db: Session, user_id: int, ids: list[int]) -> dict[int, dict]:
    out = {i: {a: False for a in ACTIONS} for i in ids}
    if ids:
        for item_id, action in db.execute(select(NewsAction.item_id, NewsAction.action).where(
                NewsAction.user_id == user_id, NewsAction.item_id.in_(ids))):
            if action in ACTIONS:
                out[item_id][action] = True
    return out


def attach_also(db: Session, rows: list[dict], collapse: bool = False) -> list[dict]:
    """一覧の各記事に also（同じ記事の別の出どころの数。代表の記事だけに付く）と、その出どころの名前（最大 5）を付ける。"""
    ids = [r["id"] for r in rows]
    if not ids:
        return rows
    found: dict[int, list[str]] = defaultdict(list)
    for dup_of, source in db.execute(select(NewsItem.dup_of, NewsItem.source).where(NewsItem.dup_of.in_(ids)).order_by(NewsItem.id)):
        found[dup_of].append(source)
    for p in writer.get().pending_items():
        if p.dup_of in ids:
            found[p.dup_of].append(p.source)
    for r in rows:
        names = found.get(r["id"], [])
        r["also"] = len(names)
        if names:
            r["also_sources"] = list(dict.fromkeys(names))[:5]
    from qnapnewshub import entities

    entities.attach_tags(db, rows)      # 物の札（要素の取り出しが済んだ記事だけ。無ければ空）
    if collapse:      # 同じ出来事（イベント）の記事は 1 つにまとめて、event: {id, others} を付ける（検索・日付・スターの一覧ではまとめない）
        from qnapnewshub import events

        rows = events.collapse(db, rows)
    return rows


def present(item: NewsItem, state: dict, score: float | None = None) -> dict:
    url = safe_url(item.url) or (_internal_path(item.url) if item.kind == "event" else None)
    row = {"id": item.id, "kind": item.kind, "source": item.source, "source_key": item.source_key, "title": item.title,
           "url": url, "summary": item.summary_ja or item.summary,
           "image_url": safe_url(item.image_url),
           "published_at": iso(item.published_at),
           "topics": topics_of(item), "state": state, "persisted": not getattr(item, "_unsaved", False)}
    if getattr(item, "body_state", 0) == 1:      # 本文を取り出してある印（本文は出さない。元のページを開く）
        row["has_body"] = True
    if item.summary_ja:      # 訳した概要を summary に出し、元の抜粋は開いたとき見られるよう別に持つ
        row["translated"], row["summary_original"] = True, item.summary
    if score is not None:
        row["score"] = round(score, 4)
    return row


def list_items(db: Session, user_id: int, *, view: str = "latest", kinds: list[str] | None = None, source: str = "", topic: str = "",
               q: str = "", day: str = "", before: str = "", limit: int = PAGE_DEFAULT, now: datetime | None = None,
               channel: dict | None = None) -> dict:
    """{"items": [...], "next": 次のページの印 | None}。view は recommended・latest・starred。
    おすすめ・新着は、隠した記事を除く（検索・日付で探すときは隠したものも出す）。"""
    limit = max(1, min(int(limit or PAGE_DEFAULT), PAGE_MAX))
    now = now or datetime.now(_UTC)
    searching = bool(q.strip() or day)
    plan = search_plan(db, q.strip()) if q.strip() else None
    conds = filters(db, user_id, kinds=kinds, source=source, topic=topic, q=q, day=day, mute=not searching, search=plan)
    if view == "starred":
        conds.append(action_exists(user_id, "star"))
    elif not searching:
        conds.append(not_(action_exists(user_id, "hide")))
    pend_kw = dict(kinds=kinds, source=source, topic=topic, q=q, day=day, mute=not searching)
    pend = [] if view == "starred" else pending_for(user_id, **pend_kw)      # まだ書いていない記事（操作が付かないので、星は無い）
    if channel is not None:
        return _channel_page(db, user_id, channel, conds, view=view, before=before, limit=limit, now=now, pend=pend)
    if plan is not None and plan.order is not None:
        return _ranked_page(db, user_id, plan, conds, pend, before=before, limit=limit)
    if view == "recommended" and not searching:
        since = utc_naive(now - timedelta(days=RECOMMEND_WINDOW_DAYS))
        rows = [writer.get().effective(r) for r in db.scalars(select(NewsItem).where(*conds, NewsItem.published_at >= since)
                          .order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(RECOMMEND_CANDIDATES)).all()]
        rows = sorted([*rows, *[p for p in pend if utc_naive(p.published_at) >= since]], key=lambda r: (utc_naive(r.published_at), r.id), reverse=True)[:RECOMMEND_CANDIDATES]
        st = states(db, user_id, [r.id for r in rows])
        ranked = profile.rank(db, user_id, rows, st, now)
        offset = int(before[2:]) if before.startswith("o:") and before[2:].isdigit() else 0
        page = ranked[offset:offset + limit]
        return {"items": attach_also(db, [present(r, st[r.id], score) for r, score in page], collapse=True),
                "next": f"o:{offset + limit}" if offset + limit < len(ranked) else None}
    if before:
        stamp, _, last = before.partition("|")
        try:
            cut = utc_naive(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
            conds.append(or_(NewsItem.published_at < cut, and_(NewsItem.published_at == cut, NewsItem.id < int(last or 0))))
        except ValueError:
            pass
    rows = [writer.get().effective(r) for r in hydrate(db, db.scalars(select(NewsItem).where(*conds).order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(limit + 1)).all())]
    if pend:
        if before:
            stamp, _, last = before.partition("|")
            try:
                cut = utc_naive(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
                pend = [p for p in pend if (utc_naive(p.published_at), p.id) < (cut, int(last or 0))]
            except ValueError:
                pass
        rows = sorted([*rows, *pend], key=lambda r: (utc_naive(r.published_at), r.id), reverse=True)[:limit + 1]
    more = len(rows) > limit
    rows = rows[:limit]
    st = states(db, user_id, [r.id for r in rows])
    return {"items": attach_also(db, [present(r, st[r.id]) for r in rows], collapse=view != "starred" and not searching),
            "next": f"{iso(rows[-1].published_at)}|{rows[-1].id}" if more and rows else None}


def _ranked_page(db: Session, user_id: int, plan: SearchPlan, conds: list, pend: list, *, before: str, limit: int) -> dict:
    """検索の結果を BM25 の順で。候補は索引の上位（searchindex.CANDIDATES 件）から表示の条件で絞った分。ページは o:<位置>。
    まだ書いていない記事（pend）は先頭、索引に無い記事（移行の間の LIKE の分）は最後に新しい順。"""
    order = plan.order or {}
    rows = [writer.get().effective(r) for r in hydrate(db, db.scalars(select(NewsItem).where(*conds)).all())]
    seen = {r.id for r in rows}
    rows = [*[p for p in pend if p.id not in seen], *rows]
    big = len(order) + 1
    rows.sort(key=lambda r: (-1 if getattr(r, "_unsaved", False) and r.id not in order else order.get(r.id, big), -utc_naive(r.published_at).timestamp(), -r.id))
    offset = int(before[2:]) if before.startswith("o:") and before[2:].isdigit() else 0
    page = rows[offset:offset + limit]
    st = states(db, user_id, [r.id for r in page])
    return {"items": attach_also(db, [present(r, st[r.id]) for r in page]), "next": f"o:{offset + limit}" if offset + limit < len(rows) else None}


CHANNEL_CANDIDATES = 1500


def _channel_page(db: Session, user_id: int, channel: dict, conds: list, *, view: str, before: str, limit: int, now: datetime, pend: list | None = None) -> dict:
    """チャンネルの記事。語の判定は Python で行うので、新しい順の候補（最大 1500 件）から選ぶ。ページは o:<位置>。"""
    since = utc_naive(now - timedelta(days=RECOMMEND_WINDOW_DAYS * 2))
    rows = [writer.get().effective(r) for r in db.scalars(select(NewsItem).where(*conds, NewsItem.published_at >= since)
                                  .order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(CHANNEL_CANDIDATES))]
    rows = [r for r in [*rows, *[p for p in (pend or []) if utc_naive(p.published_at) >= since]]
            if channels_mod.matches(channel, kind=r.kind, source_key=r.source_key, title=r.title, summary=r.summary or "")]
    rows.sort(key=lambda r: (utc_naive(r.published_at), r.id), reverse=True)
    st = states(db, user_id, [r.id for r in rows])
    scored = profile.rank(db, user_id, rows, st, now) if view == "recommended" else [(r, None) for r in rows]
    offset = int(before[2:]) if before.startswith("o:") and before[2:].isdigit() else 0
    page = scored[offset:offset + limit]
    return {"items": attach_also(db, [dict(present(r, st[r.id], score), channel=channel["id"]) for r, score in page], collapse=view != "starred"),
            "next": f"o:{offset + limit}" if offset + limit < len(scored) else None}


def set_action(db: Session, user_id: int, item_id: int, action: str, on: bool = True) -> dict | None:
    """操作を記録・取り消す。like と dislike は同時に付かない。open は取り消さない。記事が無い・見えないなら None。"""
    item = db.get(NewsItem, item_id)
    if item is None or (item.user_id is not None and item.user_id != user_id):
        return None
    rows = {a.action: a for a in db.scalars(select(NewsAction).where(NewsAction.user_id == user_id, NewsAction.item_id == item_id))}
    if on or action == "open":
        opposite = {"like": "dislike", "dislike": "like"}.get(action)
        if opposite in rows:
            db.delete(rows.pop(opposite))
        if action in rows:
            rows[action].at = datetime.now(_UTC)
        else:
            db.add(NewsAction(user_id=user_id, item_id=item_id, action=action, at=datetime.now(_UTC)))
    elif action in rows:
        db.delete(rows[action])
    db.commit()
    return states(db, user_id, [item_id])[item_id]


def set_action_now(user_id: int, item_id: int, action: str, on: bool = True) -> dict | None:
    """利用者の操作を即時に書く（Write Coordinator 経由）。操作先の記事がまだ未保存なら、先にバッファを書く（外部キーの行を先に作る）。"""
    def do():
        coord = writer.get()
        if coord.has_pending_id(item_id):
            coord._flush("action")  # noqa: SLF001
        with news_db.session() as db:
            return set_action(db, user_id, item_id, action, on)
    return writer.run("news.action", do)


# ---- アーカイブ・話題の動き ----

def archive_days(db: Session, user_id: int, *, kinds: list[str] | None = None) -> dict:
    """日ごとの件数（新しい日が先）。"""
    conds = [visible(user_id)] + ([NewsItem.kind.in_(kinds)] if kinds else [])
    counts: Counter[str] = Counter(local_day(p) for (p,) in db.execute(select(NewsItem.published_at).where(*conds)))
    starred = db.scalar(select(func.count(NewsAction.id)).where(NewsAction.user_id == user_id, NewsAction.action == "star")) or 0
    return {"days": [{"date": d, "count": counts[d]} for d in sorted(counts, reverse=True)], "total": sum(counts.values()), "starred": starred}


def trends(db: Session, user_id: int, days: int = 14, now: datetime | None = None, top: int = 8) -> dict:
    """話題の語ごとの日別件数（上位 top 語）と、ソース別の件数。出来事は数えない。"""
    days = max(1, min(int(days or 14), 90))
    today = (now or datetime.now(_UTC)).astimezone(local_tz()).date()
    labels = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    start, _ = day_range(labels[0])
    per_topic: dict[str, Counter] = defaultdict(Counter)
    sources: Counter[str] = Counter()
    rows = db.execute(select(NewsItem.published_at, NewsItem.topics, NewsItem.source).where(
        visible(user_id), NewsItem.kind != "event", NewsItem.published_at >= start))
    for published, raw, source in rows:
        day = local_day(published)
        sources[source] += 1
        try:
            names = json.loads(raw or "[]")
        except ValueError:
            names = []
        for name in names:
            if isinstance(name, str):
                per_topic[name][day] += 1
    best = sorted(per_topic, key=lambda t: (-sum(per_topic[t].values()), t))[:top]
    return {"days": labels,
            "topics": [{"topic": t, "counts": [per_topic[t][d] for d in labels], "total": sum(per_topic[t].values())} for t in best],
            "sources": [{"source": s, "count": c} for s, c in sources.most_common(10)]}
