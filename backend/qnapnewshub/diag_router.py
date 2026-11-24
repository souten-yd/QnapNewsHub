"""診断の API（/api/v1/diagnostics）。管理者だけ（一行の知らせの notice だけはログインした人に、文言だけ返す）。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from qnapnewshub import diagnostics, rest
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import require_admin
from qnapnewshub.db import get_news_db

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("")
def get_diagnostics(user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    return diagnostics.build(db)


@router.get("/notice")
def get_notice(user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """ホームの上の一行（問題が無ければ message は null）。管理者以外には出さない（取得先の名前などを見せない）。"""
    return diagnostics.notice(db)


@router.post("/selfcheck")
async def run_selfcheck(user: User = Depends(require_admin)):
    return await asyncio.to_thread(diagnostics.selfcheck)


@router.get("/bundle")
def get_bundle(user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    body = json.dumps(diagnostics.bundle(db), ensure_ascii=False, indent=1)
    return Response(body, media_type="application/json", headers={"Content-Disposition": 'attachment; filename="newshub-diagnostics.json"'})


@router.post("/flush")
async def flush_now(user: User = Depends(require_admin)):
    """「いますぐ保存」（利用者の操作。お休み中でも書く）。"""
    from qnapnewshub import writer

    return await asyncio.to_thread(writer.flush, "manual")


@router.get("/rest")
def get_rest(user: User = Depends(get_current_user)):
    return rest.state()


@router.post("/search/rebuild")
async def rebuild_search(user: User = Depends(require_admin)):
    """検索の索引を空にして、作り直しの仕事を予約する（入れるのは 1 分ごとの巡回が 2 万件ずつ。お休み中は進めない）。"""
    from qnapnewshub import db as news_db, searchindex, writer

    def do():
        with news_db.engine().begin() as conn:
            return searchindex.request_rebuild(conn)

    top = await asyncio.to_thread(writer.run, "news.index", do)
    return {"queued": True, "articles": top}
