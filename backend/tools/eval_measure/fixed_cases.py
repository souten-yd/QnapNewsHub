"""Cond 2.0 の固定合成評価。既存処理の純粋関数を直接呼ぶ。DBや通信は使わない。
cd backend && python -m tools.eval_measure.fixed_cases
合否を表示する調査用であり、未実装項目を全体テストの失敗にしない。
"""
from __future__ import annotations

import json
from qnapnewshub import bench


def run():
    cases = [
        ("gpu_suffix", bench.cond_status, ({"hw": "RTX 4070"}, {"hw": "RTX 4070 Ti"}), "other"),
        ("quant_suffix", bench.cond_status, ({"quant": "Q4"}, {"quant": "Q4_K_M"}), "other"),
        ("alias", bench.cond_status, ({"hw": "R9700"}, {"hw": "AMD Radeon AI PRO R9700"}), "matching"),
        ("missing", bench.cond_status, ({"ctx": "32K"}, {}), "unspecified"),
        ("ambig_gpu", bench.parse_cond, ("RTX 4070 and RTX 4090",), {}),
        ("explicit_gpu_quant", bench.parse_cond, ("RTX 4070 Q4_K_M",), {"hw": "RTX 4070", "quant": "Q4_K_M"}),
        ("mixed_engine_backend", bench.parse_cond, ("llama.cpp ROCm",), {"engine": "llama.cpp", "backend": "ROCm"}),
        ("bare_Q4", bench.parse_cond, ("RTX 4070 Q4",), {"hw": "RTX 4070", "quant": "Q4"}),
        ("separate_units", bench.parse_value, ("60", "with 24 GB RAM; score 60"), (60.0, "")),
        ("signed_value", bench.parse_value, ("-60", "60 tok/s; -60 W"), (-60.0, "w")),
        ("ambiguous_unit", bench.parse_value, ("60", "60 tok/s and 60 W"), (60.0, "")),
        ("currency", bench.parse_value, ("$1,299", ""), (1299.0, "usd")),
        ("range", bench.parse_value, ("40–60 tok/s", ""), (None, "")),
        ("scientific_notation", bench.parse_value, ("1e3 tok/s", ""), (1000.0, "tok/s")),
        ("decode", bench.metric_id, ("benchmark", "decode 60 tok/s", "tok/s"), "llm.decode_tps"),
        ("prefill", bench.metric_id, ("benchmark", "prefill 420 tok/s", "tok/s"), "llm.prefill_tps"),
        ("aggregate", bench.metric_id, ("benchmark", "total throughput 180 tok/s", "tok/s"), "llm.throughput_total_tps"),
        ("per_user", bench.metric_id, ("benchmark", "decode 60 tok/s per user", "tok/s"), "llm.throughput_per_user_tps"),
    ]
    rows = []
    for name, fn, args, expected in cases:
        got = fn(*args)
        rows.append({"case": name, "pass": got == expected, "got": got, "expected": expected})
    return {"mode": "synthetic", "passed": sum(r["pass"] for r in rows), "total": len(rows), "cases": rows}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
