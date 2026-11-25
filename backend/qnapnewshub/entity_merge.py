"""物（Things）の名寄せ: 同じ物の別表記を 1 つに寄せる。ARCHITECTURE 6 章（つながりの精度・2026-11-25）。

目的: LLM が引く検索（context・neighbors・検索）で、同じ物が「Qwen3.8」と「Qwen3.8 (2.4T-A95B)」のように割れていると、記事・主張・関係が分かれて密度が下がる。
寄せて、記事・主張・関係を 1 つの物に集める。`extract.normalize_key`（NFKC・小文字・記号と空白を除く）で寄らない表記を、規則と手がかりで寄せる。

- **規則（`rule_key`）**: 会社の語尾（Inc.・Corporation・Ltd…）・先頭の The・版の v 接頭辞・末尾の `.0`（`7` と `7.0`）・長音符を除いた鍵が同じ。
  種類が同じ（または片方が other）なら**自動で寄せる**（`auto`）。種類が違う・括弧を除くと同じ（`Ear (3)` と `Ear (note)` は別物なので括弧は除かない＝括弧だけの違いは候補止まり）は**候補**（管理者が確定）。
- **手がかり（`cross_script`）**: 日本語の表記と英語の表記は規則で寄らない。同じ出来事（events）・近い重複の記事の組の中で、同じ種類の物が「英語側の 1 つ」と「日本語側の 1 つ」だけで、
  同じ記事には一緒に出ない組を**候補**にする（自動では寄せない）。
- **寄せ方（`merge`）**: 勝つのは記事の多い方。別名（名前と別名）・記事の結び付き（article_entities）・主張・関係（同じ主語・目的語・型は 1 本にして根拠を移す・自己ループは捨てる）・メモ・
  物の集計（ana_entity）・つなぐ候補を付け替える。**機密の区分が public どうしだけ**（機密は触らない）。冪等（寄せ済みは None）。
- **元に戻せる**: 寄せるたびに `entity_merges` へ snapshot（負けた物の行・別名・結び付き・移した主張と関係・落とした関係の全行・根拠の移動）を残し、`undo` で戻す。戻した組は「違う」の候補にして夜に再び寄せない。
- 寄せた後に同じ表記が取り出されたら、`resolve_key` が勝った物へ向ける（`entities._get_or_create`）。
書くのは夜の仕事（kg_job.aggregate）と管理者の操作（writer の中）だけ。コミットは呼ぶ側。
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from qnapnewshub import kg
from qnapnewshub.db import (ArticleEntity, Claim, Entity, EntityAlias, EntityCard, EntityCommunity, EntityMerge, EntityNote, EventArticle, Evidence,
                            MentionCandidate, MergeCandidate, NewsItem, Relationship)

_UTC = timezone.utc
MAX_AUTO_PER_RUN = 500
MAX_CANDIDATES = 400
_CORP = re.compile(r"[\s,]+(inc|corp|corporation|llc|ltd|co|company|gmbh|plc)\.?$")
_PAREN = re.compile(r"[(（\[【].*?[)）\]】]")
_DOT_ZERO = re.compile(r"(?<=\d)(?:\.0)+(?=$|[\s\-_/])")
_V_PREFIX = re.compile(r"(?<![a-z0-9])v(?=\d)")
_JP_CORP = re.compile(r"株式会社|\(株\)|㈱|有限会社")


def rule_key(name: str, *, paren: bool = False) -> str:
    """規則で寄せる鍵。paren=True は括弧とその中身も除く（候補を作るときだけ）。短すぎる（2 文字未満）・空は ""。"""
    t = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()
    t = _JP_CORP.sub("", t)
    if paren:
        t = _PAREN.sub("", t)
    t = re.sub(r"^the\s+", "", t)
    for _ in range(2):
        t = _CORP.sub("", t.strip())
    t = _V_PREFIX.sub("", t)
    t = _DOT_ZERO.sub("", t)
    key = "".join(ch for ch in t.replace("ー", "") if ch.isalnum())[:200]
    return key if len(key) >= 2 else ""


def compatible(a: str, b: str) -> bool:
    return a == b or "other" in (a, b)


# ---- 行の保存と復元 ----

def _dump(row) -> dict:
    out = {}
    for col in row.__table__.columns:
        v = getattr(row, col.key)
        out[col.key] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _load(cls, data: dict) -> dict:
    out = {}
    for col in cls.__table__.columns:
        if col.key not in data:
            continue
        v = data[col.key]
        if v is not None and col.type.python_type is datetime:
            v = datetime.fromisoformat(v)
        out[col.key] = v
    return out


def _now() -> datetime:
    return datetime.now(_UTC)


# ---- 寄せた後の表記の向け先 ----

def resolve_key(db: Session, key: str) -> Entity | None:
    """key の物が無いとき、寄せた記録（元に戻していないもの）から勝った物を返す。勝った物も寄せられていれば先へたどる（最大 5 回）。"""
    rec = db.scalar(select(EntityMerge).where(EntityMerge.loser_key == key, EntityMerge.undone_at.is_(None)).order_by(EntityMerge.id.desc()).limit(1))
    for _ in range(5):
        if rec is None:
            return None
        ent = db.get(Entity, rec.winner_id)
        if ent is not None:
            return ent
        rec = db.scalar(select(EntityMerge).where(EntityMerge.loser_id == rec.winner_id, EntityMerge.undone_at.is_(None)).order_by(EntityMerge.id.desc()).limit(1))
    return None


# ---- 寄せる ----

def _rel_move(db: Session, winner_id: int, loser_id: int, snap: dict) -> list[int]:
    """loser の関係を winner へ付け替える。同じ（主語・目的語・型）があれば 1 本にして根拠を移す。自己ループは捨てる。再計算する関係の id を返す。"""
    redo: list[int] = []
    rels = list(db.scalars(select(Relationship).where((Relationship.subject_id == loser_id) | (Relationship.object_id == loser_id)).order_by(Relationship.id)))
    for rel in rels:
        s = winner_id if rel.subject_id == loser_id else rel.subject_id
        o = winner_id if rel.object_id == loser_id else rel.object_id
        if s == o:
            evs = list(db.scalars(select(Evidence).where(Evidence.relationship_id == rel.id)))
            snap["rels"].append({"op": "dropped", "rel": _dump(rel), "evidences": [_dump(e) for e in evs]})
            db.execute(delete(Evidence).where(Evidence.relationship_id == rel.id))
            db.delete(rel)
            continue
        if rel.type in kg.SYMMETRIC and s > o:
            s, o = o, s
        other = db.scalar(select(Relationship).where(Relationship.subject_id == s, Relationship.object_id == o, Relationship.type == rel.type, Relationship.id != rel.id))
        if other is None:
            snap["rels"].append({"op": "moved", "id": rel.id, "subject_id": rel.subject_id, "object_id": rel.object_id})
            rel.subject_id, rel.object_id = s, o
            db.flush()
            continue
        ev_ids = list(db.scalars(select(Evidence.id).where(Evidence.relationship_id == rel.id)))
        snap["rels"].append({"op": "merged", "rel": _dump(rel), "into": other.id, "evidence_ids": ev_ids})
        if ev_ids:
            db.execute(update(Evidence).where(Evidence.id.in_(ev_ids)).values(relationship_id=other.id))
        if rel.type == "co_occurs":
            other.stat_n = max(other.stat_n, rel.stat_n)
            other.stat_ratio = max(other.stat_ratio, rel.stat_ratio)
            other.confidence = max(other.confidence, rel.confidence)
        else:
            redo.append(other.id)
        db.delete(rel)
        db.flush()
    return redo


def _deltas(db: Session, loser_id: int, winner_id: int, articles: set[int], winner_articles: set[int], sign: int) -> list[tuple]:
    """物の集計（ana_entity）への増減。sign=+1 は寄せる（負けた物の分を引いて、勝った物へ足す）、-1 は戻す。"""
    if not articles:
        return []
    meta = {r[0]: r[1:] for r in db.execute(select(NewsItem.id, NewsItem.published_at, NewsItem.dup_of, NewsItem.kind, NewsItem.user_id, NewsItem.source_key)
                                            .where(NewsItem.id.in_(list(articles))))}
    out: list[tuple] = []
    for aid in articles:
        if aid not in meta:
            continue
        out.append((loser_id, *meta[aid], -sign))
        if aid not in winner_articles:
            out.append((winner_id, *meta[aid], sign))
    return out


def merge(db: Session, winner_id: int, loser_id: int, *, mode: str = "auto", reason: str = "") -> int | None:
    """loser を winner へ寄せる。記録の id を返す（寄せ済み・機密・同じ物は None）。"""
    if winner_id == loser_id:
        return None
    w, lo = db.get(Entity, winner_id), db.get(Entity, loser_id)
    if w is None or lo is None or w.sensitivity != "public" or lo.sensitivity != "public":
        return None
    from qnapnewshub import analytics

    snap: dict = {"loser": _dump(lo), "winner": {"type": w.type, "first_seen": w.first_seen.isoformat(), "last_seen": w.last_seen.isoformat()}, "rels": []}
    # 別名: 負けた物の名前と別名を勝った物へ（同じ綴りは足さない）
    have = {a for (a,) in db.execute(select(EntityAlias.alias).where(EntityAlias.entity_id == winner_id))} | {w.name}
    lo_aliases = [a for (a,) in db.execute(select(EntityAlias.alias).where(EntityAlias.entity_id == loser_id).order_by(EntityAlias.id))]
    added = [a for a in dict.fromkeys([lo.name, *lo_aliases]) if a not in have]
    for a in added:
        db.add(EntityAlias(entity_id=winner_id, alias=a))
    snap["aliases"], snap["aliases_added"] = lo_aliases, added
    if w.type == "other" and lo.type != "other":
        w.type = lo.type
    # 記事
    l_arts = {a for (a,) in db.execute(select(ArticleEntity.article_id).where(ArticleEntity.entity_id == loser_id))}
    w_arts = {a for (a,) in db.execute(select(ArticleEntity.article_id).where(ArticleEntity.entity_id == winner_id))}
    moved = sorted(l_arts - w_arts)
    snap["articles"], snap["articles_moved"] = sorted(l_arts), moved
    db.execute(delete(ArticleEntity).where(ArticleEntity.entity_id == loser_id))
    if moved:
        db.execute(insert(ArticleEntity), [{"article_id": a, "entity_id": winner_id} for a in moved])
    # 主張
    claim_ids = list(db.scalars(select(Claim.id).where(Claim.entity_id == loser_id)))
    snap["claims"] = claim_ids
    if claim_ids:
        db.execute(update(Claim).where(Claim.id.in_(claim_ids)).values(entity_id=winner_id))
    # 関係
    redo = _rel_move(db, winner_id, loser_id, snap)
    kg.recompute_relationships(db, redo)
    # メモ・カード
    lnote = db.get(EntityNote, loser_id)
    if lnote is not None and lnote.body:
        wnote = db.get(EntityNote, winner_id)
        snap["note"] = {"loser": _dump(lnote), "winner_body": wnote.body if wnote else None}
        if wnote is None:
            db.add(EntityNote(entity_id=winner_id, body=lnote.body, updated_by=lnote.updated_by))
        elif lnote.body not in wnote.body:
            wnote.body = f"{wnote.body}\n\n（{lo.name} から寄せたメモ）\n{lnote.body}"
    lcard = db.get(EntityCard, loser_id)
    if lcard is not None:
        snap["card"] = _dump(lcard)
    wcard = db.get(EntityCard, winner_id)
    if wcard is not None:
        snap["winner_card_sig"] = wcard.sig
        wcard.sig = ""      # 材料が変わったので、夜に概要を作り直す
    # まとまり・つなぐ候補
    snap["community"] = db.scalar(select(EntityCommunity.community_id).where(EntityCommunity.entity_id == loser_id))
    snap["mentions"] = [_dump(m) for m in db.scalars(select(MentionCandidate).where(MentionCandidate.entity_id == loser_id))]
    db.execute(delete(MentionCandidate).where(MentionCandidate.entity_id == loser_id))
    db.execute(delete(EntityCommunity).where(EntityCommunity.entity_id == loser_id))
    # 集計
    analytics.apply_entity_deltas(db, _deltas(db, loser_id, winner_id, l_arts, w_arts, 1))
    # 本体
    w.first_seen = min(w.first_seen, lo.first_seen, key=lambda d: d.replace(tzinfo=None))
    w.last_seen = max(w.last_seen, lo.last_seen, key=lambda d: d.replace(tzinfo=None))
    db.flush()
    w.article_count = int(db.scalar(select(func.count()).select_from(ArticleEntity).where(ArticleEntity.entity_id == winner_id)) or 0)
    db.execute(delete(Entity).where(Entity.id == loser_id))
    db.expire_all()
    rec = EntityMerge(winner_id=winner_id, loser_id=loser_id, loser_key=lo_key(snap), loser_name=snap["loser"]["name"], mode=mode, reason=reason[:40],
                      snapshot=json.dumps(snap, ensure_ascii=False), merged_at=_now())
    db.add(rec)
    lo_id, hi_id = sorted((winner_id, loser_id))
    cand = db.scalar(select(MergeCandidate).where(MergeCandidate.lo_id == lo_id, MergeCandidate.hi_id == hi_id))
    if cand is not None:
        cand.status = "merged"
    db.flush()
    return rec.id


def lo_key(snap: dict) -> str:
    return str(snap["loser"]["key"])


def undo(db: Session, merge_id: int) -> dict | None:
    """寄せを元に戻す。戻した組は「違う」の候補にして、夜に再び寄せない。戻せない（無い・戻し済み・勝った物が無い）は None。"""
    rec = db.get(EntityMerge, merge_id)
    if rec is None or rec.undone_at is not None:
        return None
    w = db.get(Entity, rec.winner_id)
    if w is None or db.get(Entity, rec.loser_id) is not None:
        return None
    from qnapnewshub import analytics

    snap = json.loads(rec.snapshot)
    ld = snap["loser"]
    db.execute(insert(Entity), [_load(Entity, ld)])
    lid, wid = rec.loser_id, rec.winner_id
    for a in snap["aliases"]:
        db.add(EntityAlias(entity_id=lid, alias=a))
    if snap["aliases_added"]:
        db.execute(delete(EntityAlias).where(EntityAlias.entity_id == wid, EntityAlias.alias.in_(snap["aliases_added"])))
    w.type = snap["winner"]["type"]
    # 記事（まだある記事だけ）
    alive = {a for (a,) in db.execute(select(NewsItem.id).where(NewsItem.id.in_(snap["articles"] or [0])))}
    now_w = {a for (a,) in db.execute(select(ArticleEntity.article_id).where(ArticleEntity.entity_id == wid))}
    if snap["articles_moved"]:
        db.execute(delete(ArticleEntity).where(ArticleEntity.entity_id == wid, ArticleEntity.article_id.in_(snap["articles_moved"])))
    back = sorted(set(snap["articles"]) & alive)
    if back:
        db.execute(insert(ArticleEntity), [{"article_id": a, "entity_id": lid} for a in back])
    # 主張
    if snap["claims"]:
        db.execute(update(Claim).where(Claim.id.in_(snap["claims"]), Claim.entity_id == wid).values(entity_id=lid))
    # 関係（逆順）
    redo: list[int] = []
    for item in reversed(snap["rels"]):
        if item["op"] == "moved":
            rel = db.get(Relationship, item["id"])
            if rel is not None:
                rel.subject_id, rel.object_id = item["subject_id"], item["object_id"]
        elif item["op"] == "merged":
            db.execute(insert(Relationship), [_load(Relationship, item["rel"])])
            if item["evidence_ids"]:
                db.execute(update(Evidence).where(Evidence.id.in_(item["evidence_ids"])).values(relationship_id=item["rel"]["id"]))
            redo += [item["rel"]["id"], item["into"]]
        else:
            db.execute(insert(Relationship), [_load(Relationship, item["rel"])])
            if item["evidences"]:
                db.execute(insert(Evidence), [_load(Evidence, e) for e in item["evidences"]])
            redo.append(item["rel"]["id"])
        db.flush()
    kg.recompute_relationships(db, [r for r in redo if db.get(Relationship, r) is not None])
    # メモ・カード・まとまり・つなぐ候補
    if "note" in snap:
        db.add(EntityNote(**_load(EntityNote, snap["note"]["loser"])))
        wn = db.get(EntityNote, wid)
        if wn is not None:
            if snap["note"]["winner_body"] is None:
                db.delete(wn)
            else:
                wn.body = snap["note"]["winner_body"]
    if "card" in snap:
        db.add(EntityCard(**_load(EntityCard, snap["card"])))
    wc = db.get(EntityCard, wid)
    if wc is not None and "winner_card_sig" in snap:
        wc.sig = snap["winner_card_sig"]
    if snap.get("community") is not None:
        db.add(EntityCommunity(entity_id=lid, community_id=snap["community"]))
    for m in snap["mentions"]:
        if db.get(NewsItem, m["article_id"]) is not None and not db.scalar(
                select(MentionCandidate.id).where(MentionCandidate.entity_id == lid, MentionCandidate.article_id == m["article_id"])):
            db.add(MentionCandidate(**{k: v for k, v in _load(MentionCandidate, m).items() if k != "id"}))
    # 集計（寄せるときの逆）
    analytics.apply_entity_deltas(db, _deltas(db, lid, wid, set(back), now_w - set(snap["articles_moved"]), -1))
    db.flush()
    for ent_id in (lid, wid):
        e = db.get(Entity, ent_id)
        e.article_count = int(db.scalar(select(func.count()).select_from(ArticleEntity).where(ArticleEntity.entity_id == ent_id)) or 0)
    rec.undone_at = _now()
    lo_id, hi_id = sorted((wid, lid))
    cand = db.scalar(select(MergeCandidate).where(MergeCandidate.lo_id == lo_id, MergeCandidate.hi_id == hi_id))
    if cand is None:
        db.add(MergeCandidate(lo_id=lo_id, hi_id=hi_id, reason="undone", score=0.0, status="dismissed"))
    else:
        cand.status = "dismissed"
    db.flush()
    return {"winner_id": wid, "loser_id": lid}


# ---- 夜: 自動の寄せと候補 ----

def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _blocked(db: Session) -> set[tuple[int, int]]:
    """決まった組（違うと言われた・寄せた・戻した）。夜に作り直さない。"""
    return {(lo, hi) for lo, hi in db.execute(select(MergeCandidate.lo_id, MergeCandidate.hi_id).where(MergeCandidate.status != "candidate"))}


def compute_merges(db: Session) -> dict:
    """夜の名寄せ。①規則で確かな組を自動で寄せる ②迷う組（種類違い・括弧違い・日英の表記）を候補にする。{"merged", "candidates"}。"""
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.sensitivity == "public"))}
    blocked = _blocked(db)
    merged = 0
    # ① 規則の鍵が同じ物のまとまり
    groups: dict[str, list[int]] = {}
    for e in ents.values():
        k = rule_key(e.name)
        if k:
            groups.setdefault(k, []).append(e.id)
    pending: list[tuple[int, int, str, float]] = []
    gone: set[int] = set()
    for k, ids in sorted(groups.items()):
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: (-ents[i].article_count, i))
        top = ids[0]
        for other in ids[1:]:
            if _pair(top, other) in blocked:
                continue
            if compatible(ents[top].type, ents[other].type):
                if merged < MAX_AUTO_PER_RUN and merge(db, top, other, mode="auto", reason="rule") is not None:
                    merged += 1
                    gone.add(other)
            else:
                pending.append((top, other, "rule_type", 0.8))
    # ② 括弧を除くと同じ（括弧の中が違う物どうしは別物かもしれないので候補止まり）
    pgroups: dict[str, list[int]] = {}
    for i, e in ents.items():
        if i in gone:
            continue
        k = rule_key(e.name, paren=True)
        if k and _PAREN.search(unicodedata.normalize("NFKC", e.name)):
            pgroups.setdefault(k, []).append(i)
    plain: dict[str, int] = {}
    for i, e in ents.items():
        if i not in gone:
            plain.setdefault(rule_key(e.name), i)
    for k, ids in pgroups.items():
        base = plain.get(k)
        for i in ids:
            if base is not None and base != i and base not in gone:
                pending.append((base, i, "rule_paren", 0.6))
    # ③ 日本語と英語の表記（同じ出来事の記事に別々に出る）
    pending += _cross_script(db, {i: e for i, e in ents.items() if i not in gone}, blocked)
    # 候補を保存（未確定は作り直す。決まったものは残す）
    db.execute(delete(MergeCandidate).where(MergeCandidate.status == "candidate"))
    seen: set[tuple[int, int]] = set()
    kept = 0
    for a, b, reason, score in sorted(pending, key=lambda p: -p[3]):
        pr = _pair(a, b)
        if pr in seen or pr in blocked or a in gone or b in gone or db.get(Entity, a) is None or db.get(Entity, b) is None:
            continue
        seen.add(pr)
        db.add(MergeCandidate(lo_id=pr[0], hi_id=pr[1], reason=reason, score=score, status="candidate"))
        kept += 1
        if kept >= MAX_CANDIDATES:
            break
    return {"merged": merged, "candidates": kept}


def _cross_script(db: Session, ents: dict[int, Entity], blocked: set[tuple[int, int]]) -> list[tuple[int, int, str, float]]:
    """同じ出来事（events）か近い重複（dup_of）の記事の組の中で、同じ種類（other 以外）の物が英語側 1 つ・日本語側 1 つだけのとき、その組を候補にする（同じ記事には一緒に出ない）。"""
    unit: dict[int, tuple] = {}
    size: dict[tuple, int] = {}
    ev = dict(db.execute(select(EventArticle.article_id, EventArticle.event_id)).all())
    for aid, dup in db.execute(select(NewsItem.id, NewsItem.dup_of)):
        u = ("e", ev[aid]) if aid in ev else ("d", dup or aid)
        unit[aid] = u
        size[u] = size.get(u, 0) + 1
    by_unit: dict[tuple, dict[int, set[int]]] = {}
    arts_of: dict[int, set[int]] = {}
    for aid, eid in db.execute(select(ArticleEntity.article_id, ArticleEntity.entity_id)):
        if eid not in ents or size.get(unit.get(aid), 0) < 2 or ents[eid].type == "other":
            continue
        by_unit.setdefault(unit[aid], {}).setdefault(eid, set()).add(aid)
        arts_of.setdefault(eid, set()).add(aid)
    score: dict[tuple[int, int], int] = {}
    for u, members in by_unit.items():
        for kind in {ents[e].type for e in members}:
            ascii_ids = [e for e in members if ents[e].type == kind and ents[e].name.isascii()]
            jp_ids = [e for e in members if ents[e].type == kind and not ents[e].name.isascii()]
            if len(ascii_ids) == 1 and len(jp_ids) == 1:
                pr = _pair(ascii_ids[0], jp_ids[0])
                if pr not in blocked and not (arts_of[pr[0]] & arts_of[pr[1]]):
                    score[pr] = score.get(pr, 0) + 1
    units_of: dict[int, set] = {}
    for eid, arts in arts_of.items():
        units_of[eid] = {unit[a] for a in arts}
    out = []
    for (a, b), n in score.items():
        ua, ub = len(units_of[a]), len(units_of[b])
        if n / min(ua, ub) < 0.5 or n / max(ua, ub) < 0.34:      # 片方が他の出来事にも多く出る物（Anthropic など）は、組の相手とは言えない
            continue
        out.append((a, b, "cross_script", min(0.9, 0.3 + 0.2 * n)))
    return out


# ---- 読む（API 用） ----

def _brief(e: Entity) -> dict:
    return {"id": e.id, "name": e.name, "type": e.type, "article_count": int(e.article_count or 0)}


def list_candidates(db: Session, limit: int = 50) -> dict:
    rows = list(db.scalars(select(MergeCandidate).where(MergeCandidate.status == "candidate").order_by(MergeCandidate.score.desc(), MergeCandidate.id).limit(max(1, min(limit, 200)))))
    ids = {i for c in rows for i in (c.lo_id, c.hi_id)}
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(ids or {0})))}
    items = []
    for c in rows:
        a, b = ents.get(c.lo_id), ents.get(c.hi_id)
        if a is None or b is None:
            continue
        keep, drop = sorted((a, b), key=lambda e: (-e.article_count, e.id))
        items.append({"id": c.id, "reason": c.reason, "score": c.score, "keep": _brief(keep), "drop": _brief(drop)})
    total = int(db.scalar(select(func.count(MergeCandidate.id)).where(MergeCandidate.status == "candidate")) or 0)
    return {"items": items, "total": total}


def list_merges(db: Session, limit: int = 30) -> list[dict]:
    out = []
    for r in db.scalars(select(EntityMerge).order_by(EntityMerge.id.desc()).limit(max(1, min(limit, 200)))):
        w = db.get(Entity, r.winner_id)
        out.append({"id": r.id, "winner": {"id": r.winner_id, "name": w.name if w else ""}, "loser": {"id": r.loser_id, "name": r.loser_name}, "mode": r.mode, "reason": r.reason,
                    "merged_at": kg.iso(r.merged_at), "undone": r.undone_at is not None, "can_undo": r.undone_at is None and w is not None})
    return out


def decide(db: Session, candidate_id: int, *, accept: bool, keep_id: int | None = None) -> dict | None:
    """候補を確定（寄せる）か却下する。keep_id で残す方を選べる（既定は記事の多い方）。無い・決め済みは None。"""
    c = db.get(MergeCandidate, candidate_id)
    if c is None or c.status != "candidate":
        return None
    if not accept:
        c.status = "dismissed"
        return {"status": "dismissed"}
    a, b = db.get(Entity, c.lo_id), db.get(Entity, c.hi_id)
    if a is None or b is None:
        c.status = "dismissed"
        return None
    keep = db.get(Entity, keep_id) if keep_id in (a.id, b.id) else min((a, b), key=lambda e: (-e.article_count, e.id))
    drop = b if keep is a else a
    mid = merge(db, keep.id, drop.id, mode="confirmed", reason=c.reason)
    if mid is None:
        return None
    return {"status": "merged", "merge_id": mid, "winner_id": keep.id}

