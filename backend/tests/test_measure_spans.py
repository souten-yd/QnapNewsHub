"""目的: C3 原文位置の検証と同値別測定の分離。根拠: MEASUREMENT.md C3、2026-11-26。"""
from dataclasses import replace
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from qnapnewshub.db import Base, Document, Measurement, MeasurementCond
from qnapnewshub.measure_spans import extract_explicit, Span
from qnapnewshub.measure_store import save_draft


def test_exact_source_spans_and_idempotence():
    source = "Foo decode 60 tok/s on RTX 4070 Q4_K_M. Foo decode 60 tok/s on RTX 4070 Ti Q8_0."
    rows = extract_explicit(source, "Foo")
    assert len(rows) == 2
    assert all(c.span.valid(source) for r in rows for c in r.conditions)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        doc = Document(kind="news", ident="url:test", sensitivity="confidential", ai_use="local_only")
        db.add(doc)
        db.flush()
        a, b = [save_draft(db, doc, source, r) for r in rows]
        assert a.id != b.id and a.sensitivity == "confidential"
        assert save_draft(db, doc, source, rows[0]).id == a.id
        assert len(db.scalars(select(Measurement)).all()) == 2
        assert {c.value_text for c in db.scalars(select(MeasurementCond)).all() if c.key == "hw"} == {"RTX 4070", "RTX 4070 Ti"}
        with pytest.raises(ValueError):
            save_draft(db, doc, source, replace(rows[0], value=Span(0, 3, "60")))
