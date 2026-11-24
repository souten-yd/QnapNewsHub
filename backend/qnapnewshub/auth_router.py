"""認証の API（/api/v1/auth）。状態・最初の管理者・ログイン・ログアウト・API トークン（ControlDeck 用。作る・一覧・取り消す）。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from qnapnewshub import auth, hubconf

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=300, repr=False)


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@router.get("/status")
async def status(request: Request):
    """画面が最初に見る（認証なし）: 管理者を作る必要があるか・ログイン中か。利用者名はログイン中の本人にだけ返す。"""
    have = await asyncio.to_thread(auth.has_users)
    user = None
    cookie = request.cookies.get(auth.COOKIE, "")
    if cookie:
        user = await asyncio.to_thread(auth.session_user, cookie)
    return {"setup_needed": not have, "authenticated": user is not None, "username": user.username if user else None}


@router.post("/setup")
async def setup(body: Credentials, request: Request, response: Response):
    """最初の管理者を作ってそのままログインする（利用者がいる間は 409）。"""
    try:
        await asyncio.to_thread(auth.create_admin, body.username, body.password)
    except PermissionError:
        raise HTTPException(status_code=409, detail="管理者はすでにいます") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _login(body, request, response)


async def _login(body: Credentials, request: Request, response: Response):
    try:
        token, user = await asyncio.to_thread(auth.login, body.username, body.password, _ip(request))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    auth.set_cookie(response, token, hubconf.load()["cookie_secure"])
    return {"username": user.username}


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response):
    return await _login(body, request, response)


@router.post("/logout")
async def logout(request: Request, response: Response, user: auth.AuthUser = Depends(auth.current_user)):
    cookie = request.cookies.get(auth.COOKIE, "")
    if cookie:
        await asyncio.to_thread(auth.logout, cookie)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/tokens")
async def tokens(user: auth.AuthUser = Depends(auth.require_admin)):
    return {"tokens": await asyncio.to_thread(auth.list_tokens)}


@router.post("/tokens")
async def new_token(body: TokenBody, user: auth.AuthUser = Depends(auth.require_admin)):
    """ControlDeck 用の API トークンを作る（読み取り専用）。本体はこの応答でだけ見える（保存はハッシュ）。"""
    try:
        return await asyncio.to_thread(auth.create_token, user.id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/tokens/{token_id}")
async def revoke(token_id: int, user: auth.AuthUser = Depends(auth.require_admin)):
    if not await asyncio.to_thread(auth.revoke_token, token_id):
        raise HTTPException(status_code=404, detail="そのトークンはありません（取り消し済みかもしれません）")
    return {"ok": True}


class HubSettings(BaseModel):
    host: str = Field(default="127.0.0.1", max_length=20)
    port: int = Field(default=8787, ge=1, le=65535)
    cookie_secure: bool = False


@router.get("/hub")
async def get_hub(user: auth.AuthUser = Depends(auth.require_admin)):
    """待ち受けの設定（変更は再起動で効く）。"""
    return hubconf.load()


@router.put("/hub")
async def put_hub(body: HubSettings, user: auth.AuthUser = Depends(auth.require_admin)):
    from qnapnewshub.sources import ConfigError

    try:
        return {**hubconf.save(body.model_dump()), "restart_needed": True}
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
