"""ニュースハブの FastAPI アプリ（1 プロセス: API・取得の見張り）。画面は 2 段で足す。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from qnapnewshub import __version__, auth_router, router, service

logger = logging.getLogger("qnapnewshub.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if os.environ.get("NEWSHUB_AUTO") != "0":
        task = asyncio.create_task(service.news_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    app = FastAPI(title="QNAP News Hub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api = APIRouter(prefix="/api/v1")

    @api.get("/health")
    async def health():
        """生きているか（認証なし。中身は出さない）。"""
        return {"status": "ok", "version": __version__}

    api.include_router(auth_router.router)
    api.include_router(router.router)
    app.include_router(api)
    return app


app = create_app()
