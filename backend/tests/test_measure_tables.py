"""目的: C4 の明示的な表と機械出力の変換。根拠: MEASUREMENT.md C4 (2026-11-26)。"""
import json
from qnapnewshub.measure_tables import markdown_measurements, llama_bench_records


def test_table_column_and_row_are_separate():
    src = "| Model | GPU | Decode tok/s | Prefill tok/s |\n| --- | --- | --- | --- |\n| Foo | RTX 4070 | 60 | 420 |\n| Foo | RTX 4070 Ti | 60 | 500 |"
    rows = markdown_measurements(src)
    assert len(rows) == 4
    assert {r.metric_id for r in rows} == {"llm.decode_tps", "llm.prefill_tps"}
    assert {r.conditions[0].span.text for r in rows} == {"RTX 4070", "RTX 4070 Ti"}
    assert all(r.value.valid(src) and r.unit.valid(src) for r in rows)


def test_llama_bench_does_not_mix_pp_and_tg():
    rows = [{"model_type": "Foo", "n_prompt": 512, "n_gen": 0, "avg_ts": 420},
            {"model_type": "Foo", "n_prompt": 0, "n_gen": 128, "avg_ts": 60},
            {"model_type": "Foo", "n_prompt": 512, "n_gen": 128, "avg_ts": 90}]
    out = llama_bench_records(json.dumps(rows))
    assert [r["metric_id"] for r in out] == ["llm.prefill_tps", "llm.decode_tps"]
    assert out[1]["source_path"] == "row:1/avg_ts"


def test_table_skips_negative_and_unbounded_rate():
    src = "| Model | Decode TOK/S |\n| --- | --- |\n| Foo | -60 |\n| Foo | 1e999 |\n| Foo | 60 |"
    rows = markdown_measurements(src)
    assert len(rows) == 1 and rows[0].value.text == "60"
