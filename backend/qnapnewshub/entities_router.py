"""物（entities）の API（/api/v1/entities）。ログインした利用者が読む。読むだけ（書き込みは Write Coordinator のまとめ書きだけ）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from qnapnewshub import entities
from qnapnewshub.kg_router import scopes_for
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.db import get_news_db

router = APIRouter(prefix="/entities", tags=["entities"])


@router.get("")
def list_entities(q: str = Query("", max_length=100), type: str = Query("", max_length=16), days: int = Query(0, ge=0, le=3650),
                  sort: str = Query("count", pattern="^(count|recent)$"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
                  confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """物の一覧。q（名前・別名の部分一致）・type（種類）・days（その日数のうちに記事が出た物）・sort（count＝記事の数・recent＝最近出た順）。"""
    return entities.list_entities(db, q=q, kind=type, days=days, sort=sort, limit=limit, offset=offset, scopes=scopes_for(user, confidential))


@router.get("/{entity_id}")
def get_entity(entity_id: int, confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """物のページ: 記事の年表・facts（ai_extracted は AI の取り出しの印）・一緒に出る物の上位。"""
    found = entities.detail(db, entity_id, scopes=scopes_for(user, confidential))
    if found is None:
        raise HTTPException(status_code=404, detail="その物は見つかりません")
    return found
