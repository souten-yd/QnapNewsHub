"""ControlDeck との連携の API（/api/v1）。

管理者（session）: GET /link（状態）・POST/DELETE /link/pairing（つなぐ番号）・DELETE /link/peers/{id}（取り消し）。
つなぐ前: POST /link/pair（番号。認証なし・総当たりの制限つき）。
つないだ ControlDeck（scope=link のトークン。`link_user` を付けた API だけが書ける）: POST /link/secret・POST /jobs/claim・POST /jobs/complete・PUT /artifacts/items/{id}/image・POST /research。
読み取り（session か任意のトークン）: GET /changes・GET /artifacts/images/{name}。
"""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from qnapnewshub import artifacts, changes, jobs, link
from qnapnewshub.auth import AuthUser, link_user, require_admin
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.db import get_news_db

router = APIRouter(tags=["link"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _raise(exc: Exception):
    status = getattr(exc, "status", 422)
    raise HTTPException(status_code=status, detail=getattr(exc, "message", str(exc))) from None


# ---- 管理者 ----

@router.get("/link")
async def link_status(user: AuthUser = Depends(require_admin), db: Session = Depends(get_news_db)):
    """つなぐ画面: 公開鍵の指紋・番号の状態・つないだ相手・受け取った鍵の名前・仕事の数（値は出さない）。"""
    ident = await asyncio.to_thread(link.hub_identity)
    return {"fingerprint": ident["fingerprint"], **await asyncio.to_thread(link.summary), "jobs": jobs.summary(db), "allowed_secrets": sorted(link.ALLOWED_SECRETS)}


@router.post("/link/pairing")
async def start_pairing(user: AuthUser = Depends(require_admin)):
    return await asyncio.to_thread(link.start_pairing, user.id)


@router.delete("/link/pairing")
async def cancel_pairing(user: AuthUser = Depends(require_admin)):
    link.cancel_pairing()
    return {"ok": True}


@router.delete("/link/peers/{peer_id}")
async def revoke_peer(peer_id: int, user: AuthUser = Depends(require_admin)):
    if not await asyncio.to_thread(link.revoke_peer, peer_id):
        raise HTTPException(status_code=404, detail="その相手はありません（取り消し済みかもしれません）")
    return {"ok": True}


# ---- つなぐ ----

class PairBody(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=60)
    public_key: str = Field(min_length=40, max_length=60)


@router.post("/link/pair")
async def pair(body: PairBody, request: Request):
    """ControlDeck がつなぐ番号で呼ぶ。認証なし（番号が認証）。間違いは同じ文言で、番号ごと・IP ごとに数える。"""
    try:
        return await asyncio.to_thread(link.pair, body.code, body.name, body.public_key, _ip(request))
    except link.LinkError as exc:
        _raise(exc)


class SecretBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    sealed: dict


@router.post("/link/secret")
async def secret(body: SecretBody, user: AuthUser = Depends(link_user)):
    """封じた鍵（ECIES）を受け取って secretbox へ。応答に値は含まない。"""
    try:
        peer = await asyncio.to_thread(link.peer_of_token, user.token_id)
        return await asyncio.to_thread(link.receive_secret, peer, body.name, body.sealed)
    except link.LinkError as exc:
        _raise(exc)


# ---- 差分 ----

_LIST = re.compile(r"^[\w\-,.]{0,400}$")


@router.get("/changes")
def get_changes(cursor: int = 0, limit: int = 500, kinds: str = "", channels: str = "", categories: str = "", user: AuthUser = Depends(get_current_user),
                db: Session = Depends(get_news_db)):
    """保存済みの変更だけ（cursor より後）を cursor の順に。cursor は返った値を次に渡す。"""
    for value in (kinds, channels, categories):
        if not _LIST.match(value):
            raise HTTPException(status_code=422, detail="絞り込みの書き方が正しくありません（カンマ区切り）")
    split = lambda v: [x for x in v.split(",") if x]  # noqa: E731
    try:
        return changes.feed(db, cursor=cursor, limit=limit, kinds=split(kinds) or None, channels=split(channels) or None,
                            categories=split(categories) or None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# ---- 仕事 ----

class ClaimBody(BaseModel):
    kinds: list[str] = Field(default_factory=lambda: list(jobs.KINDS), max_length=8)
    limit: int = Field(default=10, ge=1, le=jobs.CLAIM_MAX)
    lease_seconds: int = Field(default=jobs.LEASE_DEFAULT, ge=jobs.LEASE_MIN, le=jobs.LEASE_MAX)


@router.post("/jobs/claim")
async def claim(body: ClaimBody, user: AuthUser = Depends(link_user)):
    try:
        return await asyncio.to_thread(jobs.claim, user.token_id, body.kinds, body.limit, body.lease_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class JobResult(BaseModel):
    job_id: int
    status: str = Field(default="done", pattern="^(done|failed)$")
    result: dict | None = None
    model: str = Field(default="", max_length=200)
    error: str = Field(default="", max_length=300)

    @field_validator("result")
    @classmethod
    def _size(cls, v):  # noqa: ANN001, ANN206
        import json

        if v is not None and len(json.dumps(v)) > jobs.RESULT_MAX:
            raise ValueError("結果が大きすぎます")
        return v


class CompleteBody(BaseModel):
    results: list[JobResult] = Field(min_length=1, max_length=jobs.CLAIM_MAX)


@router.post("/jobs/complete")
async def complete(body: CompleteBody, user: AuthUser = Depends(link_user)):
    """結果を返す（冪等: 終わった仕事への 2 回目は duplicate）。結果は種類ごとに検査して取り込む。"""
    return await asyncio.to_thread(jobs.complete, user.token_id, [r.model_dump() for r in body.results])


# ---- 生成物・調査 ----

@router.put("/artifacts/items/{item_id}/image")
async def put_image(item_id: int, request: Request, replace: bool = False, user: AuthUser = Depends(link_user)):
    """記事の画像を ControlDeck の生成物に差し替える。本文は画像そのもの（Content-Type: image/webp・png・jpeg。512KB まで）。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > artifacts.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="画像は 512KB までです")
    data = bytearray()
    async for chunk in request.stream():      # 申告より多く送られても、上限を超えたところで止める
        data += chunk
        if len(data) > artifacts.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="画像は 512KB までです")
    try:
        peer = await asyncio.to_thread(link.peer_of_token, user.token_id)
        return await asyncio.to_thread(artifacts.store_image, item_id, bytes(data), request.headers.get("content-type", ""), peer, replace)
    except artifacts.ArtifactError as exc:
        _raise(exc)


@router.get("/artifacts/images/{name}")
async def get_image(name: str, user: AuthUser = Depends(get_current_user)):
    found = await asyncio.to_thread(artifacts.image_path, name)
    if found is None:
        raise HTTPException(status_code=404, detail="見つかりません")
    path, media = found
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


class Source(BaseModel):
    title: str = Field(default="", max_length=artifacts.LIMITS["title"])
    url: str = Field(default="", max_length=artifacts.LIMITS["url"], pattern=r"^(https?://\S*)?$")


class ResearchBody(BaseModel):
    theme: str = Field(min_length=1, max_length=artifacts.LIMITS["theme"])
    sources: list[Source] = Field(default_factory=list, max_length=artifacts.LIMITS["sources"])
    facts: list[str] = Field(default_factory=list, max_length=artifacts.LIMITS["facts"])
    conclusion: str = Field(default="", max_length=artifacts.LIMITS["conclusion"])

    @field_validator("facts")
    @classmethod
    def _facts(cls, v: list[str]):  # noqa: ANN206
        if any(len(f) > artifacts.LIMITS["fact"] for f in v):
            raise ValueError("確かめた事実は 1 つ 500 文字までです")
        return v


@router.post("/research")
async def research(body: ResearchBody, user: AuthUser = Depends(link_user)):
    peer = await asyncio.to_thread(link.peer_of_token, user.token_id)
    return await asyncio.to_thread(artifacts.save_research, peer, body.theme, [s.model_dump() for s in body.sources], body.facts, body.conclusion)

