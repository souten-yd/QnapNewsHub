"""ニュースハブの FastAPI アプリ（1 プロセス: API・画面の配信・取得の見張り）。画面は frontend/dist（./hub.sh build）。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse

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


def web_dir() -> Path | None:
    """build した画面の置き場（NEWSHUB_WEB か、リポジトリの frontend/dist）。index.html が無ければ None（API だけで動く）。"""
    raw = os.environ.get("NEWSHUB_WEB", "").strip()
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    root = root.resolve()
    return root if (root / "index.html").is_file() else None


_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def _mount_web(app: FastAPI, root: Path) -> None:
    """画面の静的なファイルを `/` 以下で配る。ファイルが無い道は index.html（画面の中の道）。/api は含めない。
    パスは resolve() して root の中か確かめる（`..` で外へ出さない）。"""
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="見つかりません")
        target = (root / path).resolve() if path else index
        if target != root and root not in target.parents:
            raise HTTPException(status_code=404, detail="見つかりません")
        if path.startswith("assets/"):      # build のファイル名はハッシュつき: 長く覚えさせてよい
            if not target.is_file():
                raise HTTPException(status_code=404, detail="見つかりません")
            return FileResponse(target, headers={**_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"})
        if target.is_file():
            return FileResponse(target, headers={**_HEADERS, "Cache-Control": "no-cache"})
        return FileResponse(index, headers={**_HEADERS, "Cache-Control": "no-cache"})


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
    root = web_dir()
    if root is not None:
        _mount_web(app, root)
    return app


app = create_app()
