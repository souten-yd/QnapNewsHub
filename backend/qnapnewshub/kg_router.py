"""知識グラフの API（/api/v1/kg・/api/v1/graph）。ログインした利用者か読み取りの API トークン（GET と、読むだけの POST）。

- 読む API は既定で public の範囲だけ。`confidential=true` は管理者（ログイン。API トークンは不可）だけ。
- 文書・主張・関係を作る API と、メモの保存・集計の作り直しは管理者だけ。ニュース以外の種類（paper・repo・patent…）は、いまは表と API だけ（取り込みは統合の段）。
- `POST /graph/neighbors` は読むだけ（物の id の一覧・辺の型・深さ・上限）。v0.6 で ControlDeck の MPKR が使う。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qnapnewshub import auth as auth_mod
from qnapnewshub import rcache
from qnapnewshub import kg, kg_job, kg_view, writer
from qnapnewshub import db as news_db
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import read_user, require_admin
from qnapnewshub.db import Claim, Document, Entity, Relationship, get_news_db

router = APIRouter(prefix="/kg", tags=["kg"])
graph = APIRouter(prefix="/graph", tags=["graph"])


def scopes_for(user: User, confidential: bool, sensitivity: list[str] | None = None) -> list[str]:
    """読んでよい機密の区分の一覧（読む API はみな、ここを通す）。既定は public だけ。
    - confidential=true は public ＋ confidential。sensitivity=[...] を渡せば、その区分だけ（例 ["confidential"] は機密だけ）。
    - 許される範囲: ログインした管理者（session）は全部。トークンは、つないだときに許された区分だけ（auth.allowed_sensitivities。既定 public）。範囲の外は 403（黙って落とさない）。
    - 区分の名前が正しくなければ 422。"""
    want = set(sensitivity) if sensitivity else ({"public", "confidential"} if confidential else {"public"})
    if want - set(kg.SENSITIVITY):
        raise HTTPException(status_code=422, detail="区分は public・confidential のどちらかです")
    if want - auth_mod.allowed_sensitivities(user):
        raise HTTPException(status_code=403, detail="機密の区分を読めるのはログインした管理者か、機密を許してつないだ相手だけです")
    return [s for s in kg.SENSITIVITY if s in want]


class DocIn(BaseModel):
    kind: str = Field(max_length=12)
    ident: str = Field(min_length=1, max_length=1100)
    title: str = Field("", max_length=400)
    url: str = Field("", max_length=1000)
    source: str = Field("", max_length=120)
    published_at: datetime | None = None
    version: str = Field("", max_length=64)
    sensitivity: str = "confidential"
    ai_use: str = "local_only"


class EvidenceIn(BaseModel):
    quote: str = Field("", max_length=300)
    start: int | None = Field(None, ge=0)
    end: int | None = Field(None, ge=0)
    page: int | None = Field(None, ge=0)


class ClaimIn(BaseModel):
    document_id: int
    entity: str = Field(min_length=1, max_length=200)
    entity_type: str = "other"
    kind: str = "claim"
    text: str = Field(min_length=1, max_length=2000)
    value: str = Field("", max_length=120)
    evidences: list[EvidenceIn] = Field(default_factory=list, max_length=20)


class RelationIn(BaseModel):
    document_id: int
    subject: str = Field(min_length=1, max_length=200)
    object: str = Field(min_length=1, max_length=200)
    type: str
    evidences: list[EvidenceIn] = Field(default_factory=list, max_length=20)


class NoteIn(BaseModel):
    body: str = Field("", max_length=kg_view.NOTE_MAX)


class NeighborsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=kg_view.NEIGHBOR_IDS_MAX)
    types: list[str] = Field(default_factory=list, max_length=12)
    direction: str = Field("both", pattern="^(both|out|in)$")      # 入り口の物から見た向き（out＝出る・in＝入る＝逆向きにたどる）
    type_directions: dict[str, str] = Field(default_factory=dict, max_length=12)      # 型ごとの向き（例 {"uses": "out", "part_of": "in"}）
    order: str = Field("weight", pattern="^(weight|recent|evidence)$")      # weight＝確かさの順・recent＝最後の日時が新しい順（時系列）・evidence＝根拠の数の順
    only_contradicts: bool = False      # 矛盾（contradicts）の辺だけ
    hub_k: int | None = Field(None, ge=1, le=kg_view.HUB_K_MAX)      # ハブの抑制: 物 1 つにつき重みの高い順に k 本まで
    budget_ms: int = Field(rcache.BUDGET_DEFAULT_MS, ge=10, le=rcache.BUDGET_MAX_MS)
    sensitivity: list[str] | None = Field(None, max_length=2)
    depth: int = Field(1, ge=1, le=kg_view.NEIGHBOR_DEPTH_MAX)
    limit: int = Field(kg_view.NEIGHBOR_LIMIT_DEFAULT, ge=1, le=kg_view.NEIGHBOR_LIMIT_MAX)
    min_confidence: float = Field(0.0, ge=0, le=1)
    kinds: list[str] = Field(default_factory=list, max_length=12)
    since: datetime | None = None
    until: datetime | None = None
    confidential: bool = False


class ExportIn(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=kg_view.EXPORT_LIMIT_MAX)
    types: list[str] = Field(default_factory=list, max_length=12)
    since: datetime | None = None
    until: datetime | None = None
    min_articles: int = Field(3, ge=0, le=100000)
    limit: int = Field(500, ge=1, le=kg_view.EXPORT_LIMIT_MAX)
    include_notes: bool = True
    confidential: bool = False


@router.get("/stats")
def stats(user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """文書・主張・関係の数（診断・画面の空の判定用。区分をまたいだ合計の数だけで中身は出さない）。"""
    kinds = dict(db.execute(select(Relationship.type, func.count()).group_by(Relationship.type)).all())
    return {"documents": int(db.scalar(select(func.count(Document.id))) or 0), "relationships": kinds, "claims": int(db.scalar(select(func.count(Claim.id))) or 0)}


@router.get("/documents")
def list_documents(kind: str = Query("", max_length=12), q: str = Query("", max_length=100), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
                   confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    scopes = scopes_for(user, confidential)
    conds = [Document.sensitivity.in_(scopes)]
    if kind:
        conds.append(Document.kind == kind)
    if q.strip():
        conds.append(Document.title.contains(q.strip(), autoescape=True))
    rows = db.scalars(select(Document).where(*conds).order_by(Document.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [kg.doc_dict(d) for d in rows], "total": int(db.scalar(select(func.count(Document.id)).where(*conds)) or 0)}


@router.post("/documents")
async def create_document(body: DocIn, user: User = Depends(require_admin)):
    """文書を識別子で 1 つにして入れる。区分の既定は fail-closed（confidential・local_only）。すでにある文書の区分は厳しい方へだけ動く。"""
    def do() -> dict:
        with news_db.session() as db:
            try:
                doc = kg.upsert_document(db, **body.model_dump())
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            db.commit()
            return kg.doc_dict(doc)

    return await asyncio.to_thread(writer.run, "news.kg_document", do)


@router.post("/claims")
async def create_claim(body: ClaimIn, user: User = Depends(require_admin)):
    """文書の主張（根拠は複数）。機密の区分は文書から引き継ぐ。"""
    def do() -> dict:
        with news_db.session() as db:
            doc = db.get(Document, body.document_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="その文書は見つかりません")
            ent = kg.entity_for(db, body.entity, body.entity_type, doc.published_at or kg._now(), doc.sensitivity, doc.ai_use)
            if ent is None:
                raise HTTPException(status_code=422, detail="物の名前が正しくありません")
            claim = kg.add_claim(db, document=doc, entity=ent, kind=body.kind, text_=body.text, value=body.value, evidences=[e.model_dump() for e in body.evidences])
            db.commit()
            return {"id": claim.id, "entity_id": ent.id, "sensitivity": claim.sensitivity, "ai_use": claim.ai_use}

    return await asyncio.to_thread(writer.run, "news.kg_claim", do)


@router.post("/relationships")
async def create_relationship(body: RelationIn, user: User = Depends(require_admin)):
    """人が根拠つきで入れる関係。引用（quote）のある根拠が 1 つ以上あれば confirmed、無ければ inferred（推測）。区分は根拠の文書から引き継ぐ。"""
    def do() -> dict:
        with news_db.session() as db:
            doc = db.get(Document, body.document_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="その文書は見つかりません")
            when = doc.published_at or kg._now()
            a = kg.entity_for(db, body.subject, "other", when, doc.sensitivity, doc.ai_use)
            b = kg.entity_for(db, body.object, "other", when, doc.sensitivity, doc.ai_use)
            if a is None or b is None:
                raise HTTPException(status_code=422, detail="物の名前が正しくありません")
            try:
                rel = None
                for ev in body.evidences or [EvidenceIn()]:
                    rel = kg.add_relationship(db, subject=a, object_=b, rel_type=body.type, document=doc, quote=ev.quote, start=ev.start, end=ev.end, page=ev.page,
                                              grounded=False, by=user.username)
                kg.recompute_relationships(db, [rel.id])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            db.commit()
            db.refresh(rel)
            return kg_view.edge_dict(rel)

    return await asyncio.to_thread(writer.run, "news.kg_relationship", do)


@router.get("/entities/{entity_id}/card")
def get_card(entity_id: int, confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """知識カード: 概要（AI）・要素・年表・主張と根拠・関係・変化と矛盾・利用者のメモ。"""
    found = kg_view.card(db, entity_id, scopes_for(user, confidential))
    if found is None:
        raise HTTPException(status_code=404, detail="その物は見つかりません")
    return found


@router.put("/entities/{entity_id}/note")
async def put_note(entity_id: int, body: NoteIn, user: User = Depends(require_admin)):
    """利用者のメモをすぐ保存する（AI の部分・再分析とは別の表）。"""
    def do() -> dict | None:
        with news_db.session() as db:
            out = kg_view.save_note(db, entity_id, body.body, user.id)
            db.commit()
            return out

    found = await asyncio.to_thread(writer.run, "news.kg_note", do)
    if found is None:
        raise HTTPException(status_code=404, detail="その物は見つかりません")
    return found


@router.post("/rebuild")
async def rebuild(user: User = Depends(require_admin)):
    """文書の対応・「一緒に出る」の集計を今すぐ作り直す（管理者。ふだんは夜に 1 日 1 回）。"""
    return await asyncio.to_thread(kg_job.aggregate)


@router.post("/export")
async def export(body: ExportIn, user: User = Depends(get_current_user)):
    """範囲を指定して Obsidian 用の Markdown を zip で返す。機密の区分の範囲は既定 public だけ（confidential は管理者だけ）。"""
    scopes = scopes_for(user, body.confidential)

    def do() -> tuple[bytes, dict]:
        with news_db.session() as db:
            ents = kg_view.select_entities(db, ids=body.ids, types=body.types, since=body.since, until=body.until, min_articles=body.min_articles, scopes=scopes, limit=body.limit)
            return kg_view.export_zip(db, ents, scopes, include_notes=body.include_notes and user.via == "session")

    data, info = await asyncio.to_thread(do)
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="knowledge.zip"', "X-Export-Files": str(info["files"])})


@graph.post("/neighbors")
def neighbors(body: NeighborsIn, user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """物の id の一覧から、辺の型（と向き）・確かさ・期間・種類で絞った周りを段ごとにまとめて返す（読むだけ。トークンも POST できる）。
    辺の向き（逆向きも）・時系列（order=recent）・矛盾だけ・ハブの抑制（hub_k）・点の次数と最後の辺の日時つき。応答はキャッシュされる（DB の版つき）。"""
    scopes = scopes_for(user, body.confidential, body.sensitivity)
    td = {k: v for k, v in body.type_directions.items() if k in kg.REL_TYPES and v in kg_view.DIRECTIONS}

    def compute() -> dict:
        budget = rcache.Budget(body.budget_ms)
        with rcache.hard_limit(db, budget):
            out = kg_view.neighbors(db, body.ids, types=body.types, depth=body.depth, limit=body.limit, scopes=scopes, min_confidence=body.min_confidence, kinds=body.kinds,
                                    since=body.since, until=body.until, direction=body.direction, type_directions=td, order=body.order, only_contradicts=body.only_contradicts,
                                    hub_k=body.hub_k, deadline=budget.deadline)
        out["meta"] = {"elapsed_ms": budget.elapsed_ms(), "budget_ms": budget.ms}
        return rcache.fit(out, [(lv, "edges") for lv in out["levels"]])

    return rcache.cached("neighbors", scopes, body.model_dump(mode="json", exclude={"budget_ms"}), compute)


@graph.get("/seeds")
def seeds(limit: int = Query(12, ge=1, le=50), confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    return {"items": kg_view.seeds(db, limit=limit, scopes=scopes_for(user, confidential))}
