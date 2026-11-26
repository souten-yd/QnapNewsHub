"""目的: C2 の加算的な表と単位正規化。根拠: MEASUREMENT.md §2（2026-11-26）。外部通信なし。"""
from sqlalchemy import create_engine, inspect
from qnapnewshub.db import Base
from qnapnewshub.measure_vocab import normalize_quantity, normalize_condition


def test_schema_additive_and_idempotent():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Base.metadata.create_all(engine)
    assert {"claims", "measurements", "measurement_conds", "measurement_conflicts"} <= set(inspect(engine).get_table_names())
    assert any(x["name"] == "ix_measure_cond_lookup" for x in inspect(engine).get_indexes("measurement_conds"))


def test_units_and_exact_aliases():
    assert normalize_quantity("1", "s") == normalize_quantity("1000", "ms")
    assert normalize_quantity("1", "GB") != normalize_quantity("1", "GiB")
    assert normalize_quantity("1", "USD")[2] != normalize_quantity("1", "JPY")[2]
    assert normalize_condition("hw", "RTX 4070") != normalize_condition("hw", "RTX 4070 Ti")
