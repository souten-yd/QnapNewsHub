"""Explicit benchmark layouts. Candidates only: no DB, HTTP or inferred conditions."""
from __future__ import annotations

import hashlib
import re
from qnapnewshub.measure_quantity import NUMBER, decimal_value

MAX_INPUT = 256_000
MAX_RECORDS = 500
_TEST = re.compile(r"(pp|tg)\s*([1-9]\d{0,7})(?:\s*@\s*d([0-9]{1,8}))?", re.I)
_MATRIX = re.compile(r"(ROCm|HIP|CUDA|Vulkan|Metal)\s+(pp|tg)\s*([1-9]\d{0,7})", re.I)
_MEAN_SD = re.compile(r"(" + NUMBER + r")\s*(?:±|\+/-)\s*(" + NUMBER + r")")
_RATE = re.compile(r"(" + NUMBER + r")\s*(?:t/s|tok/s|tokens/s)", re.I)
_SEP = re.compile(r":?-{2,}:?")


def _number(text):
    value = decimal_value(text)
    if value < 0:
        raise ValueError("negative benchmark result")
    return str(value)


def _cells(line):
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|") or "\\|" in line:
        return []
    return [v.strip() for v in line[1:-1].split("|")]


def _metric(kind):
    return "llm.prefill_tps" if kind.casefold() == "pp" else "llm.decode_tps"


def _record(raw_hash, subject, metric, value, stat, unit, conditions, line, col, **extra):
    return {"subject": subject, "metric_id": metric, "value": value, "unit": unit,
            "stat": stat, "conditions_raw": conditions,
            "evidence": {"source_sha256": raw_hash, "line": line, "column": col}, **extra}


def benchmark_markdown_records(raw: str) -> list[dict]:
    """llama-bench native tables and explicit backend/pp/tg matrix columns.

    Keep printed model labels (including footnotes); never inherit a model,
    device, quantization or build from an unrelated paragraph. t/s is treated
    as tokens/s only in these explicitly recognized benchmark layouts.
    """
    if len(raw) > MAX_INPUT:
        raise ValueError("benchmark input limit")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    lines = raw.splitlines()
    out, i = [], 0
    while i + 1 < len(lines):
        header = _cells(lines[i])
        keys = [x.casefold() for x in header]
        sep = _cells(lines[i+1])
        if (not keys or len(keys) != len(set(keys)) or "model" not in keys
                or len(sep) != len(keys) or not all(_SEP.fullmatch(x) for x in sep)):
            i += 1
            continue
        native = ({"model", "size", "params", "backend", "test", "t/s"} <= set(keys)
                  and ("ngl" in keys or "threads" in keys))
        columns = [(c, m) for c,h in enumerate(header) if (m := _MATRIX.fullmatch(h))]
        if not native and (not columns or len({(m[1].casefold(),m[2].casefold(),m[3]) for _,m in columns}) != len(columns)):
            i += 1
            continue
        header_line = i + 1
        i += 2
        while i < len(lines):
            cells = _cells(lines[i])
            if len(cells) != len(keys) or (i+1 < len(lines) and len(_cells(lines[i+1])) == len(keys)
                    and all(_SEP.fullmatch(x) for x in _cells(lines[i+1]))):
                break
            subject = cells[keys.index("model")]
            base = {k: cells[keys.index(k)] for k in ("ngl", "size", "params", "file", "n_batch", "threads") if k in keys}
            if subject:
                if native:
                    test = _TEST.fullmatch(cells[keys.index("test")])
                    col = keys.index("t/s")
                    value = _MEAN_SD.fullmatch(cells[col])
                    if test and value:
                        try:
                            avg, sd = _number(value[1]), _number(value[2])
                            conditions = {**base,"backend":cells[keys.index("backend")],
                                          "n_prompt" if test[1].casefold()=="pp" else "n_gen":int(test[2])}
                            if test[3] is not None:
                                conditions["n_depth"] = int(test[3])
                            out.append(_record(digest,subject,_metric(test[1]),avg,"mean","tok/s",conditions,
                                               i+1,col,stddev=sd,header_line=header_line,value_text=cells[col]))
                        except ValueError:
                            pass
                else:
                    for col, m in columns:
                        cell = cells[col]
                        unstyled = cell[2:-2] if cell.startswith("**") and cell.endswith("**") else cell
                        value = _RATE.fullmatch(unstyled)
                        if not value:
                            continue
                        try:
                            conditions = {**base,"backend":m[1],"n_prompt" if m[2].casefold()=="pp" else "n_gen":int(m[3])}
                            out.append(_record(digest,subject,_metric(m[2]),_number(value[1]),"unknown","tok/s",conditions,
                                               i+1,col,header_line=header_line,value_text=cell))
                        except ValueError:
                            pass
            if len(out) > MAX_RECORDS:
                raise ValueError("benchmark candidate limit")
            i += 1
    return out


_LOG_START = re.compile(r"=+\s*Serving Benchmark Result\s*=+", re.I)
_LOG_END = re.compile(r"={10,}")
_LOG_LINE = re.compile(r"([^:]{1,100}):\s*(" + NUMBER + r")")
_LOG_METRICS = {
    "request throughput (req/s)": ("llm.request_throughput_rps", "req/s", "unknown"),
    "output token throughput (tok/s)": ("llm.output_throughput_tps", "tok/s", "unknown"),
    "total token throughput (tok/s)": ("llm.total_token_throughput_tps", "tok/s", "unknown"),
    "peak output token throughput (tok/s)": ("llm.peak_output_throughput_tps", "tok/s", "peak"),
}
for stat in ("mean", "median", "p99"):
    for kind in ("ttft", "tpot", "itl"):
        _LOG_METRICS[f"{stat} {kind} (ms)"] = (f"llm.{kind}_ms", "ms", stat)


def vllm_log_records(raw: str) -> list[dict]:
    """Parse complete result blocks, not arbitrary prose. Subject is unknown.

    Output-only throughput is not single-user decode speed, and total includes
    input tokens. Keep mean/median/p99 separate. No model/hardware inference.
    """
    if len(raw) > MAX_INPUT:
        raise ValueError("benchmark input limit")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    out, block, seen, invalid = [], None, set(), False
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if _LOG_START.fullmatch(line):
            block, seen, invalid = [], set(), False
            continue
        if block is None:
            continue
        if _LOG_END.fullmatch(line):
            if not invalid:
                out.extend(block)
                if len(out) > MAX_RECORDS:
                    raise ValueError("benchmark candidate limit")
            block = None
            continue
        m = _LOG_LINE.fullmatch(line)
        if not m:
            continue
        label = m[1].strip().casefold()
        if label not in _LOG_METRICS:
            continue
        if label in seen:
            invalid = True
            continue
        seen.add(label)
        metric, unit, stat = _LOG_METRICS[label]
        try:
            block.append(_record(digest,None,metric,_number(m[2]),stat,unit,{},i,None,value_text=m[2]))
        except ValueError:
            invalid = True
    return out
