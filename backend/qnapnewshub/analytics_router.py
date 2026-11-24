"""分析の API（/api/v1/analytics）。ログインした利用者か、読み取りの API トークン（GET だけ）。集計の表だけを読む。作り直しは管理者だけ。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from qnapnewshub import analytics, trend
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import require_admin
from qnapnewshub.db import get_news_db

router = APIRouter(prefix="/analytics", tags=["analytics"])
RANGE = "^(24h|7d|30d|90d)$"


def _ready(_user: User = Depends(get_current_user)) -> None:
    analytics.ensure()


@router.get("/filters")
def filters(user: User = Depends(get_current_user)):
    return analytics.filters_info()


@router.get("/overview", dependencies=[Depends(_ready)])
def overview(kind: str = "", lang: str = "", db: Session = Depends(get_news_db)):
    return analytics.overview(db, kind, lang)


@router.get("/series", dependencies=[Depends(_ready)])
def series(range: str = Query("7d", pattern=RANGE), grain: str = Query("", pattern="^(|hour|day)$"), by: str = Query("none", pattern="^(none|kind|channel|source|lang|origin)$"),
           kind: str = "", lang: str = "", metric: str = Query("all", pattern="^(all|indep)$"), db: Session = Depends(get_news_db)):
    return analytics.series(db, range_=range, grain=grain, by=by, kind=kind, lang=lang, metric=metric)


@router.get("/hot-topics", dependencies=[Depends(_ready)])
def hot_topics(kind: str = "", lang: str = "", db: Session = Depends(get_news_db)):
    return analytics.hot_topics(db, kind, lang)


@router.get("/sources", dependencies=[Depends(_ready)])
def sources(range: str = Query("7d", pattern=RANGE), kind: str = "", lang: str = "", db: Session = Depends(get_news_db)):
    return analytics.sources_analysis(db, range_=range, kind=kind, lang=lang)


@router.get("/compare", dependencies=[Depends(_ready)])
def compare(by: str = Query("kind", pattern="^(kind|channel|origin|lang|source)$"), kind: str = "", lang: str = "", db: Session = Depends(get_news_db)):
    return analytics.compare(db, by=by, kind=kind, lang=lang)


@router.get("/trend", dependencies=[Depends(_ready)])
def trend_stats(db: Session = Depends(get_news_db)):
    """新着の傾向: 数字（集計の表だけ）と、LLM の「今日の傾向」の解説（無ければ null）。"""
    out = dict(analytics.trend_stats(db))
    out["note"] = trend.latest(db)
    return out


@router.post("/rebuild")
async def rebuild(user: User = Depends(require_admin)):
    """既存の記事から集計の表を作り直す（管理者）。"""
    return await asyncio.to_thread(analytics.rebuild, "manual")
