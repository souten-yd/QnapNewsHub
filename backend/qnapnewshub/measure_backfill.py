"""C6b: 既存記事の段階的な再抽出を計画する読み取り専用ツール。
本番スケジューラ・LLM・書き込みには接続しない。正本を消さず、適用前に評価する。
"""
from __future__ import annotations

import hashlib
from collections import Counter
from sqlalchemy import select

from qnapnewshub.db import NewsItem, NewsBody, NewsDocument, Document, Entity, ArticleEntity
from qnapnewshub.measure_spans import extract_explicit
from qnapnewshub.measure_tables import markdown_measurements
from qnapnewshub import ztext

VERSION = "measure-c6b-preview-1"
MAX_ARTICLES = 20
MAX_BODY_BYTES = 1_000_000
MAX_ENTITIES = 10


def plan_article(source: str, subjects: list[str]) -> dict:
    """合成・本番コピーの同じ原文に適用できる、決定的な候補計数。"""
    if len(source.encode("utf-8")) > MAX_BODY_BYTES:
        return {"status": "too_large", "measurements": 0, "by_metric": {}}
    names = list(dict.fromkeys(s.casefold() for s in subjects if s))[:MAX_ENTITIES]
    drafts = [d for subject in names for d in extract_explicit(source, subject)]
    drafts.extend(d for d in markdown_measurements(source) if d.subject.text.casefold() in names)
    keys = {(d.value.start, d.value.end, d.subject.start, d.metric_id): d for d in drafts}
    counts = Counter(d.metric_id for d in keys.values())
    return {"status": "preview", "doc_hash": hashlib.sha256(source.encode()).hexdigest(),
            "measurements": len(keys), "by_metric": dict(counts)}


def preview(db, *, after_id: int = 0, limit: int = MAX_ARTICLES) -> dict:
    """public/allowed の保存済み本文だけ。カーソルで少しずつ確認。
    件数と文書 ID/hash のみ返す。本文や LLM 入力をログに出さない。DB を変更しない。
    """
    if not 1 <= limit <= MAX_ARTICLES or after_id < 0:
        raise ValueError("invalid preview bounds")
    query = (select(NewsItem.id, NewsBody, Document).join(NewsBody, NewsBody.item_id == NewsItem.id)
             .join(NewsDocument, NewsDocument.news_item_id == NewsItem.id)
             .join(Document, Document.id == NewsDocument.document_id)
             .where(NewsItem.id > after_id, NewsItem.dup_of.is_(None),
                    NewsBody.state == 1, Document.sensitivity == "public", Document.ai_use == "allowed")
             .order_by(NewsItem.id).limit(limit))
    out = []
    cursor = after_id
    for aid, body, doc in db.execute(query):
        cursor = aid
        if body.raw_len > MAX_BODY_BYTES or body.body is None:
            out.append({"article_id": aid, "doc_id": doc.id, "status": "too_large_or_missing"})
            continue
        try:
            source = ztext.unpack(body.body)
        except (ValueError, TypeError):
            out.append({"article_id": aid, "doc_id": doc.id, "status": "unreadable"})
            continue
        names = list(db.scalars(select(Entity.name).join(ArticleEntity, ArticleEntity.entity_id == Entity.id)
                                .where(ArticleEntity.article_id == aid, Entity.type == "model",
                                       Entity.sensitivity == "public", Entity.ai_use == "allowed")
                                .order_by(Entity.id).limit(MAX_ENTITIES)))
        out.append({"article_id": aid, "doc_id": doc.id, **plan_article(source, names)})
    return {"version": VERSION, "after_id": cursor, "scanned": len(out), "items": out,
            "done": len(out) < limit, "mode": "read_only_preview"}
