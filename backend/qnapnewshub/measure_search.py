"""C5b: bounded internal measurement search, with current-source and policy checks.

Only the service may derive current_hashes from stored source. The low-level
function accepts those verified hashes, not an HTTP request's parameters.
"""
from __future__ import annotations

import re
from collections import defaultdict
from sqlalchemy import case, exists, or_, select, tuple_
from qnapnewshub.db import Document, Entity, Measurement, MeasurementCond
from qnapnewshub.measure_vocab import LLM_KEYS, PRICE_KEYS, normalize_condition

MAX_DOCS = 200
MAX_FILTERS = 16


def condition_match(wanted: dict[str, str], actual: dict[str, str]) -> tuple[str, dict[str, str]]:
    if (len(wanted) > MAX_FILTERS or not set(wanted) <= LLM_KEYS | PRICE_KEYS
            or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in wanted.values())):
        raise ValueError("invalid condition filter")
    fields = {}
    for k, v in wanted.items():
        fields[k] = ("unknown" if k not in actual else
                     "exact" if normalize_condition(k, v) == actual[k] else "mismatch")
    state = "other" if "mismatch" in fields.values() else "unspecified" if "unknown" in fields.values() else "matching"
    return state, fields


def search(db, *, entity_id: int, metric_id: str, filters: dict[str, str],
           scopes: set[str], current_hashes: dict[int, str], limit: int = 30,
           local_only: bool = False) -> list[dict]:
    """Rank matching/unknown/mismatch in SQL BEFORE the final bounded limit.

    Public allowed is the default even if a caller passes confidential scope.
    local_only=True is reserved for explicitly authorized on-device callers.
    Old rows with changed source hashes or changed policy are excluded.
    """
    condition_match(filters, {})
    if (not 1 <= limit <= 100 or len(current_hashes) > MAX_DOCS
            or not isinstance(metric_id, str) or not metric_id or len(metric_id) > 80):
        raise ValueError("invalid search bounds")
    if not scopes <= {"public", "confidential"}:
        raise ValueError("invalid scopes")
    if any(type(i) is not int or i <= 0 or not isinstance(h, str) or not re.fullmatch("[0-9a-f]{64}", h)
           for i, h in current_hashes.items()):
        raise ValueError("invalid source hashes")
    visible = scopes if local_only else scopes & {"public"}
    uses = {"allowed", "local_only"} if local_only else {"allowed"}
    if not visible or not current_hashes:
        return []
    # Same conditions as the Python classification; EXISTS avoids row multiplication.
    mismatches, unknowns, matches = [], [], []
    for key, want in filters.items():
        predicate = (MeasurementCond.measurement_id == Measurement.id, MeasurementCond.key == key)
        # Older text folding could collapse "-1" to "1". For offload compare
        # the literal canonical integer evidence, not the legacy folded value.
        column = MeasurementCond.value_text if key == "offload" else MeasurementCond.value_norm
        target = normalize_condition(key, want)
        mismatches.append(exists(select(1).where(*predicate, column != target)))
        unknowns.append(~exists(select(1).where(*predicate)))
        matches.append(case((exists(select(1).where(*predicate, column == target)), 1), else_=0))
    priority = case((or_(*mismatches), 2), (or_(*unknowns), 1), else_=0) if filters else Measurement.id * 0
    coverage = sum(matches) if matches else Measurement.id * 0
    query = (select(Measurement, Document).join(Document, Document.id == Measurement.doc_id)
             .join(Entity, Entity.id == Measurement.entity_id)
             .where(Measurement.entity_id == entity_id, Measurement.metric_id == metric_id,
                    Measurement.status == "active", Document.sensitivity.in_(visible),
                    Document.ai_use.in_(uses), Measurement.sensitivity == Document.sensitivity,
                    Measurement.ai_use == Document.ai_use, Document.sensitivity == Entity.sensitivity,
                    Document.ai_use == Entity.ai_use,
                    tuple_(Measurement.doc_id, Measurement.doc_hash).in_(list(current_hashes.items())))
             .order_by(priority, coverage.desc(), Measurement.id.desc()).limit(limit)
             .execution_options(populate_existing=True))
    rows = list(db.execute(query))
    ids = [m.id for m, _ in rows]
    if not ids:
        return []
    conds = defaultdict(dict)
    for c in db.scalars(select(MeasurementCond).where(MeasurementCond.measurement_id.in_(ids))):
        conds[c.measurement_id][c.key] = c.value_text if c.key == "offload" else c.value_norm
    out = []
    for m, d in rows:
        cond = conds[m.id]
        status, fields = condition_match(filters, cond)
        out.append({"id": m.id, "metric_id": m.metric_id, "value": m.value, "unit": m.unit,
                    "conditions": dict(cond), "status": status, "fields": fields,
                    "doc_id": d.id, "doc_hash": m.doc_hash, "url": d.url,
                    "sensitivity": d.sensitivity, "ai_use": d.ai_use,
                    "span": [m.span_start, m.span_end]})
    return out
