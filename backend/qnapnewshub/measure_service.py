"""C5b/C6b: explicit, bounded service for stored public news bodies.

No network, LLM, scheduler or public endpoint. The caller must be authorized
by the existing application before invoking this internal service. Only saved
public/allowed, non-user-specific representative news is supported for now.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from sqlalchemy import func, select
import zstandard

from qnapnewshub.db import ArticleEntity, Document, Entity, NewsBody, NewsDocument, NewsItem
from qnapnewshub.measure_spans import extract_explicit
from qnapnewshub.measure_tables import markdown_measurements
from qnapnewshub.measure_store import save_draft
from qnapnewshub.measure_search import search

MAX_BODY = 128 * 1024
MAX_DOCS = 20
MAX_SUBJECTS = 10
MAX_DRAFTS = 20


@dataclass(frozen=True)
class StoredSource:
    doc: Document
    article_id: int
    text: str
    digest: str


def load_source(db, doc_id: int) -> StoredSource | None:
    """Read the unique representative body from the DB, never caller text/hash."""
    rows = db.execute(
        select(Document, NewsItem.id, NewsBody.raw_len, NewsBody.state)
        .join(NewsDocument, NewsDocument.document_id == Document.id)
        .join(NewsItem, NewsItem.id == NewsDocument.news_item_id)
        .outerjoin(NewsBody, NewsBody.item_id == NewsItem.id)
        .where(Document.id == doc_id, Document.kind == "news",
               Document.sensitivity == "public", Document.ai_use == "allowed",
               NewsItem.kind != "event", NewsItem.user_id.is_(None),
               NewsItem.dup_of.is_(None)).limit(2)).all()
    if len(rows) != 1:
        return None
    doc, aid, size, state = rows[0]
    if state != 1 or not size or not 0 < size <= MAX_BODY:
        return None
    blob = db.scalar(select(NewsBody.body).where(NewsBody.item_id == aid,
                      NewsBody.state == 1, NewsBody.raw_len == size,
                      func.length(NewsBody.body) <= MAX_BODY * 2))
    if not blob:
        return None
    # Known frame size only; zstd max_output_size alone does not bound a frame
    # that declares its decompressed size. Reject extra frames/trailing bytes.
    try:
        if zstandard.frame_content_size(blob) != size:
            return None
        data = zstandard.ZstdDecompressor(max_window_size=MAX_BODY).decompress(
            blob, max_output_size=MAX_BODY, allow_extra_data=False)
        if len(data) != size:
            return None
        text = data.decode("utf-8")
    except (ValueError, TypeError, UnicodeError, zstandard.ZstdError):
        return None
    if not text.strip():
        return None
    return StoredSource(doc, aid, text, hashlib.sha256(data).hexdigest())


def _drafts(db, src: StoredSource):
    entities = db.scalars(select(Entity).join(ArticleEntity, ArticleEntity.entity_id == Entity.id)
                          .where(ArticleEntity.article_id == src.article_id, Entity.type == "model",
                                 Entity.sensitivity == "public", Entity.ai_use == "allowed")
                          .order_by(Entity.id).limit(MAX_SUBJECTS+1)).all()
    if len(entities) > MAX_SUBJECTS:
        return None
    table = markdown_measurements(src.text)
    candidates = {}
    for ent in entities:
        drafts = extract_explicit(src.text, ent.name)
        drafts += [d for d in table if d.subject.text.casefold() == ent.name.casefold()]
        for d in drafts:
            key = (d.value.start, d.value.end, d.metric_id)
            candidates.setdefault(key, []).append((ent, d))
            if len(candidates) > MAX_DRAFTS:
                return None
    # If several article entities could own the same value, don't choose one.
    return [pairs[0] for pairs in candidates.values() if len(pairs) == 1]


def preview(db, doc_id: int) -> dict:
    src = load_source(db, doc_id)
    if src is None:
        return {"status": "unavailable"}
    drafts = _drafts(db, src)
    if drafts is None:
        return {"status": "limit", "doc_hash": src.digest}
    return {"status": "preview", "doc_id": doc_id, "doc_hash": src.digest, "count": len(drafts)}


def apply_in_transaction(db, doc_id: int, expected_hash: str) -> dict:
    """Call only from the writer transaction. No commit or arbitrary source input."""
    src = load_source(db, doc_id)
    if src is None or src.digest != expected_hash:
        return {"status": "changed_or_unavailable"}
    drafts = _drafts(db, src)
    if drafts is None:
        return {"status": "limit"}
    ids = []
    for entity, draft in drafts:
        ids.append(save_draft(db, src.doc, src.text, draft, entity=entity, extractor="rule:c5b").id)
    return {"status": "saved", "doc_id": doc_id, "doc_hash": src.digest, "ids": ids}


def apply_confirmed(doc_id: int, expected_hash: str) -> dict:
    """Explicit authorized caller only. Use existing writer and honor rest window."""
    from qnapnewshub import db as news_db, rest, writer

    def do():
        if rest.blocks_analysis():
            return {"status": "rest"}
        with news_db.session() as db:
            with db.begin():
                return apply_in_transaction(db, doc_id, expected_hash)

    return writer.run("news.measurement.confirmed", do, user=True)


def search_saved_news(db, *, document_ids: list[int], entity_id: int,
                      metric_id: str, filters: dict[str, str], limit: int = 30) -> dict:
    """Validate actual saved sources in this bounded explicit candidate set."""
    if len(document_ids) > MAX_DOCS or any(type(i) is not int or i <= 0 for i in document_ids):
        raise ValueError("invalid document candidates")
    hashes = {}
    for doc_id in dict.fromkeys(document_ids):
        src = load_source(db, doc_id)
        if src:
            hashes[doc_id] = src.digest
    return {"mode": "explicit_candidates", "checked": len(set(document_ids)),
            "available": len(hashes), "items": search(db, entity_id=entity_id, metric_id=metric_id,
                filters=filters, scopes={"public"}, current_hashes=hashes, limit=limit)}
