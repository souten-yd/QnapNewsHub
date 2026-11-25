"""検索の道具の API（v0.6。ControlDeck の MPKR が使う。MPKR 本体は ControlDeck）: /retrieve/seeds・/retrieve/evidence・/graph/ppr・GET /research。
読むだけ。session か Bearer トークン（read・link。POST も通る＝auth.read_user）。区分は scopes_for（既定 public。トークンはつないだときに許された範囲だけ）。
応答は DB の版つきでキャッシュ（rcache）。時間（budget_ms）・件数の上限つき。POST /graph/neighbors は kg_router。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qnapnewshub import rcache, retrieval
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import read_user
from qnapnewshub.db import get_news_db
from qnapnewshub.kg_router import scopes_for

router = APIRouter(tags=["retrieval"])
BUDGET = Field(rcache.BUDGET_DEFAULT_MS, ge=10, le=rcache.BUDGET_MAX_MS)


class SeedsIn(BaseModel):
    q: str = Field(min_length=1, max_length=500)
    kinds: list[str] = Field(default_factory=list, max_length=4)      # document・entity・event・claim（空は全部）
    doc_kinds: list[str] = Field(default_factory=list, max_length=9)      # news・paper・repo…（空は全部）
    limit: int = Field(20, ge=1, le=100)      # 種類ごとの上限
    since: datetime | None = None
    until: datetime | None = None
    confidential: bool = False
    sensitivity: list[str] | None = Field(None, max_length=2)
    budget_ms: int = BUDGET


@router.post("/retrieve/seeds")
def retrieve_seeds(body: SeedsIn, user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """問いの文から種を探す: 全文（既存の BM25 の索引を OR で）・物の名前と別名・イベント。種類つきの候補（点数・根拠の位置・日時・区分）を返す。"""
    scopes = scopes_for(user, body.confidential, body.sensitivity)

    def compute() -> dict:
        budget = rcache.Budget(body.budget_ms)
        with rcache.hard_limit(db, budget):
            out = retrieval.seeds(db, body.q, kinds=body.kinds, doc_kinds=body.doc_kinds, scopes=scopes, since=body.since, until=body.until, limit=body.limit, budget=budget)
        out["meta"] = {"elapsed_ms": budget.elapsed_ms(), "budget_ms": budget.ms}
        return rcache.fit(out, [(out, "items")])

    return rcache.cached("seeds", scopes, body.model_dump(mode="json", exclude={"budget_ms"}), compute)


class ExpandIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=retrieval.MEET_SEEDS * 4)      # 種の物（点数の大きい順。上位 MEET_SEEDS だけ使う）
    seed_scores: list[float] = Field(default_factory=list, max_length=retrieval.MEET_SEEDS * 4)
    q: str = Field("", max_length=500)
    depth: int | None = Field(None, ge=1, le=retrieval.MEET_DEPTH_MAX)      # 省略＝自動（普通の問いは 1・物を結ぶ問いだけ最大 5。会合した段で止める）
    meet: bool = True      # 2 つ以上の種から届いた物・辺に加点（会合）
    min_confidence: float = Field(0.0, ge=0, le=1)
    confidential: bool = False
    sensitivity: list[str] | None = Field(None, max_length=2)
    budget_ms: int = BUDGET


@router.post("/graph/expand")
def graph_expand(body: ExpandIn, user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """上位の種から別々に展開し、2 つ以上の種から届いた物・辺に加点（会合）する。深さは問いで決める（既定 1・物を結ぶ問いだけ最大 5）。応答の plan に判定（cap・kind・当たった語）・depth に実際の段。"""
    scopes = scopes_for(user, body.confidential, body.sensitivity)

    def compute() -> dict:
        budget = rcache.Budget(body.budget_ms)
        with rcache.hard_limit(db, budget):
            out = retrieval.expand_meet(db, body.ids, body.seed_scores, body.q, scopes=scopes, depth=body.depth, meet=body.meet, min_confidence=body.min_confidence, budget=budget)
        out["meta"] = {"elapsed_ms": budget.elapsed_ms(), "budget_ms": budget.ms}
        return rcache.fit(out, [(out, "edges"), (out, "nodes")])

    return rcache.cached("expand", scopes, body.model_dump(mode="json", exclude={"budget_ms"}), compute)


class PprIn(BaseModel):
    seeds: list[int] = Field(min_length=1, max_length=retrieval.PPR_SEEDS_MAX)
    weights: list[float] = Field(default_factory=list, max_length=retrieval.PPR_SEEDS_MAX)      # 種ごとの重み（空は均等）
    alpha: float = Field(0.15, gt=0.01, lt=0.9)
    epsilon: float = Field(1e-4, ge=1e-7, le=0.1)
    max_nodes: int = Field(2000, ge=10, le=retrieval.PPR_NODES_MAX)
    top: int = Field(50, ge=1, le=retrieval.PPR_TOP_MAX)
    types: list[str] = Field(default_factory=list, max_length=12)
    direction: str = Field("both", pattern="^(both|out|in)$")
    type_directions: dict[str, str] = Field(default_factory=dict, max_length=12)
    min_confidence: float = Field(0.0, ge=0, le=1)
    weight: str = Field("confidence", pattern="^(confidence|uniform)$")
    hub_k: int | None = Field(100, ge=1, le=500)
    since: datetime | None = None
    until: datetime | None = None
    include_seeds: bool = False
    confidential: bool = False
    sensitivity: list[str] | None = Field(None, max_length=2)
    budget_ms: int = BUDGET


@router.post("/graph/ppr")
def graph_ppr(body: PprIn, user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """種の集合から局所の Personalized PageRank（push 法。全体の再計算をしない）。ε・最大の点数・時間の上限つき。順位は score の大きい順。"""
    scopes = scopes_for(user, body.confidential, body.sensitivity)
    seeds = list(dict.fromkeys(body.seeds))
    weights = {s: (body.weights[i] if i < len(body.weights) else 1.0) for i, s in enumerate(seeds)}
    from qnapnewshub.kg import REL_TYPES

    td = {k: v for k, v in body.type_directions.items() if k in REL_TYPES and v in ("both", "out", "in")}

    def compute() -> dict:
        budget = rcache.Budget(body.budget_ms)
        with rcache.hard_limit(db, budget):
            out = retrieval.ppr(db, weights, scopes=scopes, alpha=body.alpha, eps=body.epsilon, max_nodes=body.max_nodes, top=body.top, types=body.types, direction=body.direction,
                                type_directions=td, min_confidence=body.min_confidence, weight=body.weight, hub_k=body.hub_k, since=body.since, until=body.until,
                                include_seeds=body.include_seeds, budget=budget)
        out["meta"] = {"elapsed_ms": budget.elapsed_ms(), "budget_ms": budget.ms}
        return rcache.fit(out, [(out, "items")])

    return rcache.cached("ppr", scopes, body.model_dump(mode="json", exclude={"budget_ms"}), compute)


class EvidenceIn(BaseModel):
    entities: list[int] = Field(default_factory=list, max_length=retrieval.EVIDENCE_IDS_MAX)
    claims: list[int] = Field(default_factory=list, max_length=retrieval.EVIDENCE_IDS_MAX)
    relationships: list[int] = Field(default_factory=list, max_length=retrieval.EVIDENCE_IDS_MAX)
    per_item: int = Field(5, ge=1, le=retrieval.EVIDENCE_PER_ITEM_MAX)
    compact: bool = False      # true なら文書の url を省く（label・id だけ。LLM に渡す文脈用）
    confidential: bool = False
    sensitivity: list[str] | None = Field(None, max_length=2)


@router.post("/retrieve/evidence")
def retrieve_evidence(body: EvidenceIn, user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """物・主張・関係の id の一覧から、根拠（引用・位置・文書の題・URL・日時・区分）をまとめて返す。区分の範囲の外・無い id は missing。"""
    scopes = scopes_for(user, body.confidential, body.sensitivity)

    def compute() -> dict:
        budget = rcache.Budget(rcache.BUDGET_DEFAULT_MS)
        out = retrieval.evidence(db, entities=body.entities, claims=body.claims, relationships=body.relationships, scopes=scopes, per_item=body.per_item, compact=body.compact)
        out["meta"] = {"elapsed_ms": budget.elapsed_ms()}
        return out

    return rcache.cached("evidence", scopes, body.model_dump(mode="json"), compute)


@router.get("/research")
def get_research(topic: str = Query(min_length=1, max_length=200), limit: int = Query(10, ge=1, le=50), min_similarity: float = Query(0.35, ge=0.1, le=1), full: bool = False,
                 confidential: bool = False, user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """調査の記録をテーマで読む（差分調査の材料）: 同じテーマの過去の記録・類似のテーマ・前回の日時。"""
    scopes = scopes_for(user, confidential)
    return rcache.cached("research", scopes, {"topic": topic, "limit": limit, "ms": min_similarity, "full": full},
                         lambda: retrieval.research(db, topic, scopes=scopes, limit=limit, min_similarity=min_similarity, full=full))
