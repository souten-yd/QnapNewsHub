"""C4: 既知のベンチマーク形式を LLM なしで読む。ネットワーク・書き込みなし。
Markdown は原文 span を保持。JSON/CSV はフィールドのパスを保持し、span が無いので
Measurement へ直接保存しない（原文との照合は後続）。
"""
from __future__ import annotations

import csv
import io
import json
import re
from qnapnewshub.measure_spans import Span, Candidate, MeasurementDraft
from qnapnewshub.measure_vocab import normalize_quantity
from qnapnewshub.measure_quantity import NUMBER

_METRICS = {"decode tok/s": "llm.decode_tps", "prefill tok/s": "llm.prefill_tps",
            "total tok/s": "llm.throughput_total_tps", "per user tok/s": "llm.throughput_per_user_tps", "tok/s": "llm.tps_unspecified"}
_CONDS = {"gpu": "hw", "quant": "quant", "engine": "engine", "backend": "backend",
          "concurrency": "concurrency"}
_NUMBER = re.compile(NUMBER)
_MAX_INPUT = 2_000_000
_MAX_ROWS = 10000


def _cells(line: str, offset: int) -> list[Span]:
    # Escape/結合セル等は推測しない。
    if not line.strip().startswith("|") or not line.strip().endswith("|") or "\\|" in line:
        return []
    bars = [m.start() for m in re.finditer(r"\|", line)]
    cells = []
    for a, b in zip(bars, bars[1:]):
        raw = line[a+1:b]
        begin = offset + a + 1 + len(raw) - len(raw.lstrip())
        cells.append(Span(begin, begin + len(raw.strip()), raw.strip()))
    return cells


def markdown_measurements(source: str) -> list[MeasurementDraft]:
    if len(source) > _MAX_INPUT:
        raise ValueError("table too large")
    rows, pos = [], 0
    for line in source.splitlines(keepends=True):
        rows.append(_cells(line.rstrip("\r\n"), pos))
        pos += len(line)
    out, i = [], 0
    while i+1 < len(rows):
        header, separator = rows[i], rows[i+1]
        names = [h.text.casefold() for h in header]
        if (not names or len(names) != len(set(names)) or "model" not in names
                or len(header) != len(separator)
                or not all(re.fullmatch(r":?-{3,}:?", c.text) for c in separator)):
            i += 1
            continue
        metrics = [(j, _METRICS[n]) for j, n in enumerate(names) if n in _METRICS]
        if not metrics:
            i += 1
            continue
        i += 2
        while i < len(rows) and len(rows[i]) == len(header):
            row = rows[i]
            subject = row[names.index("model")]
            conds = tuple(Candidate(_CONDS[n], row[j]) for j, n in enumerate(names)
                          if n in _CONDS and row[j].text and row[j].text not in ("-", "—", "N/A"))
            for col, mid in metrics:
                value = row[col]
                if not subject.text or not _NUMBER.fullmatch(value.text):
                    continue
                h = header[col]
                # 単位は列ヘッダーの原文。条件の列は同じ行のみ。
                unit = Span(h.end-5, h.end, h.text[-5:])
                if unit.text.casefold() != "tok/s":
                    continue
                out.append(MeasurementDraft(subject, value, unit, mid, f"table:{header[0].start}:row:{row[0].start}", conds))
                if len(out) >= _MAX_ROWS:
                    return out
            i += 1
    return out


def llama_bench_records(raw: str, *, fmt: str = "json") -> list[dict]:
    """llama-bench の明示的な pp/tg 行のみ。pp+tg 混在は除外。"""
    if len(raw) > _MAX_INPUT:
        raise ValueError("input too large")
    if fmt == "json":
        rows = json.loads(raw)
    elif fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(raw)))
    else:
        raise ValueError("unsupported format")
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        raise ValueError("invalid rows")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            pp, tg = int(row["n_prompt"]), int(row["n_gen"])
            if pp < 0 or tg < 0 or bool(pp) == bool(tg):
                continue
            value, unit, _ = normalize_quantity(str(row["avg_ts"]), "tok/s")
            if value < 0:
                continue
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
        model = row.get("model_type") or row.get("model_filename")
        if not isinstance(model, str) or not model.strip():
            continue
        cond = {k: row[k] for k in ("gpu_info", "cpu_info", "backends", "n_gpu_layers", "n_threads",
                                     "n_batch", "n_ubatch", "type_k", "type_v", "flash_attn", "build_commit")
                if k in row and isinstance(row[k], (str, int, float, bool))}
        cond.update({"n_prompt": pp, "n_gen": tg})
        out.append({"subject": model, "metric_id": "llm.prefill_tps" if pp else "llm.decode_tps",
                    "value": str(value), "unit": unit, "stat": "mean",
                    "conditions_raw": cond, "source_path": f"row:{i}/avg_ts"})
    return out
