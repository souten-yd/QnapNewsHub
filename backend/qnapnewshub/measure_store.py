"""C3: 測定候補の厳密な span 検証と追加保存。内部の呼び出し専用。
本文を読む権限・正本の本文取得は呼び出し側で確保する。自動処理/API はまだ接続しない。
"""
from __future__ import annotations

import hashlib
import json
import math

from sqlalchemy import select
from qnapnewshub.db import Document, Entity, Measurement, MeasurementCond
from qnapnewshub.measure_spans import MeasurementDraft
from qnapnewshub.measure_vocab import LLM_KEYS, normalize_condition, normalize_quantity


def save_draft(db, doc: Document, source: str, draft: MeasurementDraft, *, entity: Entity | None = None,
               extractor: str = "rule:c3") -> Measurement:
    if doc.id is None or doc.ai_use == "prohibited":
        raise ValueError("document unavailable for analysis")
    if entity is not None and (entity.sensitivity != doc.sensitivity or entity.ai_use != doc.ai_use):
        raise ValueError("entity/document scope mismatch")
    spans = [draft.subject, draft.value, draft.unit] + [c.span for c in draft.conditions]
    if not all(s.valid(source) for s in spans):
        raise ValueError("unverified source span")
    # Verify the relation, not just independent literal spans. Only reconstructed
    # candidates from the same prose clause or parsed Markdown row can be saved.
    if len(source.encode("utf-8")) > 1_000_000:
        raise ValueError("source too large")
    from qnapnewshub.measure_spans import extract_explicit
    from qnapnewshub.measure_tables import markdown_measurements
    candidates = (markdown_measurements(source) if draft.block_id.startswith("table:")
                  else extract_explicit(source, draft.subject.text))
    core = (draft.subject, draft.value, draft.unit, draft.metric_id, draft.block_id)
    if not any((d.subject, d.value, d.unit, d.metric_id, d.block_id) == core
               and set(draft.conditions) <= set(d.conditions) for d in candidates):
        raise ValueError("unverified measurement relation")
    keys = [c.key for c in draft.conditions]
    if len(keys) != len(set(keys)) or not set(keys) <= LLM_KEYS:
        raise ValueError("invalid condition keys")
    if not draft.metric_id.startswith("llm."):
        raise ValueError("unsupported metric")
    value, unit, _ = normalize_quantity(draft.value.text, draft.unit.text)
    if not math.isfinite(float(value)):
        raise ValueError("nonfinite measurement")
    digest = hashlib.sha256(source.encode()).hexdigest()
    conds = sorted((c.key, normalize_condition(c.key, c.span.text), c.span.start, c.span.end)
                   for c in draft.conditions)
    identity = (doc.id, digest, entity.id if entity else None, draft.subject.text, draft.metric_id,
                str(value.normalize()), unit, draft.value.start, draft.value.end, conds)
    fp = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    old = db.scalar(select(Measurement).where(Measurement.fingerprint == fp))
    if old is not None:
        return old
    row = Measurement(doc_id=doc.id, doc_hash=digest, entity_id=entity.id if entity else None,
                      subject_text=draft.subject.text, metric_id=draft.metric_id, value=float(value),
                      value_text=draft.value.text, unit=unit, unit_text=draft.unit.text,
                      sensitivity=doc.sensitivity, ai_use=doc.ai_use, stated_at=doc.published_at,
                      span_start=draft.value.start, span_end=draft.value.end, block_id=draft.block_id,
                      extractor=extractor, confidence="exact", fingerprint=fp)
    db.add(row)
    db.flush()
    for c in draft.conditions:
        db.add(MeasurementCond(measurement_id=row.id, key=c.key, value_text=c.span.text,
                               value_norm=normalize_condition(c.key, c.span.text), scope="explicit",
                               span_start=c.span.start, span_end=c.span.end, extractor=extractor, confidence="exact"))
    db.flush()
    return row
