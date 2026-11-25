"""Things の API（/api/v1/things）。旧「物」（/entities）の別名の道と、LLM の脳の API（context）・全体の地図・主題の入口（MOC）・つなぐ候補。互換のため /entities・/kg は変えない。

- 読む API は session か Bearer トークン（read・link。POST の context も通る＝auth.read_user）。区分は kg_router.scopes_for（既定 public。トークンはつないだときに許された範囲だけ）。
- つなぐ候補の確定・却下だけ管理者（require_admin）。応答は DB の版つきでキャッシュ（rcache）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qnapnewshub import channels as channels_mod
from qnapnewshub import db as news_db
from qnapnewshub import entities, entity_merge, kg_view, mapgrav, rcache, things, writer
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import read_user, require_admin
from qnapnewshub.db import Entity, get_news_db
from qnapnewshub.kg_router import scopes_for

router = APIRouter(prefix="/things", tags=["things"])
CONTEXT_IDS_MAX = 20


# ---- 一覧・ページ・カード（/entities と同じ中身の別名） ----

@router.get("")
def list_things(q: str = Query("", max_length=100), type: str = Query("", max_length=16), days: int = Query(0, ge=0, le=3650),
                sort: str = Query("count", pattern="^(count|recent)$"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
                confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    found = entities.list_entities(db, q=q, kind=type, days=days, sort=sort, limit=limit, offset=offset, scopes=scopes_for(user, confidential))
    if q.strip() and offset == 0:
        mapgrav.note_search([int(i["id"]) for i in found.get("items", [])])
    return found


@router.get("/lookup")
def lookup(q: str = Query(min_length=1, max_length=100), limit: int = Query(5, ge=1, le=20), confidential: bool = False, sensitivity: list[str] | None = Query(None),
           user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """名前から Things の id を引く（LLM が context を呼ぶ前の入口）。1 行の定義つき。"""
    scopes = scopes_for(user, confidential, sensitivity)
    rows = entities.list_entities(db, q=q, limit=limit, scopes=scopes)["items"]
    mapgrav.note_search([int(r["id"]) for r in rows])
    defs = things.definitions(db, [r["id"] for r in rows])
    return {"items": [{"id": r["id"], "name": r["name"], "type": r["type"], "article_count": r["article_count"], "definition": defs.get(r["id"], "")} for r in rows]}


# ---- LLM の脳の API ----

class ContextIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=CONTEXT_IDS_MAX)
    max_relations: int = Field(12, ge=0, le=40)
    max_facts: int = Field(5, ge=0, le=15)
    format: str = Field("json", pattern="^(json|text)$")
    confidential: bool = False
    sensitivity: list[str] | None = Field(None, max_length=2)


def _contexts(db: Session, ids: list[int], scopes: list[str], max_relations: int, max_facts: int) -> tuple[list[dict], list[int]]:
    ents = {e.id: e for e in db.query(Entity).filter(Entity.id.in_(ids), Entity.sensitivity.in_(scopes))}
    items = [things.context(db, ents[i], scopes, max_relations=max_relations, max_facts=max_facts) for i in dict.fromkeys(ids) if i in ents]
    return items, [i for i in dict.fromkeys(ids) if i not in ents]      # 無い物と読めない物は区別しない（区分の外の物の存在を漏らさない）


# ---- 名寄せ（同じ物の別表記）。確かなものは夜に自動で寄せ、迷うものは候補として管理者が確定する ----

class MergeDecideIn(BaseModel):
    keep_id: int | None = None      # 残す方（既定は記事の多い方）


@router.get("/merge-candidates")
def merge_candidates(limit: int = Query(30, ge=1, le=200), user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    return {**entity_merge.list_candidates(db, limit), "merges": entity_merge.list_merges(db, 15)}


async def _merge_job(name: str, fn):
    def do():
        with news_db.session() as db:
            out = fn(db)
            db.commit()
            return out

    out = await asyncio.to_thread(writer.run, name, do)
    if out is None:
        raise HTTPException(status_code=404, detail="その候補・記録は見つからないか、すでに決めてあります")
    return out


@router.post("/merge-candidates/{candidate_id}/confirm")
async def merge_confirm(candidate_id: int, body: MergeDecideIn | None = None, user: User = Depends(require_admin)):
    """候補を確定して寄せる（記事・主張・関係・別名を付け替える。記録が残り、元に戻せる）。"""
    return await _merge_job("news.things_merge", lambda db: entity_merge.decide(db, candidate_id, accept=True, keep_id=body.keep_id if body else None))


@router.post("/merge-candidates/{candidate_id}/dismiss")
async def merge_dismiss(candidate_id: int, user: User = Depends(require_admin)):
    return await _merge_job("news.things_merge", lambda db: entity_merge.decide(db, candidate_id, accept=False))


@router.post("/merges/{merge_id}/undo")
async def merge_undo(merge_id: int, user: User = Depends(require_admin)):
    """寄せを元に戻す（負けた物の行・別名・記事・主張・関係を戻す。戻した組は夜に再び寄せない）。"""
    return await _merge_job("news.things_merge_undo", lambda db: entity_merge.undo(db, merge_id))


@router.get("/{entity_id}/context")
def get_context(entity_id: int, max_relations: int = Query(12, ge=0, le=40), max_facts: int = Query(5, ge=0, le=15), format: str = Query("json", pattern="^(json|text)$"),
                confidential: bool = False, sensitivity: list[str] | None = Query(None), user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """LLM に渡す短い文脈: 1 行の定義・frontmatter のような項目・型つきの関係（相手の 1 行の定義つき）・主な事実と根拠の URL・未解決の問い。format=text で Markdown（さらに短い）。"""
    scopes = scopes_for(user, confidential, sensitivity)
    items, _ = _contexts(db, [entity_id], scopes, max_relations, max_facts)
    if not items:
        raise HTTPException(status_code=404, detail="その Things は見つかりません")
    if format == "text":
        return PlainTextResponse(things.context_text(items[0]))
    return items[0]


@router.post("/context")
def post_context(body: ContextIn, user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """まとめて引く（読むだけの POST。トークンも通る）。見つからない・読めない id は missing に（区別しない）。"""
    scopes = scopes_for(user, body.confidential, body.sensitivity)
    items, missing = _contexts(db, body.ids, scopes, body.max_relations, body.max_facts)
    if body.format == "text":
        return PlainTextResponse("\n\n".join(things.context_text(c) for c in items))
    return {"items": items, "missing": missing}


# ---- 全体の地図・主題の入口 ----

@router.get("/map/overview")
def map_overview(limit: int = Query(things.OVERVIEW_LIMIT_DEFAULT, ge=10, le=things.OVERVIEW_LIMIT_MAX), isolated: int = Query(things.OVERVIEW_ISOLATED, ge=0, le=500),
                 confidential: bool = False, sensitivity: list[str] | None = Query(None), user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    """最初に出す全体の地図: 主要な点（次数・最近の重みの上位）・線・まとまりの代表・孤立した点（別の配列）。近づく・まとまりを押すと中を開く（/map/community/{id}）。"""
    scopes = scopes_for(user, confidential, sensitivity)
    return rcache.cached("things_overview", scopes, {"limit": limit, "isolated": isolated}, lambda: things.overview(db, scopes, limit=limit, isolated=isolated))


@router.get("/map/community/{community_id}")
def map_community(community_id: int, limit: int = Query(300, ge=1, le=1000), confidential: bool = False, sensitivity: list[str] | None = Query(None),
                  user: User = Depends(read_user), db: Session = Depends(get_news_db)):
    scopes = scopes_for(user, confidential, sensitivity)

    def compute() -> dict:
        out = things.community_members(db, community_id, scopes, limit=limit)
        if out is None:
            raise HTTPException(status_code=404, detail="そのまとまりは見つかりません")
        return out

    return rcache.cached("things_community", scopes, {"id": community_id, "limit": limit}, compute)


@router.get("/moc")
def moc_index(user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """主題の入口の一覧（まとまりごと・チャンネルごと）。"""
    return things.moc_index(db, channels_mod.load()["channels"])


@router.get("/moc/community/{community_id}")
def moc_community(community_id: int, confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    scopes = scopes_for(user, confidential)
    out = rcache.cached("moc_community", scopes, {"id": community_id}, lambda: things.moc_community(db, community_id, scopes) or {"missing": True})
    if b'"missing":true' in out.body:
        raise HTTPException(status_code=404, detail="そのまとまりは見つかりません")
    return out


@router.get("/moc/channel/{channel_id}")
def moc_channel(channel_id: str, confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    scopes = scopes_for(user, confidential)
    ch = next((c for c in channels_mod.load()["channels"] if c["id"] == channel_id), None)
    if ch is None:
        raise HTTPException(status_code=404, detail="そのチャンネルは見つかりません")
    return rcache.cached("moc_channel", scopes, {"id": channel_id, "include": ch["include"], "exclude": ch["exclude"], "kinds": ch["kinds"]}, lambda: things.moc_channel(db, ch, scopes))


# ---- カード・つなぐ候補 ----

@router.get("/{entity_id}/card")
def get_card(entity_id: int, confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    found = kg_view.card(db, entity_id, scopes_for(user, confidential))
    if found is None:
        raise HTTPException(status_code=404, detail="その Things は見つかりません")
    mapgrav.note_card(entity_id)
    return found


@router.get("/{entity_id}")
def get_thing(entity_id: int, confidential: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    found = entities.detail(db, entity_id, scopes=scopes_for(user, confidential))
    if found is None:
        raise HTTPException(status_code=404, detail="その Things は見つかりません")
    mapgrav.note_card(entity_id)
    return found


async def _decide(entity_id: int, article_id: int, accept: bool) -> dict:
    def do() -> dict | None:
        with news_db.session() as db:
            out = things.confirm_mention(db, entity_id, article_id, accept=accept)
            db.commit()
            return out

    found = await asyncio.to_thread(writer.run, "news.things_mention", do)
    if found is None:
        raise HTTPException(status_code=404, detail="その候補は見つからないか、すでに決めてあります")
    return found


@router.post("/{entity_id}/mentions/{article_id}/confirm")
async def confirm_mention(entity_id: int, article_id: int, user: User = Depends(require_admin)):
    """リンクされていない言及を「この Things の記事」として確定する（article_entities へ足す）。"""
    return await _decide(entity_id, article_id, True)


@router.post("/{entity_id}/mentions/{article_id}/dismiss")
async def dismiss_mention(entity_id: int, article_id: int, user: User = Depends(require_admin)):
    return await _decide(entity_id, article_id, False)
