"""翻訳の API（/api/v1/translate）。モデルの取得・消去・状態は管理者だけ。記事の翻訳の読み・要求はログインした利用者。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from qnapnewshub import settings, translate, translate_flow
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import require_admin
from qnapnewshub.db import get_news_db

router = APIRouter(prefix="/translate", tags=["translate"])


def status_dict(db: Session | None = None) -> dict:
    cfg = settings.load()["translate"]
    out = {"engine": cfg["engine"], "model": {"id": translate.MODEL["id"], "title": translate.MODEL["title"], "repo": translate.MODEL["repo"], "rev": translate.MODEL["rev"][:8],
                                              "license": translate.MODEL["license"], "license_note": translate.MODEL["license_note"],
                                              "size": sum(s for _, s in translate.MODEL["files"].values()), "installed": translate.installed(), "loaded": translate.engine_loaded()},
           "download": translate.download_state(), "stats": translate.stats(), "untranslated_mark": translate.UNTRANSLATED,
           "default_glossary": translate.DEFAULT_GLOSSARY}
    if db is not None:
        out["counts"] = translate.counts(db)
        out["llm_state"] = translate_flow.llm_state(db, cfg)
    return out


@router.get("/status")
def get_status(user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    return status_dict(db)


@router.post("/model")
async def fetch_model(user: User = Depends(require_admin)):
    """モデルの取得を始める（利用者の操作）。取得は裏で進み、進み具合は /status の download。"""
    if translate.installed():
        raise HTTPException(status_code=409, detail="すでに入っています")
    if not translate.start_download():
        raise HTTPException(status_code=409, detail="取得の途中です")
    return {"started": True, "download": translate.download_state()}


@router.delete("/model")
async def delete_model(user: User = Depends(require_admin)):
    if translate.download_state()["phase"] == "downloading":
        raise HTTPException(status_code=409, detail="取得の途中です")
    return {"removed": await asyncio.to_thread(translate.remove_model)}


@router.post("/try")
async def try_text(body: dict, user: User = Depends(require_admin)):
    """試し訳（設定の画面。500 文字まで・保存しない）。"""
    text = str(body.get("text") or "").strip()[:500]
    cfg = settings.load()["translate"]
    engine = await asyncio.to_thread(translate.get_engine, cfg)
    if engine is None:
        raise HTTPException(status_code=409, detail="翻訳のモデルが入っていません")

    def run():
        with translate.run_lock:
            ctx = translate.Ctx([], cfg)
            ja, units, fail = translate.translate_text(text, ctx, engine, {})
            return {"ja": ja, "units": units, "failed": fail}
    return await asyncio.to_thread(run)


@router.get("/articles/{item_id}")
def get_article(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    row = translate.get_row(db, item_id)
    if not row or row.get("kind") == "ja":
        return {"translated": False}
    return {"translated": True, "title_ja": row.get("title_ja", ""), "summary_ja": row.get("summary_ja", ""), "body_ja": row.get("body_ja", ""), "engine": row.get("engine", ""),
            "hq": row.get("kind") == "hq", "partial": int(row.get("n_fail") or 0) > 0, "untranslated_mark": translate.UNTRANSLATED}


@router.post("/articles/{item_id}")
async def request_article(item_id: int, user: User = Depends(get_current_user)):
    """記事を開いたとき: 本文（なければ題・概要）を訳す。時間がかかる（本文 8,000 文字で QNAP では約 15 秒の見込み）。"""
    try:
        got = await asyncio.to_thread(translate_flow.ondemand, item_id)
    except translate_flow.NotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = got["row"]
    return {"translated": True, "title_ja": row.get("title_ja", ""), "summary_ja": row.get("summary_ja", ""), "body_ja": row.get("body_ja", ""), "engine": row.get("engine", ""),
            "hq": row.get("kind") == "hq", "partial": int(row.get("n_fail") or 0) > 0, "untranslated_mark": translate.UNTRANSLATED}
