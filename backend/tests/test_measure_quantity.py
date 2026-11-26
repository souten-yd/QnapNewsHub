"""目的: 数値の字句（指数・範囲・近似・誤差）を単一の値と取り違えないこと、速さの指標の区別。
根拠: 2026-11-26 Codex の #51（Pint の数量の考え・llama-bench・vLLM の指標の分け方）から、実際に動く数値パスと比較で使う部分だけを採った。"""
from decimal import Decimal

import pytest
from qnapnewshub import bench, measure_quantity as mq

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
    assert bench.parse_value("1e3 tok/s") == (1000.0, "tok/s")
    assert bench.parse_cond("Foo decode 1e3 tok/s on RTX 4070 Q4 with llama.cpp ROCm.") == {"hw": "RTX 4070", "quant": "Q4", "engine": "llama.cpp", "backend": "ROCm"}


def test_per_request_is_not_per_user_or_aggregate():
    assert bench.metric_id("benchmark", "60 tok/s per request", "tok/s") == "llm.throughput_per_request_tps"
    assert bench.metric_id("benchmark", "60 tok/s per user", "tok/s") == "llm.throughput_per_user_tps"
    assert bench.metric_id("benchmark", "aggregate 60 tok/s per user", "tok/s") == "llm.tps_unspecified"


@pytest.mark.parametrize("text", ["40 tok/s–60 tok/s", "1,2 tok/s", "60 tok/s以上",
                                  "60 tok/s程度", "60 tok/s±5 tok/s", "60±5 tok/s"])
def test_extended_scalar_occurrence_rejects_non_scalar(text):
    assert list(mq.scalar_tokens(text)) == []
