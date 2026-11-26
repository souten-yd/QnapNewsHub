"""目的: 独立原文注釈と予測の評価。根拠: MEASUREMENT.md C6a (2026-11-26)。
同じ値の別測定、条件の誤結合、不明、重複検索結果を区別する。外部通信なし。
"""
from copy import deepcopy

import pytest
from tools.eval_measure.__main__ import evaluate


def test_measurement_and_condition_link_are_scored_separately():
    text = "Foo: 60 tok/s RTX 4070; Foo: 60 tok/s RTX 4090"
    a = text.index("60")
    b = text.rindex("60")
    hw = text.index("RTX 4070")
    hw2 = text.index("RTX 4090")
    def row(rid, pos, gpu, start):
        return {"id": rid, "doc_id": "d", "subject": "Foo", "metric_id": "llm.decode_tps",
                "value_span": [pos, pos+2], "unit": "tok/s",
                "conditions": {"hw": {"value": gpu, "span": [start, start+8]}}}
    gold = {"documents": [{"id": "d", "text": text}],
            "measurements": [row("a", a, "RTX 4070", hw), row("b", b, "RTX 4090", hw2)],
            "questions": [{"id": "q", "relevant": ["a", "b"]}]}
    pred = deepcopy(gold)
    pred["measurements"][0]["conditions"] = deepcopy(gold["measurements"][1]["conditions"])
    result = evaluate(gold, pred, {"q": ["a", "a", "missing"]})
    assert result["measurement"]["f1"] == 1
    assert result["condition_link"]["precision"] == 0.5
    assert result["retrieval"]["r@10"] == 0.5
    pred["measurements"][0]["conditions"]["hw"]["value"] = "fake"
    with pytest.raises(ValueError):
        evaluate(gold, pred)
