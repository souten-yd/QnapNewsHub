"""C5b/C6b: explicit, bounded service for stored public news bodies.

No network, LLM, scheduler or public endpoint. The caller must be authorized
by the existing application before invoking this internal service. Only saved
public/allowed, non-user-specific representative news is supported for now.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select, update
import zstandard

from qnapnewshub.db import ArticleEntity, Document, Entity, Measurement, MeasurementRun, NewsBody, NewsDocument, NewsItem
from qnapnewshub.measure_spans import extract_explicit
from qnapnewshub.measure_tables import markdown_measurements
from qnapnewshub.measure_store import save_draft
from qnapnewshub.measure_search import search

MAX_BODY = 128 * 1024
MAX_DOCS = 20
MAX_SUBJECTS = 10
MAX_DRAFTS = 20
EXTRACTOR = "rule:c6c"
MANAGED = ("rule:c5b", EXTRACTOR)


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
               NewsItem.dup_of.is_(None)).limit(2).execution_options(populate_existing=True)).all()
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
        frame = zstandard.get_frame_parameters(blob)
        if frame.content_size != size or not 0 < frame.window_size <= MAX_BODY:
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


def _entities(db, src: StoredSource):
    entities = db.scalars(select(Entity).join(ArticleEntity, ArticleEntity.entity_id == Entity.id)
                          .where(ArticleEntity.article_id == src.article_id, Entity.type == "model",
                                 Entity.sensitivity == "public", Entity.ai_use == "allowed")
                          .order_by(Entity.id).limit(MAX_SUBJECTS+1).execution_options(populate_existing=True)).all()
    return entities if len(entities) <= MAX_SUBJECTS else None


def _input_hash(src, entities):
    # Changes to extraction semantics must bump EXTRACTOR.
    payload = [src.doc.id, src.article_id, src.digest, src.doc.sensitivity, src.doc.ai_use,
               EXTRACTOR, [(e.id, e.name, e.type, e.sensitivity, e.ai_use) for e in entities]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _drafts(src: StoredSource, entities):
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


def _reusable(db, src, signature, entities):
    run = db.get(MeasurementRun, src.doc.id, populate_existing=True)
    if run is None or run.input_hash != signature or run.doc_hash != src.digest or run.extractor != EXTRACTOR:
        return None
    try:
        ids = json.loads(run.ids_json)
    except (ValueError, TypeError):
        return None
    if (not isinstance(ids, list) or len(ids) > MAX_DRAFTS
            or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids)):
        return None
    valid = set(db.scalars(select(Measurement.id).where(Measurement.id.in_(ids),
                Measurement.doc_id == src.doc.id, Measurement.doc_hash == src.digest,
                Measurement.status == "active", Measurement.sensitivity == src.doc.sensitivity,
                Measurement.ai_use == src.doc.ai_use, Measurement.entity_id.in_([e.id for e in entities]))))
    active_managed = set(db.scalars(select(Measurement.id).where(Measurement.doc_id == src.doc.id,
                    Measurement.status == "active", Measurement.extractor.in_(MANAGED)).limit(MAX_DRAFTS+1)))
    return ids if valid == set(ids) and active_managed <= valid else None


def preview(db, doc_id: int) -> dict:
    src = load_source(db, doc_id)
    if src is None:
        return {"status": "unavailable"}
    entities = _entities(db, src)
    if entities is None:
        return {"status": "limit", "doc_hash": src.digest}
    signature = _input_hash(src, entities)
    reused = _reusable(db, src, signature, entities)
    drafts = None if reused is not None else _drafts(src, entities)
    if reused is None and drafts is None:
        return {"status": "limit", "doc_hash": src.digest}
    return {"status": "preview", "doc_id": doc_id, "doc_hash": src.digest,
            "input_hash": signature, "count": len(reused if reused is not None else drafts),
            "unchanged": reused is not None}


def apply_in_transaction(db, doc_id: int, expected_hash: str, *, expected_input_hash: str) -> dict:
    """Only the writer transaction. Confirm both source and extraction inputs."""
    src = load_source(db, doc_id)
    if src is None or src.digest != expected_hash:
        return {"status": "changed_or_unavailable"}
    entities = _entities(db, src)
    if entities is None:
        return {"status": "limit"}
    signature = _input_hash(src, entities)
    if signature != expected_input_hash:
        return {"status": "changed_or_unavailable"}
    ids = _reusable(db, src, signature, entities)
    if ids is not None:
        return {"status": "saved", "doc_id": doc_id, "doc_hash": src.digest, "ids": ids, "reused": True}
    drafts = _drafts(src, entities)
    if drafts is None:
        return {"status": "limit"}
    ids = [save_draft(db, src.doc, src.text, draft, entity=entity, extractor=EXTRACTOR,
                      reactivate=True).id for entity, draft in drafts]
    # Only service-owned rows; never delete or mutate legacy claims or manual data.
    # Do this after successful extraction, in the same transaction.
    db.execute(update(Measurement).where(Measurement.doc_id == doc_id,
               Measurement.status == "active", Measurement.extractor.in_(MANAGED),
               Measurement.id.not_in(ids)).values(status="superseded"))
    run = db.get(MeasurementRun, doc_id)
    if run is None:
        run = MeasurementRun(doc_id=doc_id)
        db.add(run)
    run.doc_hash, run.input_hash, run.extractor = src.digest, signature, EXTRACTOR
    run.ids_json = json.dumps(ids)
    run.updated_at = datetime.now(timezone.utc)
    db.flush()
    return {"status": "saved", "doc_id": doc_id, "doc_hash": src.digest, "ids": ids, "reused": False}


def apply_confirmed(doc_id: int, expected_hash: str, *, expected_input_hash: str) -> dict:
    """Explicit authorized caller only; no scheduler/API is attached."""
    from qnapnewshub import db as news_db, rest, writer

    def do():
        if rest.blocks_analysis():
            return {"status": "rest"}
        with news_db.session() as db:
            with db.begin():
                return apply_in_transaction(db, doc_id, expected_hash, expected_input_hash=expected_input_hash)

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
