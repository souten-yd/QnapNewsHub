"""物（entities）・記事との結びつき・fact（claims）の保存と読み。ARCHITECTURE 3 章の一部（v0.2 前半）。

- 書き込みは Write Coordinator のまとめ書きの中（writer._flush → apply_extractions）だけ。ここの apply_* を直接呼ばない。
- 同じ記事を取り出し直したら、その記事の AI の取り出し（article_entities・claims）を消してから入れ直す（二重にならない）。
- 物は正規化のキー（extract.normalize_key）で 1 行。表記が違えば別名（entity_aliases）に残す。種類は最初のものを使い、other だった物だけ具体的な種類に更新する。
- 読みは DB にある分だけ（未保存の取り出しは、まとめ書きのあとに出る）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from qnapnewshub.db import ArticleEntity, Claim, Entity, EntityAlias, Evidence, NewsItem, Relationship
from qnapnewshub import thing_filter
from qnapnewshub.extract import ENTITY_TYPES, normalize_key, relation_ok

_UTC = timezone.utc
MAX_ENTITIES_PER_ARTICLE = 10      # 8 ＋ fact の about が物の一覧に無かった分
TAGS_PER_ITEM = 4


def _naive(value: datetime | None) -> datetime:
    value = value or datetime.now(_UTC)
    return (value.astimezone(_UTC) if value.tzinfo else value).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return _naive(value).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- 書く（まとめ書きの中から） ----

def _get_or_create(db: Session, name: str, kind: str, when: datetime, cache: dict[str, Entity]) -> Entity | None:
    key = normalize_key(name)
    if not key:
        return None
    ent = cache.get(key) or db.scalar(select(Entity).where(Entity.key == key))
    if ent is None:
        from qnapnewshub import entity_merge

        ent = entity_merge.resolve_key(db, key)      # 名寄せで寄せた表記は、勝った物へ向ける
    if ent is None:
        ent = Entity(key=key, name=name[:200], type=kind if kind in ENTITY_TYPES else "other", first_seen=when, last_seen=when)
        db.add(ent)
        db.flush()
    else:
        if _naive(when) < _naive(ent.first_seen):
            ent.first_seen = when
        if _naive(when) > _naive(ent.last_seen):
            ent.last_seen = when
        if ent.type == "other" and kind in ENTITY_TYPES and kind != "other":
            ent.type = kind
        if name != ent.name and not db.scalar(select(EntityAlias.id).where(EntityAlias.entity_id == ent.id, EntityAlias.alias == name[:200])):
            db.add(EntityAlias(entity_id=ent.id, alias=name[:200]))
    cache[key] = ent
    return ent


def apply_extractions(db: Session, staged: dict[int, dict]) -> int:
    """{記事の id: {"entities", "facts", "model", "version", "at"}} を DB へ（コミットは呼ぶ側）。消えた記事は飛ばす。入れた記事の数を返す。"""
    if not staged:
        return 0
    ids = list(staged)
    meta = {r[0]: r[1:] for r in db.execute(select(NewsItem.id, NewsItem.published_at, NewsItem.dup_of, NewsItem.kind, NewsItem.user_id, NewsItem.source_key)
                                            .where(NewsItem.id.in_(ids)))}
    published = {i: m[0] for i, m in meta.items()}
    texts = {r[0]: (r[1] or "", r[2] or "") for r in db.execute(select(NewsItem.id, NewsItem.source, NewsItem.title).where(NewsItem.id.in_(ids)))}
    deltas: list[tuple] = []      # 物の集計（ana_entity）への増減。取り出し直しは、前の結びつきを引いて新しい結びつきを足す
    touched: set[int] = set()
    cache: dict[str, Entity] = {}
    done = 0
    from qnapnewshub import kg

    kg.sync_documents(db, ids)      # 記事 → 文書の対応（冪等）。主張・関係はその文書の機密の区分を引き継ぐ
    docs = kg.docs_of_articles(db, ids)
    # 再取り出しの前の関係の根拠を消す（人が入れたものは残す）。追加（取りこぼしの問い直し additive）の記事は、前の取り出しを消さない
    old_rels = kg.drop_article_evidence(db, [d[0] for a, d in docs.items() if a in meta and not staged[a].get("additive")])
    rel_work: list[tuple] = []
    new_claims: list[tuple[Claim, int | None, dict]] = []
    for article_id, data in staged.items():
        if article_id not in published:
            continue
        when = published[article_id]
        additive = bool(data.get("additive"))
        old = list(db.scalars(select(ArticleEntity.entity_id).where(ArticleEntity.article_id == article_id)))
        touched.update(old)
        if not additive:
            db.execute(delete(ArticleEntity).where(ArticleEntity.article_id == article_id))
            db.execute(delete(Evidence).where(Evidence.claim_id.in_(select(Claim.id).where(Claim.article_id == article_id, Claim.ai_extracted.is_(True)))))
            db.execute(delete(Claim).where(Claim.article_id == article_id, Claim.ai_extracted.is_(True)))
        doc = docs.get(article_id)
        sens, use = kg.inherit([(doc[1], doc[2])]) if doc else ("public", "allowed")
        linked: dict[str, Entity] = {}
        by_name: dict[str, Entity] = {}      # 表記の鍵 → 物（名寄せで寄せた表記は、勝った物の key と違う）
        if additive:      # 前の取り出しの物は、そのまま結び付けておく（新しい物・主張・関係だけを足す）
            for ent in db.scalars(select(Entity).join(ArticleEntity, ArticleEntity.entity_id == Entity.id).where(ArticleEntity.article_id == article_id)):
                linked[ent.key] = ent
                by_name[ent.key] = ent
        src, title = texts.get(article_id, ("", ""))

        def excluded(name: str, kind: str = "other") -> bool:
            return bool(thing_filter.reject(name, kind, source=src, title=title))

        for row in data.get("entities", []):
            if excluded(row["name"], row["type"]):
                continue      # 取得元・媒体・一般語・断片は物にしない（thing_filter）
            ent = _get_or_create(db, row["name"], row["type"], when, cache)
            if ent is not None:
                by_name[normalize_key(row["name"])] = ent
            if ent is not None and ent.key not in linked and len(linked) < MAX_ENTITIES_PER_ARTICLE:
                linked[ent.key] = ent
        for fact in data.get("facts", []):
            key = normalize_key(fact["about"])
            why = thing_filter.reject(fact["about"], "other", source=src, title=title)
            if why == "spec" and linked:      # 仕様の句（144Hz AMOLED display）は物にせず、その記事の主な物（最初に結び付いた物）の数値の事実として残す
                key = next(iter(linked))
            elif why:
                continue      # 取得元・一般語についての主張は捨てる
            ent = by_name.get(key) or linked.get(key)
            if ent is None and len(linked) < MAX_ENTITIES_PER_ARTICLE:
                ent = _get_or_create(db, fact["about"], "other", when, cache)
                if ent is not None:
                    linked[ent.key] = ent
                    by_name[key] = ent
            claim = Claim(article_id=article_id, entity_id=ent.id if ent else None, kind=fact["kind"], text=fact["text"], value=fact.get("value", ""),
                          source_article_id=article_id, model=str(data.get("model", ""))[:200], prompt_version=str(data.get("version", ""))[:16],
                          created_at=data.get("at") or datetime.now(_UTC), ai_extracted=True, document_id=doc[0] if doc else None, sensitivity=sens, ai_use=use,
                          stated_at=when)      # 主張の日付の既定は記事の公開日
            db.add(claim)
            new_claims.append((claim, doc[0] if doc else None, fact))
        for rel in data.get("relations", []):
            ends = []
            for name in (rel["subject"], rel["object"]):
                if excluded(name) or (thing_filter.is_platform(name) and rel["type"] != "other"):
                    ends.append(None)      # 取得元・一般語・プラットフォームは関係の相手にしない
                    continue
                e = by_name.get(normalize_key(name)) or linked.get(normalize_key(name))
                if e is None and len(linked) < MAX_ENTITIES_PER_ARTICLE:
                    e = _get_or_create(db, name, "other", when, cache)
                    if e is not None:
                        linked[e.key] = e
                        by_name[normalize_key(name)] = e
                ends.append(e)
            if all(ends) and relation_ok(rel["type"], ends[0].type, ends[1].type):      # 型の定義域・値域に合う関係だけ
                rel_work.append((article_id, rel, ends[0], ends[1], str(data.get("model", "")), str(data.get("version", ""))))
        had = set(old) if additive else set()
        for ent in linked.values():
            if ent.id in had:
                continue
            db.add(ArticleEntity(article_id=article_id, entity_id=ent.id))
            touched.add(ent.id)
        db.flush()
        now_ids = {ent.id for ent in linked.values()} | had
        deltas += [(eid, *meta[article_id], -1) for eid in set(old) - now_ids]
        deltas += [(eid, *meta[article_id], 1) for eid in now_ids - set(old)]
        done += 1
    for i in range(0, len(touched), 400):      # 記事の数は数え直す（取り出し直し・消えた記事で、足し引きがずれない）
        chunk = list(touched)[i:i + 400]
        counts = dict(db.execute(select(ArticleEntity.entity_id, func.count()).where(ArticleEntity.entity_id.in_(chunk)).group_by(ArticleEntity.entity_id)).all())
        for ent in db.scalars(select(Entity).where(Entity.id.in_(chunk))):
            ent.article_count = int(counts.get(ent.id, 0))
    db.flush()
    for claim, doc_id, fact in new_claims:      # 主張の根拠（文書。引用が原文に一致＝exact・言い換え・無し の段階。位置は取り出し元に無いので持たない）
        if doc_id is not None:
            t = fact.get("tier") or "none"
            db.add(kg.evidence_row(doc_id, claim_id=claim.id, quote=fact.get("quote", "") if t == "exact" else "", grounded=t == "exact", confidence=kg.TIER_CONF[t], tier=t))
    kg.apply_article_relations(db, rel_work, docs, old_rels)
    from qnapnewshub import analytics

    analytics.apply_entity_deltas(db, deltas)

    from qnapnewshub import searchindex

    searchindex.reindex_ids(db.connection(), [i for i in staged if i in published])      # 物の名前も検索の索引へ（同じトランザクション）
    return done


# ---- 読む ----

def tags_for(db: Session, article_ids: list[int]) -> dict[int, list[dict]]:
    """記事ごとの物の札（記事の数が多い順に最大 4）。"""
    if not article_ids:
        return {}
    out: dict[int, list[dict]] = {}
    rows = db.execute(select(ArticleEntity.article_id, Entity.id, Entity.name, Entity.type).join(Entity, Entity.id == ArticleEntity.entity_id)
                      .where(ArticleEntity.article_id.in_(article_ids)).order_by(Entity.article_count.desc(), Entity.id))
    for aid, eid, name, kind in rows:
        tags = out.setdefault(aid, [])
        if len(tags) < TAGS_PER_ITEM:
            tags.append({"id": eid, "name": name, "type": kind})
    return out


def attach_tags(db: Session, rows: list[dict]) -> list[dict]:
    tags = tags_for(db, [r["id"] for r in rows])
    for r in rows:
        r["entities"] = tags.get(r["id"], [])
    return rows


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_entities(db: Session, *, q: str = "", kind: str = "", days: int = 0, sort: str = "count", limit: int = 50, offset: int = 0, scopes: list[str] | None = None) -> dict:
    """物の一覧。q は名前・別名・正規化のキーの部分一致。days は「その日数のうちに記事が出た物」だけにして、その期間の記事の数（period_count）で並べる。"""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    conds = [Entity.sensitivity.in_(scopes or ["public"])]      # 機密の区分の物は、区分の範囲に入れたときだけ（既定は public だけ。v0.6）
    if kind in ENTITY_TYPES:
        conds.append(Entity.type == kind)
    needle = q.strip()
    if needle:
        key = normalize_key(needle)
        parts = [Entity.name.ilike(_like(needle), escape="\\"),
                 Entity.id.in_(select(EntityAlias.entity_id).where(EntityAlias.alias.ilike(_like(needle), escape="\\")))]
        if key:
            parts.append(Entity.key.like(_like(key), escape="\\"))
        conds.append(or_(*parts))
    if days and days > 0:
        since = datetime.now(_UTC).replace(tzinfo=None) - timedelta(days=days)
        period = (select(ArticleEntity.entity_id, func.count().label("n")).join(NewsItem, NewsItem.id == ArticleEntity.article_id)
                  .where(NewsItem.published_at >= since).group_by(ArticleEntity.entity_id).subquery())
        base = select(Entity, period.c.n).join(period, period.c.entity_id == Entity.id).where(*conds)
        order = [period.c.n.desc(), Entity.id] if sort != "recent" else [Entity.last_seen.desc(), Entity.id]
    else:
        base = select(Entity, Entity.article_count).where(*conds)
        order = [Entity.article_count.desc(), Entity.id] if sort != "recent" else [Entity.last_seen.desc(), Entity.id]
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = [{"id": e.id, "name": e.name, "type": e.type, "first_seen": _iso(e.first_seen), "last_seen": _iso(e.last_seen),
              "article_count": e.article_count, "period_count": int(n)} for e, n in db.execute(base.order_by(*order).limit(limit).offset(offset))]
    return {"items": items, "total": int(total), "types": list(ENTITY_TYPES)}


def detail(db: Session, entity_id: int, *, limit: int = 100, scopes: list[str] | None = None) -> dict | None:
    """物のページ: 記事の年表（新しい順）・facts（AI の取り出しの印つき）・一緒に出る物の上位。"""
    ent = db.get(Entity, entity_id)
    if ent is None or ent.sensitivity not in (scopes or ["public"]):
        return None
    aliases = list(db.scalars(select(EntityAlias.alias).where(EntityAlias.entity_id == ent.id).order_by(EntityAlias.id)))
    arts = db.execute(select(NewsItem).join(ArticleEntity, ArticleEntity.article_id == NewsItem.id).where(ArticleEntity.entity_id == ent.id)
                      .order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(limit)).scalars().all()
    from qnapnewshub.sources import safe_url

    timeline = [{"id": a.id, "title": a.title, "url": safe_url(a.url), "source": a.source, "published_at": _iso(a.published_at),
                 "summary": (a.summary_ja or a.summary or "")[:300]} for a in arts]
    facts = []
    for c, title, published in db.execute(select(Claim, NewsItem.title, NewsItem.published_at).join(NewsItem, NewsItem.id == Claim.article_id)
                                          .where(Claim.entity_id == ent.id).order_by(NewsItem.published_at.desc(), Claim.id).limit(200)):
        facts.append({"id": c.id, "kind": c.kind, "text": c.text, "value": c.value, "article_id": c.article_id, "article_title": title,
                      "published_at": _iso(published), "model": c.model, "prompt_version": c.prompt_version, "ai_extracted": bool(c.ai_extracted)})
    mine = select(ArticleEntity.article_id).where(ArticleEntity.entity_id == ent.id)
    co = db.execute(select(Entity.id, Entity.name, Entity.type, func.count().label("n")).join(ArticleEntity, ArticleEntity.entity_id == Entity.id)
                    .where(ArticleEntity.article_id.in_(mine), Entity.id != ent.id).group_by(Entity.id).order_by(func.count().desc(), Entity.id).limit(10))
    return {"id": ent.id, "name": ent.name, "type": ent.type, "aliases": aliases, "first_seen": _iso(ent.first_seen), "last_seen": _iso(ent.last_seen),
            "article_count": ent.article_count, "timeline": timeline, "facts": facts,
            "related": [{"id": i, "name": n, "type": t, "count": int(c)} for i, n, t, c in co]}


def summary(db: Session) -> dict:
    """診断用: 物・fact の数と、取り出し待ちの記事の数。"""
    from qnapnewshub.db import Community, EntityCard, EntityMerge, MentionCandidate, MergeCandidate

    return {"entities": int(db.scalar(select(func.count(Entity.id))) or 0), "claims": int(db.scalar(select(func.count(Claim.id))) or 0),
            "extracted_articles": int(db.scalar(select(func.count(NewsItem.id)).where(NewsItem.extract_v > 0)) or 0),
            # Things の脳（2026-11-25）: 定義を持つカード・主題のまとまり・つなぐ候補（夜の計算が動いているかを見る）
            "cards": int(db.scalar(select(func.count(EntityCard.entity_id)).where(EntityCard.definition != "")) or 0), "communities": int(db.scalar(select(func.count(Community.id))) or 0),
            "mention_candidates": int(db.scalar(select(func.count(MentionCandidate.id)).where(MentionCandidate.status == "candidate")) or 0),
            # 名寄せ（2026-11-25）: 寄せた数（元に戻した分を除く）・管理者の確認待ちの候補
            "merged": int(db.scalar(select(func.count(EntityMerge.id)).where(EntityMerge.undone_at.is_(None))) or 0),
            "merge_candidates": int(db.scalar(select(func.count(MergeCandidate.id)).where(MergeCandidate.status == "candidate")) or 0),
            # 取り出しの精度（2026-11-25）: 機械の除外で外した物・版の寄せ・取りこぼしの問い直しが済んだ記事・関係の根拠の段階（原文に一致／推測）
            "retired": thing_filter.summary(db)["retired"], "variant_merged": int(db.scalar(select(func.count(EntityMerge.id)).where(EntityMerge.reason == "variant", EntityMerge.undone_at.is_(None))) or 0),
            "gleaned_articles": int(db.scalar(select(func.count(NewsItem.id)).where(NewsItem.extract_v >= 2)) or 0),
            "relations_exact": int(db.scalar(select(func.count(Relationship.id)).where(Relationship.type != "co_occurs", Relationship.status.in_(("extracted", "confirmed")))) or 0),
            "relations_inferred": int(db.scalar(select(func.count(Relationship.id)).where(Relationship.type != "co_occurs", Relationship.status == "inferred")) or 0)}
