"""知識カード・知識の地図（neighbors）・Obsidian の書き出し（v0.5）。読むだけ（メモの保存だけ書く）。ARCHITECTURE 6 章。

- すべて scopes（読んでよい機密の区分。既定は public だけ）で絞る。confidential は管理者（ログイン）だけが指定できる（kg_router）。
- **neighbors は段ごとにまとめて返す**: 物の id の一覧から、辺の型・確かさ・期間・物の種類で絞った辺を確かさの順に上限つきで読み、新しく出た物を次の段の入り口にする（深さ最大 3）。
  1 段 1 回の SQL（IN）で、物ごとに 1 回ずつ問い合わせない。一度に全部を送らず、辺の合計は limit（最大 1,000）まで。v0.6 で ControlDeck の MPKR が使う道具にもなる。
- 利用者のメモ（entity_notes）は AI の部分（entity_cards）と別の表。再分析・カードの作り直しは触れない。
- Obsidian の書き出しは範囲を指定したときだけ（常に書き出さない）。物ごとに 1 つの .md（front matter・[[関係する物]]・根拠の URL）を zip にする。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from qnapnewshub import entities as entities_mod
from qnapnewshub import kg
from qnapnewshub.db import ArticleEntity, Claim, Document, Entity, EntityAlias, EntityCard, EntityNote, Evidence, NewsItem, Relationship, utcnow
from qnapnewshub.extract import ENTITY_TYPES

NEIGHBOR_LIMIT_DEFAULT, NEIGHBOR_LIMIT_MAX, NEIGHBOR_IDS_MAX, NEIGHBOR_DEPTH_MAX = 200, 1000, 50, 3
NOTE_MAX = 20000
EXPORT_LIMIT_MAX = 2000
STATUS_LABEL = {"extracted": "AI の取り出し（原文に引用あり）", "inferred": "推測", "statistical": "一緒に出る集計", "confirmed": "確認済み"}


def node_dict(e: Entity) -> dict:
    return {"id": e.id, "name": e.name, "type": e.type, "article_count": int(e.article_count or 0), "sensitivity": e.sensitivity, "last_seen": kg.iso(e.last_seen)}


def edge_dict(r: Relationship) -> dict:
    return {"id": r.id, "source": r.subject_id, "target": r.object_id, "type": r.type, "directed": bool(r.directed), "confidence": round(float(r.confidence or 0), 3),
            "status": r.status, "evidence_count": int(r.evidence_count or 0), "first_at": kg.iso(r.first_at), "last_at": kg.iso(r.last_at), "sensitivity": r.sensitivity}


# ---- 地図（neighbors） ----

def neighbors(db: Session, ids: list[int], *, types: list[str] | None = None, depth: int = 1, limit: int = NEIGHBOR_LIMIT_DEFAULT, scopes: list[str] | None = None,
              min_confidence: float = 0.0, kinds: list[str] | None = None, since: datetime | None = None, until: datetime | None = None) -> dict:
    """{"seeds": [物], "levels": [{"depth", "nodes": [新しい物], "edges": [辺]}], "truncated": 上限で切ったか}。見えない（区分の範囲外の）物・辺は出さない。"""
    scopes = scopes or ["public"]
    ids = list(dict.fromkeys(int(i) for i in ids))[:NEIGHBOR_IDS_MAX]
    depth = max(1, min(int(depth), NEIGHBOR_DEPTH_MAX))
    limit = max(1, min(int(limit), NEIGHBOR_LIMIT_MAX))
    seeds = [e for e in db.scalars(select(Entity).where(Entity.id.in_(ids), Entity.sensitivity.in_(scopes)))] if ids else []
    known: dict[int, Entity] = {e.id: e for e in seeds}
    frontier = list(known)
    seen_edges: set[int] = set()
    levels: list[dict] = []
    truncated = False
    remaining = limit
    for d in range(1, depth + 1):
        if not frontier or remaining <= 0:
            break
        conds = [or_(Relationship.subject_id.in_(frontier), Relationship.object_id.in_(frontier)), Relationship.sensitivity.in_(scopes), Relationship.confidence >= min_confidence]
        if types:
            conds.append(Relationship.type.in_([t for t in types if t in kg.REL_TYPES]))
        if since:
            conds.append(or_(Relationship.last_at.is_(None), Relationship.last_at >= since))
        if until:
            conds.append(or_(Relationship.first_at.is_(None), Relationship.first_at <= until))
        rows = db.scalars(select(Relationship).where(*conds).order_by(Relationship.confidence.desc(), Relationship.evidence_count.desc(), Relationship.id).limit(remaining * 3 + 50)).all()
        other_ids = {x for r in rows for x in (r.subject_id, r.object_id)} - set(known)
        fetched = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(other_ids), Entity.sensitivity.in_(scopes)))} if other_ids else {}
        edges, nodes = [], {}
        for r in rows:
            if r.id in seen_edges:
                continue
            ends = [known.get(x) or fetched.get(x) for x in (r.subject_id, r.object_id)]
            if not all(ends):
                continue
            new = [e for e in ends if e.id not in known]
            if any(kinds and e.type not in kinds for e in new):
                continue
            if len(edges) >= remaining:
                truncated = True
                break
            seen_edges.add(r.id)
            edges.append(edge_dict(r))
            for e in new:
                nodes[e.id] = e
        known.update(nodes)
        remaining -= len(edges)
        levels.append({"depth": d, "nodes": [node_dict(e) for e in nodes.values()], "edges": edges})
        frontier = list(nodes)
    return {"seeds": [node_dict(e) for e in seeds], "levels": levels, "truncated": truncated}


def seeds(db: Session, *, limit: int = 12, scopes: list[str] | None = None) -> list[dict]:
    """地図の入り口の候補: 関係をもつ物のうち、記事の多い順。"""
    scopes = scopes or ["public"]
    with_rel = select(Relationship.subject_id).union(select(Relationship.object_id))
    rows = db.scalars(select(Entity).where(Entity.id.in_(with_rel), Entity.sensitivity.in_(scopes)).order_by(Entity.article_count.desc(), Entity.id).limit(max(1, min(limit, 50))))
    return [node_dict(e) for e in rows]


# ---- 知識カード ----

def _topics_of(db: Session, entity_id: int) -> list[str]:
    counts: dict[str, int] = {}
    for (raw,) in db.execute(select(NewsItem.topics).join(ArticleEntity, ArticleEntity.article_id == NewsItem.id).where(ArticleEntity.entity_id == entity_id)
                             .order_by(NewsItem.published_at.desc()).limit(60)):
        try:
            for t in json.loads(raw or "[]"):
                counts[str(t)] = counts.get(str(t), 0) + 1
        except ValueError:
            pass
    return [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:8]]


def _claims_with_evidence(db: Session, entity_id: int, scopes: list[str], limit: int = 50) -> list[dict]:
    claims = db.scalars(select(Claim).where(Claim.entity_id == entity_id, Claim.sensitivity.in_(scopes)).order_by(Claim.id.desc()).limit(limit)).all()
    evs: dict[int, list] = {}
    if claims:
        for ev, d in db.execute(select(Evidence, Document).join(Document, Document.id == Evidence.document_id).where(Evidence.claim_id.in_([c.id for c in claims]))):
            if d.sensitivity in scopes:
                evs.setdefault(ev.claim_id, []).append({"document": {"id": d.id, "kind": d.kind, "title": d.title, "url": d.url}, "quote": ev.quote, "start": ev.start, "end": ev.end,
                                                         "page": ev.page, "quote_hash": ev.quote_hash, "grounded": bool(ev.grounded)})
    return [{"id": c.id, "kind": c.kind, "text": c.text, "value": c.value, "ai_extracted": bool(c.ai_extracted), "model": c.model, "sensitivity": c.sensitivity,
             "evidences": evs.get(c.id, [])} for c in claims]


def relations_of(db: Session, entity_id: int, scopes: list[str], limit: int = 60) -> list[dict]:
    rows = db.scalars(select(Relationship).where(or_(Relationship.subject_id == entity_id, Relationship.object_id == entity_id), Relationship.sensitivity.in_(scopes))
                      .order_by(Relationship.confidence.desc(), Relationship.evidence_count.desc(), Relationship.id).limit(limit)).all()
    others = {r.object_id if r.subject_id == entity_id else r.subject_id for r in rows}
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(others), Entity.sensitivity.in_(scopes)))} if others else {}
    out = []
    for r in rows:
        other = ents.get(r.object_id if r.subject_id == entity_id else r.subject_id)
        if other is None:
            continue
        out.append({**edge_dict(r), "direction": "both" if not r.directed else ("out" if r.subject_id == entity_id else "in"), "other": {"id": other.id, "name": other.name, "type": other.type},
                    "status_label": STATUS_LABEL.get(r.status, r.status)})
    return out


def card(db: Session, entity_id: int, scopes: list[str] | None = None) -> dict | None:
    scopes = scopes or ["public"]
    ent = db.get(Entity, entity_id)
    if ent is None or ent.sensitivity not in scopes:
        return None
    base = entities_mod.detail(db, entity_id, limit=30) or {}
    cd = db.get(EntityCard, entity_id)
    note = db.get(EntityNote, entity_id)
    return {"id": ent.id, "name": ent.name, "type": ent.type, "sensitivity": ent.sensitivity, "ai_use": ent.ai_use,
            "overview": {"text": cd.summary, "model": cd.model, "at": kg.iso(cd.at), "ai": True} if cd and cd.summary else None,
            "elements": {"aliases": base.get("aliases", []), "first_seen": kg.iso(ent.first_seen), "last_seen": kg.iso(ent.last_seen), "article_count": ent.article_count,
                         "topics": _topics_of(db, entity_id)},
            "timeline": base.get("timeline", []), "claims": _claims_with_evidence(db, entity_id, scopes), "relations": relations_of(db, entity_id, scopes),
            "history": kg.history(db, entity_id, scopes), "note": {"body": note.body if note else "", "updated_at": kg.iso(note.updated_at) if note else ""}}


def save_note(db: Session, entity_id: int, body: str, user_id: int) -> dict | None:
    """利用者のメモを保存する（コミットは呼ぶ側。writer.run の中）。AI の部分には触れない。長すぎれば ValueError。"""
    if len(body) > NOTE_MAX:
        raise ValueError(f"メモは {NOTE_MAX} 文字までです")
    if db.get(Entity, entity_id) is None:
        return None
    note = db.get(EntityNote, entity_id)
    if note is None:
        note = EntityNote(entity_id=entity_id, body=body, updated_by=user_id)
        db.add(note)
    else:
        note.body, note.updated_by, note.updated_at = body, user_id, utcnow()
    db.flush()
    return {"body": note.body, "updated_at": kg.iso(note.updated_at)}


# ---- Obsidian の書き出し ----

_BAD = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')
REL_LABEL = {"uses": "使う", "supports": "対応する", "replaces": "置き換える", "competes": "競う", "part_of": "一部", "implements": "実装する", "cites": "引用する",
             "contradicts": "矛盾する", "co_occurs": "一緒に出る", "other": "関係"}


def safe_name(name: str) -> str:
    return (_BAD.sub(" ", name).strip(" .") or "名前なし")[:80]


def select_entities(db: Session, *, ids: list[int] | None, types: list[str] | None, since: datetime | None, until: datetime | None, min_articles: int,
                    scopes: list[str], limit: int) -> list[Entity]:
    conds = [Entity.sensitivity.in_(scopes)]
    if ids:
        conds.append(Entity.id.in_(ids))
    if types:
        conds.append(Entity.type.in_([t for t in types if t in ENTITY_TYPES]))
    if since:
        conds.append(Entity.last_seen >= since)
    if until:
        conds.append(Entity.first_seen <= until)
    if min_articles:
        conds.append(Entity.article_count >= min_articles)
    return list(db.scalars(select(Entity).where(*conds).order_by(Entity.article_count.desc(), Entity.id).limit(max(1, min(limit, EXPORT_LIMIT_MAX)))))


def _yaml_list(items: list) -> str:
    return json.dumps(items, ensure_ascii=False)


def _sources_for_pair(db: Session, a: int, b: int, n: int = 3) -> list[tuple[str, str]]:
    a1, a2 = ArticleEntity.__table__.alias("a1"), ArticleEntity.__table__.alias("a2")
    rows = db.execute(select(NewsItem.title, NewsItem.url).join(a1, a1.c.article_id == NewsItem.id).join(a2, and_(a2.c.article_id == NewsItem.id, a2.c.entity_id == b))
                      .where(a1.c.entity_id == a).order_by(NewsItem.published_at.desc()).limit(n))
    return [(t, u) for t, u in rows if u]


def export_zip(db: Session, entities: list[Entity], scopes: list[str], *, include_notes: bool = True) -> tuple[bytes, dict]:
    """物ごとの .md を zip にする。リンクは範囲の中の物だけ（範囲外の機密の物の名前を漏らさない）。(zip の中身, {"files", "scopes"}) を返す。"""
    names: dict[int, str] = {}
    used: set[str] = set()
    for e in entities:
        n = safe_name(e.name)
        if n.casefold() in used:
            n = f"{n} ({e.id})"
        used.add(n.casefold())
        names[e.id] = n
    exported = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for e in entities:
            c = card(db, e.id, scopes)
            if c is None:
                continue
            fm = ["---", f"id: {e.id}", f"name: {json.dumps(e.name, ensure_ascii=False)}", f"type: {e.type}", f"aliases: {_yaml_list(c['elements']['aliases'])}",
                  f"first_seen: {json.dumps(c['elements']['first_seen'])}", f"last_seen: {json.dumps(c['elements']['last_seen'])}", f"article_count: {e.article_count}",
                  f"sensitivity: {e.sensitivity}", f"ai_use: {e.ai_use}", f"exported_at: {json.dumps(exported)}", "tags: [qnapnewshub]", "---", ""]
            body = [f"# {e.name}", ""]
            if c["overview"]:
                body += ["## 概要（AI が作った要約）", c["overview"]["text"], f"（{c['overview']['model']}。確かめた事実ではありません）", ""]
            if c["relations"]:
                body += ["## 関係"]
                for r in c["relations"]:
                    other = r["other"]
                    link = names.get(other["id"]) or safe_name(other["name"])
                    arrow = {"out": "→", "in": "←", "both": "↔"}[r["direction"]]
                    line = f"- {arrow} {REL_LABEL.get(r['type'], r['type'])} [[{link}]]（確かさ {r['confidence']}・{r['status_label']}・根拠 {r['evidence_count']} 件）"
                    if r["type"] == "co_occurs":
                        src = _sources_for_pair(db, e.id, other["id"])
                        line += "".join(f"\n  - [{t}]({u})" for t, u in src)
                    body.append(line)
                body.append("")
            if c["history"]["series"]:
                body += ["## 変化と矛盾"]
                for g in c["history"]["series"]:
                    for en in g["entries"]:
                        mark = {"change": "変化", "contradiction": "矛盾", "first": "最初", "same": "同じ"}.get(en["mark"], "")
                        body.append(f"- {en['t'][:10]} {g['kind']}: {en['value']}（{mark}{'・前は ' + en['from'] if en.get('from') else ''}）" + (f" [{en['title']}]({en['url']})" if en["url"] else ""))
                body.append("")
            if c["claims"]:
                body += ["## 主張と根拠"]
                for cl in c["claims"]:
                    body.append(f"- {cl['text']}" + (f"（{cl['value']}）" if cl["value"] else "") + ("［AI の取り出し］" if cl["ai_extracted"] else ""))
                    for ev in cl["evidences"]:
                        d = ev["document"]
                        pos = f" p.{ev['page']}" if ev["page"] else ""
                        body.append(f"  - 根拠: {d['title'] or d['kind']}{pos} {d['url']}" + (f" 「{ev['quote']}」" if ev["quote"] else ""))
                body.append("")
            if include_notes and c["note"]["body"]:
                body += ["## 自分のメモ", c["note"]["body"], ""]
            z.writestr(f"{e.type}/{names[e.id]}.md", "\n".join(fm + body))
    return buf.getvalue(), {"files": len(entities), "scopes": scopes}
