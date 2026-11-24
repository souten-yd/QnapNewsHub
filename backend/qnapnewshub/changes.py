"""差分の同期（GET /api/v1/changes）: ControlDeck が「前回の続き」から、変わった記事・物・イベントだけを取る。ARCHITECTURE 5 章・DESIGN 3.3。

- **変更番号（seq）は DB の trigger が付ける**（db.ChangeLog）。記事・物・イベントのどの書き込みの経路（まとめ書き・取り出し・イベントの更新・古い記事の削除）でも漏れない。
  1 つの物につき 1 行（最新の変更）で、物が変わるたびに古い行を消して新しい seq で入れ直す。seq は単調に増え、再利用しない（AUTOINCREMENT）。
- **保存済みだけ**: ここは DB を読むだけ。Write Coordinator のバッファにある未保存の分は出さない（保存されてから出る。store.changes_persisted と同じ決まり）。
- **削除・アーカイブも伝える**: 消えた物は op=delete（印を 90 日残す）。重複として畳まれた記事（dup_of）は op=archive。
- 続きの取り方: 返った cursor を覚えて次の問い合わせに渡す。has_more なら続けて取る。cursor が最新より大きければ reset=true（データが作り直された）。
- 絞り込み: kinds（items・entities・events）・channels（チャンネルの id）・categories（記事の種類 release・news・model・forum・reddit）。
  絞り込みで落ちた物も cursor は進む（取り直さない）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from qnapnewshub import channels as channels_mod
from qnapnewshub import store
from qnapnewshub.db import ChangeLog, Entity, Event, EventArticle, NewsItem

KINDS = ("items", "entities", "events")
LIMIT_DEFAULT, LIMIT_MAX = 500, 2000
TOMBSTONE_DAYS = 90
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

# （表, 種類, 更新で見る列, 追加の条件）
_WATCH = [
    ("news_items", "items", ("title", "summary", "summary_ja", "gist", "image_url", "image_ai", "topics", "dup_of", "extract_v", "img_prompt", "kind", "source"), "{r}.kind <> 'event'"),
    ("entities", "entities", ("name", "type", "article_count"), "1"),
    ("events", "events", ("rep_id", "title", "n_articles", "n_sources", "has_primary", "last_at", "entities", "summary"), "1"),
]


def install(conn) -> None:
    """trigger を用意し（冪等）、変更の表が空で物があれば、いまある物を全部「変更あり」として入れる（最初の同期でも全部取れるように）。"""
    for table, kind, columns, cond in _WATCH:
        put = f"INSERT OR REPLACE INTO change_log (kind, ref_id, op, at) VALUES ('{kind}', {{r}}.id, '{{op}}', {_NOW});"
        changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in columns)
        if table == "news_items":      # 古い記事の抜粋の圧縮（cold の切り替え。summary が空になる）は内容の変更ではない
            changed = changed.replace("OLD.summary IS NOT NEW.summary", "(OLD.summary IS NOT NEW.summary AND OLD.cold IS NEW.cold)")
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS trg_chg_{table}_ins AFTER INSERT ON {table} WHEN {cond.format(r='NEW')} BEGIN {put.format(r='NEW', op='upsert')} END"))
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS trg_chg_{table}_upd AFTER UPDATE ON {table} WHEN ({cond.format(r='NEW')}) AND ({changed}) BEGIN {put.format(r='NEW', op='upsert')} END"))
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS trg_chg_{table}_del AFTER DELETE ON {table} WHEN {cond.format(r='OLD')} BEGIN {put.format(r='OLD', op='delete')} END"))
    if conn.execute(text("SELECT 1 FROM change_log LIMIT 1")).first() is None:
        for table, kind, _columns, cond in _WATCH:
            conn.execute(text(f"INSERT OR IGNORE INTO change_log (kind, ref_id, op, at) SELECT '{kind}', id, 'upsert', {_NOW} FROM {table} WHERE {cond.format(r=table)} ORDER BY id"))


def trim(db: Session) -> int:
    """削除の印（tombstone）のうち TOMBSTONE_DAYS 日より古いものを消す（まとめ書きの中から。コミットは呼ぶ側）。"""
    cut = (datetime.now(timezone.utc) - timedelta(days=TOMBSTONE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.execute(text("DELETE FROM change_log WHERE op = 'delete' AND at < :cut"), {"cut": cut}).rowcount or 0


def latest_seq(db: Session) -> int:
    """いままでに付けた最大の seq（消えた分も含む）。"""
    row = db.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'change_log'")).first()
    return int(row[0]) if row else 0


def _item_row(r: NewsItem, tags: dict[int, list[str]]) -> dict:
    return {**store.item_dict(r), "image_url": store.safe_url(r.image_url) or (r.image_url if r.image_ai else None), "image_ai": bool(r.image_ai),
            "img_prompt": r.img_prompt or "", "extract_v": int(r.extract_v or 0), "dup_of": r.dup_of, "fetched_at": store.iso(r.fetched_at),
            "entities": tags.get(r.id, [])}


def _entity_row(e: Entity) -> dict:
    return {"id": e.id, "key": e.key, "name": e.name, "type": e.type, "first_seen": store.iso(e.first_seen), "last_seen": store.iso(e.last_seen), "article_count": int(e.article_count or 0)}


def _event_row(ev: Event, members: dict[int, list[int]]) -> dict:
    try:
        ents = json.loads(ev.entities or "[]")
    except ValueError:
        ents = []
    return {"id": ev.id, "rep_id": ev.rep_id, "title": ev.title, "n_articles": ev.n_articles, "n_sources": ev.n_sources, "has_primary": bool(ev.has_primary),
            "first_at": store.iso(ev.first_at), "last_at": store.iso(ev.last_at), "summary": ev.summary or "", "summary_model": ev.summary_model or "",
            "entities": ents, "article_ids": members.get(ev.id, [])[:200]}


def _tags(db: Session, ids: list[int]) -> dict[int, list[str]]:
    from qnapnewshub.db import ArticleEntity

    out: dict[int, list[str]] = {}
    for i in range(0, len(ids), 400):
        for aid, name in db.execute(select(ArticleEntity.article_id, Entity.name).join(Entity, Entity.id == ArticleEntity.entity_id).where(ArticleEntity.article_id.in_(ids[i:i + 400]))):
            out.setdefault(aid, []).append(name)
    return out


def feed(db: Session, *, cursor: int = 0, limit: int = LIMIT_DEFAULT, kinds: list[str] | None = None, channels: list[str] | None = None,
         categories: list[str] | None = None) -> dict:
    """{"cursor", "latest", "has_more", "reset", "changes": [{"seq", "kind", "id", "op", "data"}]}。kinds が不正なら ValueError。"""
    kinds = list(kinds or KINDS)
    if any(k not in KINDS for k in kinds):
        raise ValueError("kinds は items・entities・events です")
    cursor = max(0, int(cursor))
    limit = max(1, min(int(limit or LIMIT_DEFAULT), LIMIT_MAX))
    latest = latest_seq(db)
    rows = db.execute(select(ChangeLog.seq, ChangeLog.kind, ChangeLog.ref_id, ChangeLog.op).where(ChangeLog.seq > cursor, ChangeLog.kind.in_(kinds))
                      .order_by(ChangeLog.seq.asc()).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    ids = {k: [r[2] for r in rows if r[1] == k and r[3] == "upsert"] for k in KINDS}
    items: dict[int, NewsItem] = {}
    ents: dict[int, Entity] = {}
    evs: dict[int, Event] = {}
    for i in range(0, len(ids["items"]), 400):
        items.update({r.id: r for r in db.scalars(select(NewsItem).where(NewsItem.id.in_(ids["items"][i:i + 400])))})
    for i in range(0, len(ids["entities"]), 400):
        ents.update({r.id: r for r in db.scalars(select(Entity).where(Entity.id.in_(ids["entities"][i:i + 400])))})
    for i in range(0, len(ids["events"]), 400):
        evs.update({r.id: r for r in db.scalars(select(Event).where(Event.id.in_(ids["events"][i:i + 400])))})
    members: dict[int, list[int]] = {}
    for i in range(0, len(evs), 400):
        for eid, aid in db.execute(select(EventArticle.event_id, EventArticle.article_id).where(EventArticle.event_id.in_(list(evs)[i:i + 400])).order_by(EventArticle.article_id)):
            members.setdefault(eid, []).append(aid)
    tags = _tags(db, list(items))
    chans = [c for c in channels_mod.load()["channels"] if c["id"] in set(channels or [])] if channels else []
    cats = set(categories or [])
    out: list[dict] = []
    for seq, kind, ref, op in rows:
        if op == "delete":
            out.append({"seq": seq, "kind": kind, "id": ref, "op": "delete", "data": None})
        elif kind == "items":
            r = items.get(ref)
            if r is None:      # 取り出す前に消えた（次の変更に delete が出る）
                continue
            if cats and r.kind not in cats:
                continue
            if chans and not any(channels_mod.matches(c, kind=r.kind, source_key=r.source_key, title=r.title, summary=r.summary or "") for c in chans):
                continue
            if r.dup_of is not None:
                out.append({"seq": seq, "kind": kind, "id": ref, "op": "archive", "data": {"id": r.id, "dup_of": r.dup_of}})
            else:
                out.append({"seq": seq, "kind": kind, "id": ref, "op": "upsert", "data": _item_row(r, tags)})
        elif kind == "entities" and ref in ents:
            out.append({"seq": seq, "kind": kind, "id": ref, "op": "upsert", "data": _entity_row(ents[ref])})
        elif kind == "events" and ref in evs:
            out.append({"seq": seq, "kind": kind, "id": ref, "op": "upsert", "data": _event_row(evs[ref], members)})
    return {"cursor": rows[-1][0] if rows else cursor, "latest": latest, "has_more": more, "reset": cursor > latest, "changes": out}
