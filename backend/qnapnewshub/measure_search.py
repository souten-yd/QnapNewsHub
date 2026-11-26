"""C5: 内部の条件付き検索。呼び出し側が認可済み scopes と対象を指定する。
原文hashを現在の正本から取得できた文書だけ扱う。公開 API にはまだ接続しない。
"""
from __future__ import annotations

from collections import defaultdict
from sqlalchemy import select
from qnapnewshub.db import Document, Entity, Measurement, MeasurementCond
from qnapnewshub.measure_vocab import LLM_KEYS, PRICE_KEYS, normalize_condition


def condition_match(wanted: dict[str, str], actual: dict[str, str]) -> tuple[str, dict[str, str]]:
    if not set(wanted) <= LLM_KEYS | PRICE_KEYS or any(not str(v).strip() for v in wanted.values()):
        raise ValueError("invalid condition filter")
    fields = {}
    for k, v in wanted.items():
        fields[k] = ("unknown" if k not in actual else
                     "exact" if normalize_condition(k, v) == actual[k] else "mismatch")
    state = "other" if "mismatch" in fields.values() else "unspecified" if "unknown" in fields.values() else "matching"
    return state, fields


def search(db, *, entity_id: int, metric_id: str, filters: dict[str, str],
           scopes: set[str], current_hashes: dict[int, str], limit: int = 30) -> list[dict]:
    """指定指標だけ検索する。失効した span・機密・利用禁止の文書を返さない。
    current_hashes は呼び出し側が取得した現在の正本本文の SHA256。欠落は除外。
    結果は測定単位で返し、不明を平均しない。上位候補は最大 1000 件。
    """
    condition_match(filters, {})
    if not scopes or not current_hashes or not metric_id:
        return []
    if not scopes <= {"public", "confidential"}:
        raise ValueError("invalid scopes")
    entity = db.get(Entity, entity_id)
    if entity is None or entity.sensitivity not in scopes or entity.ai_use == "prohibited":
        return []
    query = (select(Measurement, Document).join(Document, Document.id == Measurement.doc_id)
             .where(Measurement.entity_id == entity_id, Measurement.metric_id == metric_id,
                    Measurement.status == "active", Measurement.sensitivity.in_(scopes),
                    Document.sensitivity.in_(scopes), Measurement.ai_use != "prohibited",
                    Document.ai_use != "prohibited", Measurement.doc_id.in_(list(current_hashes)[:1000]))
             .order_by(Measurement.id.desc()).limit(1000))
    rows = [(m, d) for m, d in db.execute(query) if current_hashes.get(d.id) == m.doc_hash]
    ids = [m.id for m, _ in rows]
    conds = defaultdict(dict)
    for c in db.scalars(select(MeasurementCond).where(MeasurementCond.measurement_id.in_(ids))):
        conds[c.measurement_id][c.key] = c.value_norm
    out = []
    for m, d in rows:
        cond = conds[m.id]
        status, fields = condition_match(filters, cond)
        out.append({"id": m.id, "metric_id": m.metric_id, "value": m.value, "unit": m.unit,
                    "conditions": dict(cond), "status": status, "fields": fields,
                    "doc_id": d.id, "doc_hash": m.doc_hash, "url": d.url,
                    "span": [m.span_start, m.span_end]})
    priority = {"matching": 0, "unspecified": 1, "other": 2}
    out.sort(key=lambda r: (priority[r["status"]], -r["id"]))
    return out[:max(1, min(int(limit), 100))]
