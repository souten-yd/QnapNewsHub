"""ニュースハブの FastAPI アプリ（1 プロセス: API・画面の配信・取得の見張り）。画面は frontend/dist（./hub.sh build）。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse

from qnapnewshub import __version__, maintenance, analytics, analytics_router, auth, auth_router, diag_router, entities_router, events_router, connectors_router, retrieval_router, kg_router, link_router, logbuf, metrics, router, service, settings, update_router, updater, writer

logger = logging.getLogger("qnapnewshub.main")


async def sampler() -> None:
    """5 分ごとに、プロセスの I/O・CPU・メモリを 1 点とる（診断。読むだけ）。日が変わったら前日の要約を 1 回だけ保存する。"""
    while True:
        try:
            await asyncio.to_thread(metrics.sample)
            await asyncio.to_thread(metrics.flush_daily)
        except Exception:  # noqa: BLE001
            logger.exception("診断の数値をとれませんでした")
        await asyncio.sleep(metrics.SAMPLE_SECONDS)


async def _analytics_ready() -> None:
    """起動の後、集計の表が無い（初回・移行）なら作る。失敗しても API は動く（読むとき再び試す）。"""
    try:
        await asyncio.to_thread(analytics.ensure)
    except Exception:  # noqa: BLE001
        logger.exception("分析の集計の表を用意できませんでした")


async def _tiers_ready() -> None:
    """起動の後、既存のニュースの文書を L1〜L3 の段に当てはめる（冪等。済んでいれば何も上がらない）。失敗しても API は動く。"""
    from qnapnewshub import db as news_db, docstore

    def do() -> int:
        with news_db.session() as db:
            n = docstore.migrate(db)
            db.commit()
            return n

    try:
        await asyncio.to_thread(writer.run, "news.tiers", do, user=False)
    except Exception:  # noqa: BLE001
        logger.exception("文書の段の移行に失敗しました")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logbuf.install(file_log=bool(settings.load()["storage"]["log_file"]))
    metrics.sample()
    token = await asyncio.to_thread(auth.ensure_setup_token)
    if token:      # 番号そのものはログに出さない（置き場のファイルと、QPKG なら QTS のシステムのログで見る）
        logger.warning("最初の管理者を作るには一回限りの番号が要ります（データの置き場の %s）", auth.SETUP_TOKEN_FILE)
    tasks = [asyncio.create_task(sampler()), asyncio.create_task(_analytics_ready()), asyncio.create_task(_tiers_ready())]
    if os.environ.get("NEWSHUB_AUTO") != "0":
        tasks.append(asyncio.create_task(service.news_loop()))
        tasks.append(asyncio.create_task(updater.loop()))
        tasks.append(asyncio.create_task(maintenance.loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 正常な停止（SIGTERM など）: バッファを書いてから終わる
        info = await asyncio.to_thread(writer.shutdown)
        logger.info("停止: 未保存の分を保存しました %s", {k: info.get(k) for k in ("items", "updates", "patches", "fetchlog", "ok")} if info else "（なし）")


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
    api.include_router(entities_router.router)
    api.include_router(events_router.router)
    api.include_router(kg_router.router)
    api.include_router(kg_router.graph)
    api.include_router(diag_router.router)
    api.include_router(update_router.router)
    api.include_router(analytics_router.router)
    api.include_router(link_router.router)
    api.include_router(connectors_router.router)
    api.include_router(connectors_router.documents)
    api.include_router(retrieval_router.router)
    app.include_router(api)
    root = web_dir()
    if root is not None:
        _mount_web(app, root)
    return app


app = create_app()
