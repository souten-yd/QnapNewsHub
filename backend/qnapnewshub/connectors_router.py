"""connector・文書の段・Metric の API（/api/v1/connectors・/api/v1/documents）。

- connector の一覧・設定・鍵・手動の実行は**管理者だけ**（利用条件・予算・鍵の有無・取得先の設定が見えるため）。鍵の値は返さない（has_key だけ）。
- 文書の一覧・検索・Metric の読みはログインした利用者か読み取りのトークン（public の文書だけ。confidential は管理者のログインだけ）。段を上げるのは管理者だけ。
- 書き込み（設定・鍵）は writer を通す（asyncio.to_thread）。取得の結果は Write Coordinator のまとめ書き（ここでは書かない）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qnapnewshub import connectors, docstore, kg, rest, writer
from qnapnewshub import db as news_db
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user, require_admin
from qnapnewshub.db import Document, DocumentLink, get_news_db

router = APIRouter(prefix="/connectors", tags=["connectors"])
documents = APIRouter(prefix="/documents", tags=["documents"])


def _spec(cid: str) -> connectors.Spec:
    if cid not in connectors.SPECS:
        raise HTTPException(status_code=404, detail="知らない connector です")
    return connectors.SPECS[cid]


@router.get("")
async def list_connectors(user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    def build() -> dict:
        return {"connectors": connectors.describe_all(), "kinds": connectors.KINDS, "promotion": connectors.promotion_config(), "tiers": docstore.tier_counts(db),
                "tier_labels": {str(k): v for k, v in docstore.TIER_LABELS.items()}, "rest": rest.state()["active"],
                "limits": {"interval": [connectors.INTERVAL_MIN, connectors.INTERVAL_MAX], "daily_items": connectors.MAX_ITEMS_LIMIT, "daily_mb": connectors.MAX_MB_LIMIT}}

    return await asyncio.to_thread(build)


class PromotionIn(BaseModel):
    channel: bool | None = None
    hot: bool | None = None
    watch: bool | None = None
    surge: bool | None = None
    derivative: bool | None = None
    surge_stars: int | None = Field(None, ge=1, le=1_000_000)


@router.put("/promotion")
async def put_promotion(body: PromotionIn, user: User = Depends(require_admin)):
    def do() -> dict:
        cfg = connectors.load()
        cfg = {**cfg, "promotion": {**cfg["promotion"], **{k: v for k, v in body.model_dump().items() if v is not None}}}
        return connectors.save(cfg)["promotion"]

    try:
        return {"promotion": await asyncio.to_thread(do)}
    except connectors.ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ConnectorIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    daily_items: int | None = None
    daily_mb: int | None = None
    settings: dict | None = None
    terms_ack: bool | None = None


@router.put("/{cid}")
async def put_connector(cid: str, body: ConnectorIn, user: User = Depends(require_admin)):
    _spec(cid)

    def do() -> dict:
        cfg = connectors.load()
        row = dict(cfg["connectors"][cid])
        for k, v in body.model_dump().items():
            if v is not None and k != "enabled":
                row[k] = v
        if body.enabled and connectors.needs_ack(cid) and not (row.get("terms_ack") or body.terms_ack):
            raise connectors.ConnectorError("利用条件が未確認の connector です。確かめたうえで、未確認と分かって入れる場合は確認のチェックを入れてください")
        if body.enabled is not None:
            row["enabled"] = body.enabled
        cfg = {**cfg, "connectors": {**cfg["connectors"], cid: row}}
        connectors.save(cfg)
        return connectors.describe(cid)

    try:
        return await asyncio.to_thread(do)
    except connectors.ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class KeyIn(BaseModel):
    value: str = Field(min_length=1, max_length=400)


@router.put("/{cid}/key")
async def put_key(cid: str, body: KeyIn, user: User = Depends(require_admin)):
    _spec(cid)
    try:
        await asyncio.to_thread(connectors.set_key, cid, body.value)
    except connectors.ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"has_key": True}


@router.delete("/{cid}/key")
async def delete_key(cid: str, user: User = Depends(require_admin)):
    _spec(cid)
    await asyncio.to_thread(connectors.delete_key, cid)
    return {"has_key": False}


@router.post("/{cid}/run")
async def run_now(cid: str, user: User = Depends(require_admin)):
    """いま 1 回取る（予算・入切・お休みは通常どおり見る）。結果はまとめ書きへ積む（保存は次のまとめ書き）。"""
    spec = _spec(cid)
    if spec.adapted:
        raise HTTPException(status_code=400, detail="既存の取得は、取得先の管理から取り直してください")
    if not spec.scheduled:
        raise HTTPException(status_code=400, detail="この connector は検索の道具です（/connectors/hn_search/query）")
    return await connectors.run_one(cid, manual=True)


class HnQuery(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    days: int | None = Field(None, ge=1, le=3650)
    save: bool = False


@router.post("/hn_search/query")
async def hn_query(body: HnQuery, user: User = Depends(require_admin)):
    """問いに応じた過去の議論の検索（常に全部は取らない。1 回の問いで 1 リクエスト）。save なら L0 の文書として保存する。"""
    import json

    res = await connectors.run_one("hn_search", manual=True, query=body.query, days=body.days, save=body.save)
    if not res.get("ran"):
        raise HTTPException(status_code=409, detail=res.get("reason") or "実行できませんでした")
    if res.get("error"):
        raise HTTPException(status_code=502, detail="検索に失敗しました")
    try:
        hits = json.loads(res.get("note") or "[]")
    except ValueError:
        hits = []
    return {"hits": hits, "saved": body.save}


# ---- 文書 ----

def _scope(user: User, confidential: bool) -> list[str]:
    if confidential and user.via != "session":
        raise HTTPException(status_code=403, detail="機密の区分を読めるのはログインした管理者だけです")
    return kg.scope_list(confidential)


@documents.get("")
def list_documents(q: str = Query("", max_length=200), kind: str = Query("", max_length=12), tier: int | None = Query(None, ge=0, le=4), limit: int = Query(30, ge=1, le=100),
                   confidential: bool = False, user: User = Depends(current_user), db: Session = Depends(get_news_db)):
    """ニュース以外の文書（論文・リポジトリ・モデル・L0 の URL・変化したページ）。q があれば題・要旨で探す（L0 も対象）。ニュースの記事は /news の検索へ。"""
    scope = _scope(user, confidential)
    if q.strip():
        rows = [d for d in docstore.search(db, q, kind=kind, tier=tier, limit=limit) if d.sensitivity in scope]
    else:
        cond = select(Document).where(Document.kind != "news", Document.sensitivity.in_(scope))
        if kind:
            cond = cond.where(Document.kind == kind)
        if tier is not None:
            cond = cond.where(Document.tier >= tier)
        rows = list(db.scalars(cond.order_by(Document.id.desc()).limit(limit)))
    return {"documents": [docstore.doc_dict(d) for d in rows], "tiers": docstore.tier_counts(db)}


@documents.get("/metrics/series")
def metric_series(subject: str = Query(min_length=1, max_length=300), metric: str = Query(min_length=1, max_length=60), days: int = Query(30, ge=1, le=400),
                  user: User = Depends(current_user), db: Session = Depends(get_news_db)):
    return {"subject": subject, "metric": metric, "points": docstore.series(db, subject, metric, days)}


@documents.get("/{doc_id}")
def get_document(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_news_db)):
    doc = db.get(Document, doc_id)
    if doc is None or (doc.sensitivity != "public" and user.via != "session"):
        raise HTTPException(status_code=404, detail="見つかりません")
    out = docstore.doc_dict(doc)
    out["promotions"] = docstore.promotions(db, doc_id)
    links = db.scalars(select(DocumentLink).where((DocumentLink.src_id == doc_id) | (DocumentLink.dst_id == doc_id)).limit(100))
    out["links"] = [{"type": ln.type, "direction": "out" if ln.src_id == doc_id else "in", "other_id": ln.dst_id if ln.src_id == doc_id else ln.src_id} for ln in links]
    return out


class PromoteIn(BaseModel):
    tier: int = Field(ge=0, le=4)
    reason: str = Field("", max_length=160)


@documents.post("/{doc_id}/promote")
async def promote_document(doc_id: int, body: PromoteIn, user: User = Depends(require_admin)):
    """段を手で上げる（理由は記録される。下げられない）。"""
    def do() -> dict:
        with news_db.session() as db:
            doc = db.get(Document, doc_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="見つかりません")
            changed = docstore.promote(db, doc, body.tier, body.reason.strip() or "利用者が上げた")
            db.commit()
            return {"promoted": changed, "tier": doc.tier}

    return await asyncio.to_thread(writer.run, "news.documents.promote", do)
