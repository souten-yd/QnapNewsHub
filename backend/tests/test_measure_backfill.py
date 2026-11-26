"""目的: C6b 段階的な再抽出の読み取り専用プレビュー。根拠: MEASUREMENT.md C6b。"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from qnapnewshub.db import Base
from qnapnewshub.measure_backfill import plan_article, preview


def test_plan_is_deterministic_and_counts_distinct_values():
    src = "Foo decode 60 tok/s on RTX 4070. Foo decode 60 tok/s on RTX 4090."
    first = plan_article(src, ["Foo", "foo"])
    assert first == plan_article(src, ["Foo"])
    assert first["measurements"] == 2 and first["by_metric"] == {"llm.decode_tps": 2}


def test_empty_preview_and_bounds():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert preview(db) == {"version": "measure-c6b-preview-1", "after_id": 0,
                               "scanned": 0, "items": [], "done": True, "mode": "read_only_preview"}
        with pytest.raises(ValueError):
            preview(db, limit=21)
        assert not db.new and not db.dirty and not db.deleted
