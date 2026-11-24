"""保守の仕事（1 分ごとの巡回）: 検索の索引の作り直し・古い記事の抜粋の圧縮・本文の取り出し。

- 書くのは必ず Write Coordinator の書き手（writer.run）の 1 トランザクション単位（まとめ書き）。お休み中（rest.blocks_analysis）は何もしない。
- 索引の作り直しは 1 回 2 万件・圧縮は 1 回 2,000 件。終わるまで次の巡回で続ける（待機中の CPU・書き込みは、仕事が無ければ 0）。
- 本文の取り出しは bodies.run_once（1 分の上限・1 日の上限・robots）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from qnapnewshub import bodies, rest, searchindex, settings, writer, ztext
from qnapnewshub import db as news_db
from qnapnewshub.db import NewsCold, NewsItem

logger = logging.getLogger("qnapnewshub.maintenance")

COMPACT_ROWS = 2000
TICK_SECONDS = 60
_UTC = timezone.utc


def index_step(rows: int = searchindex.BATCH_ROWS) -> dict:
    """索引の作り直しを 1 バッチ進める（書き手のスレッドで 1 トランザクション）。作り直し中でなければ何もしない（書かない）。"""
    with news_db.engine().connect() as conn:
        if not searchindex.has_index(conn) or not searchindex.progress(conn)["building"]:
            return {"done": 0, "building": False}

    def do() -> dict:
        with news_db.engine().begin() as conn:
            got = searchindex.backfill_batch(conn, rows)
        writer.get().checkpoint_if_large()      # 作り直しの途中で WAL を膨らませない
        return got

    return writer.run("news.index", do, user=False)


def compact_step(years: int | None = None, rows: int = COMPACT_ROWS, now: datetime | None = None) -> dict:
    """years 年より古い記事の抜粋を圧縮して news_cold へ移し、索引からも外す（1 バッチ・1 トランザクション）。対象が無ければ書かない。"""
    years = settings.load()["storage"]["compress_years"] if years is None else years
    if years <= 0:
        return {"done": 0}
    cutoff = ((now or datetime.now(_UTC)) - timedelta(days=365 * years)).replace(tzinfo=None)
    with news_db.session() as db:
        if db.scalar(select(NewsItem.id).where(NewsItem.cold == 0, NewsItem.summary != "", NewsItem.kind != "event", NewsItem.published_at < cutoff).limit(1)) is None:
            return {"done": 0}

    def do() -> dict:
        with news_db.session() as db:
            rows_ = db.execute(select(NewsItem.id, NewsItem.summary).where(NewsItem.cold == 0, NewsItem.summary != "", NewsItem.kind != "event",
                                                                          NewsItem.published_at < cutoff).order_by(NewsItem.id).limit(rows)).all()
            for item_id, summary in rows_:
                db.merge(NewsCold(item_id=item_id, z=ztext.pack(summary), raw_len=len(summary.encode("utf-8"))))
                db.execute(text("UPDATE news_items SET summary = '', cold = 1 WHERE id = :i"), {"i": item_id})      # トリガーが索引から抜粋を外す
            db.commit()
            return {"done": len(rows_)}

    return writer.run("news.compact", do, user=False)


async def tick() -> dict:
    """1 回の巡回。{"index", "compact", "bodies"}。"""
    out: dict = {}
    if not rest.blocks_analysis():
        out["index"] = await asyncio.to_thread(index_step)
        out["compact"] = await asyncio.to_thread(compact_step)
    out["bodies"] = await bodies.run_once()
    return out


async def loop() -> None:
    await asyncio.sleep(90)
    while True:
        if os.environ.get("NEWSHUB_AUTO") != "0":
            try:
                got = await tick()
                if got.get("bodies", {}).get("tried"):
                    logger.info("本文を取り出しました: %s", {k: got["bodies"][k] for k in ("tried", "ok", "failed", "deferred")})
            except Exception:  # noqa: BLE001 - 1 回の失敗で巡回を止めない
                logger.exception("保守の巡回に失敗しました")
        await asyncio.sleep(TICK_SECONDS)
