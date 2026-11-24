"""イベントの API（/api/v1/events）。ログインした利用者か読み取りの API トークン（GET だけ）。読むだけ（書き込みは Write Coordinator のまとめ書きだけ）。作り直しは管理者だけ。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from qnapnewshub import events
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import require_admin
from qnapnewshub.db import get_news_db

router = APIRouter(prefix="/events", tags=["events"])


def _ready(db: Session = Depends(get_news_db), _user: User = Depends(get_current_user)) -> None:
    events.ensure(db)


@router.get("", dependencies=[Depends(_ready)])
def list_events(sort: str = Query("recent", pattern="^(recent|size|first)$"), min_articles: int = Query(2, ge=2, le=100), primary: bool = False, days: int = Query(7, ge=0, le=60),
                entity: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), db: Session = Depends(get_news_db)):
    """イベントの一覧。sort（recent＝最終の記事が新しい順・size＝記事の多い順・first＝初報が新しい順）・days（最終の記事がその日数以内。0 で全期間）・primary（一次情報のあるものだけ）・entity（物の id）。"""
    return events.list_events(db, sort=sort, min_articles=min_articles, primary=primary, days=days, entity_id=entity, limit=limit, offset=offset)


@router.get("/{event_id}", dependencies=[Depends(_ready)])
def get_event(event_id: int, db: Session = Depends(get_news_db)):
    found = events.detail(db, event_id)
    if found is None:
        raise HTTPException(status_code=404, detail="そのイベントは見つかりません")
    return found


@router.post("/rebuild")
async def rebuild(user: User = Depends(require_admin)):
    """最近の記事からイベントを全部作り直す（管理者）。要約は引き継ぐ。"""
    return await asyncio.to_thread(events.rebuild, "manual")
