"""目的: 固定評価 #50 の5件と誤数値回帰。根拠: Pint数量、llama-bench、vLLM、2026-11-26。"""
from decimal import Decimal

import pytest
from qnapnewshub import bench, measure_quantity as mq
from qnapnewshub.measure_spans import extract_explicit
from tools.eval_measure.fixed_cases import run


def test_previous_fixed_cases():
    result = run()
    assert result["passed"] == result["total"], result["cases"]


def test_exact_decimal_and_interval():
    assert mq.decimal_value("1e3") == Decimal(1000)
    assert mq.decimal_value("−1.2e-2") == Decimal("-0.012")
    assert mq.interval_value("40–60") == (Decimal(40), Decimal(60))
    assert mq.interval_value("-60 to -40") == (Decimal(-60), Decimal(-40))
    for s in ("1,2", "NaN", "1e999", "40–60", "60±5"):
        with pytest.raises(ValueError):
            mq.decimal_value(s)


def test_scalar_legacy_does_not_launder_ranges():
    for s in ("40–60 tok/s", "40-60 tok/s", "40 to 60 tok/s", "60±5 tok/s", "about 60 tok/s", "≤60 tok/s"):
        assert bench.parse_value(s) == (None, "")
        assert extract_explicit("Foo decode " + s, "Foo") == []
    assert bench.parse_value("1e3 tok/s") == (1000.0, "tok/s")
    row = extract_explicit("Foo decode 1e3 tok/s on RTX 4070 Q4 with llama.cpp ROCm.", "Foo")[0]
    assert row.value.text == "1e3"
    assert {c.key:c.span.text for c in row.conditions} == {"hw":"RTX 4070","quant":"Q4","engine":"llama.cpp","backend":"ROCm"}


def test_per_request_is_not_per_user_or_aggregate():
    assert bench.metric_id("benchmark", "60 tok/s per request", "tok/s") == "llm.throughput_per_request_tps"
    assert bench.metric_id("benchmark", "60 tok/s per user", "tok/s") == "llm.throughput_per_user_tps"
    assert bench.metric_id("benchmark", "aggregate 60 tok/s per user", "tok/s") == "llm.tps_unspecified"
