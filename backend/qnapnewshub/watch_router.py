"""ウォッチリストと変化の要約・条件つきの数値の比較の API（/api/v1/watch・/api/v1/metrics）。

- 読む API は session か Bearer トークン（read・link。LLM が引く）。区分は kg_router.scopes_for（既定 public）。**読むだけで DB は変わらない**（前回見た時刻も進めない）。
- ウォッチリストの追加・削除・「見た」だけ管理者（require_admin）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qnapnewshub import bench, watch, writer
from qnapnewshub import db as news_db
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import read_user, require_admin
from qnapnewshub.db import get_news_db
from qnapnewshub.kg_router import scopes_for

router = APIRouter(prefix="/watch", tags=["watch"])
metrics_router = APIRouter(prefix="/metrics", tags=["metrics"])


class WatchIn(BaseModel):
    entity_id: int = Field(ge=1)


class SeenIn(BaseModel):
    entity_ids: list[int] | None = Field(None, max_length=watch.WATCH_MAX)


@router.get("")
def list_watch(confidential: bool = False, sensitivity: list[str] | None = Query(None), user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    return {"items": watch.listing(db, scopes_for(user, confidential, sensitivity))}


@router.post("")
async def add_watch(body: WatchIn, user: User = Depends(require_admin)):
    scopes = ["public", "confidential"]

    def do() -> dict:
        with news_db.session() as db:
            try:
                row = watch.add(db, body.entity_id, scopes)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
            if row is None:
                raise HTTPException(status_code=404, detail="その Things は見つかりません")
            db.commit()
            return row

    return await asyncio.to_thread(writer.run, "news.watch.add", do)


@router.delete("/{entity_id}")
async def remove_watch(entity_id: int, user: User = Depends(require_admin)):
    def do() -> dict:
        with news_db.session() as db:
            ok = watch.remove(db, entity_id)
            db.commit()
            return {"removed": ok}

    return await asyncio.to_thread(writer.run, "news.watch.remove", do)


@router.post("/seen")
async def seen(body: SeenIn, user: User = Depends(require_admin)):
    """前回見た時刻を今に進める（entity_ids を省くと全部）。次の変化の要約はここからの差になる。"""
    def do() -> dict:
        with news_db.session() as db:
            n = watch.mark_seen(db, body.entity_ids)
            db.commit()
            return {"marked": n}

    return await asyncio.to_thread(writer.run, "news.watch.seen", do)


@router.get("/changes")
def get_changes(since: str = Query("", max_length=40), thing: str = Query("", max_length=200), format: str = Query("json", pattern="^(json|text)$"),
                confidential: bool = False, sensitivity: list[str] | None = Query(None), user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """前回から何が変わったか。thing（id か名前。カンマで最大 5）を省くと監視の物ぜんぶ（起点は各自の「前回見た」・since で上書き）。thing を指定すれば監視でなくても引ける（起点は since・既定 7 日前）。
    format=text は LLM に渡す短い Markdown（出どころの印つき）。"""
    scopes = scopes_for(user, confidential, sensitivity)
    now = watch._now()  # noqa: SLF001
    try:
        cutoff = watch.parse_since(since, None, now)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    ids = None
    if thing.strip():
        ids = [e.id for e in bench.resolve_things(db, thing, scopes)]
        if not ids:
            raise HTTPException(status_code=404, detail="その Things は見つかりません")
    out = watch.changes(db, scopes, since=cutoff, entity_ids=ids, now=now)
    return PlainTextResponse(watch.text(out)) if format == "text" else out


@metrics_router.get("/compare")
def compare(thing: str = Query(min_length=1, max_length=200), metric: str = Query("", max_length=60), cond: str = Query("", max_length=200),
            format: str = Query("json", pattern="^(json|text)$"), confidential: bool = False, sensitivity: list[str] | None = Query(None),
            user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """条件つきの数値の比較。thing は id か名前（カンマで最大 5）・metric は指標（kind・単位・文の語。例 tok/s・benchmark）・cond は `hw:R9700,quant:Q4_K_M`。
    条件の違う数値は平均しない（組ごとに最新・最小・最大・点）。"""
    scopes = scopes_for(user, confidential, sensitivity)
    ents = bench.resolve_things(db, thing, scopes)
    if not ents:
        raise HTTPException(status_code=404, detail="その Things は見つかりません")
    out = bench.compare(db, ents, scopes, metric=metric, cond=cond)
    return PlainTextResponse(bench.compare_text(out)) if format == "text" else out
