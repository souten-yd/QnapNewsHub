"""更新の API（/api/v1/update）。状態の読み取りはログインした人、確認・設定・更新・戻すは管理者（session）だけ。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from qnapnewshub import updater
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import require_admin

router = APIRouter(prefix="/update", tags=["update"])


class SettingsBody(BaseModel):
    include_prerelease: bool


class VersionBody(BaseModel):
    version: str = Field(min_length=1, max_length=60)


@router.get("")
def get_status(user: User = Depends(get_current_user)):
    return updater.status()


@router.get("/notice")
def get_notice(user: User = Depends(get_current_user)):
    return updater.notice()


@router.post("/check")
async def check_now(user: User = Depends(require_admin)):
    return await updater.check(manual=True)


@router.put("/settings")
async def put_settings(body: SettingsBody, user: User = Depends(require_admin)):
    await asyncio.to_thread(updater.set_include_prerelease, body.include_prerelease)
    return updater.status()


@router.post("/install")
async def install(body: VersionBody, user: User = Depends(require_admin)):
    try:
        return updater.start_update(body.version)
    except updater.UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/rollback")
async def rollback(body: VersionBody, user: User = Depends(require_admin)):
    try:
        return updater.start_rollback(body.version)
    except updater.UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
