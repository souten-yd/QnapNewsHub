"""C4b: parse ControlDeck HTML table evidence, without inventing text offsets.

Caller must supply an authorized collector result. Hashes check consistency,
not authenticity. Returns candidates only; no writes, LLM, or network.
"""
from __future__ import annotations

import hashlib
import json
import re

from qnapnewshub.measure_quantity import decimal_value

SCHEMA = "controldeck.html-table.v1"
MAX_TABLES = 12
MAX_CELLS = 1200
MAX_RECORDS = 300
_CONDS = {"gpu": "hw", "hardware": "hw", "quant": "quant", "quantization": "quant",
          "engine": "engine", "backend": "backend", "context": "ctx", "concurrency": "concurrency"}
_METRICS = {"decode": "llm.decode_tps", "generation speed": "llm.decode_tps",
            "prefill": "llm.prefill_tps", "prompt processing": "llm.prefill_tps",
            "total": "llm.throughput_total_tps", "aggregate": "llm.throughput_total_tps",
            "total throughput": "llm.throughput_total_tps", "per user": "llm.throughput_per_user_tps",
            "per request": "llm.throughput_per_request_tps", "speed": "llm.tps_unspecified",
            "throughput": "llm.tps_unspecified", "": "llm.tps_unspecified"}
_UNIT = re.compile(r"\b(?:tokens/s|tok/s|tokens per second)\b", re.I)
_OTHER_UNIT = re.compile(r"\b(?:ms|GB|GiB|W|fps|tok/min|tokens/min|USD|JPY)\b|%", re.I)


def _int(v, lo, hi):
    if type(v) is not int or not lo <= v <= hi:
        raise ValueError("invalid grid integer")
    return v


def _hash(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def _grid(table):
    rows = _int(table["rows"], 1, 100)
    cols = _int(table["cols"], 1, 32)
    index = _int(table["table_index"], 0, MAX_TABLES-1)
    cells = table["cells"]
    if not isinstance(cells, list) or not 1 <= len(cells) <= 1000:
        raise ValueError("invalid cells")
    grid = {}
    for c in cells:
        if not isinstance(c, dict) or set(c) != {"row","col","rowspan","colspan","header","scope","text","truncated"}:
            raise ValueError("invalid cell")
        row, col = _int(c["row"], 0, rows-1), _int(c["col"], 0, cols-1)
        rs, cs = _int(c["rowspan"], 1, rows-row), _int(c["colspan"], 1, cols-col)
        if (type(c["header"]) is not bool or type(c["truncated"]) is not bool
                or c["scope"] not in ("","row","col","rowgroup","colgroup")
                or not isinstance(c["text"], str) or len(c["text"]) > 500):
            raise ValueError("invalid cell fields")
        for y in range(row, row+rs):
            for x in range(col, col+cs):
                if (y,x) in grid:
                    raise ValueError("overlapping cells")
                grid[y,x] = c
    if len(grid) != rows * cols:
        raise ValueError("incomplete table")
    return index, rows, cols, grid


def _coord(index, cell):
    return [index, cell["row"], cell["col"]]


def _headers(grid, rows, cols):
    # Only a leading all-th band. Row-header cells in the body are allowed.
    band = 0
    while band < rows and all(grid[band,c]["header"] for c in range(cols)):
        band += 1
    if band == 0 or band == rows:
        return None
    paths = []
    for col in range(cols):
        path = []
        for row in range(band):
            c = grid[row,col]
            if c["scope"] in ("row","rowgroup") or c["truncated"] or c["row"]+c["rowspan"] > band:
                return None
            if not path or path[-1] is not c:
                path.append(c)
        paths.append(path)
    return band, paths


def _metric(path):
    texts = [c["text"].strip() for c in path]
    if not any(_UNIT.search(t) for t in texts) or any(_OTHER_UNIT.search(t) for t in texts):
        return None
    # Parent labels may carry the unit, but leaf label must identify metric.
    leaf = _UNIT.sub("", texts[-1]).strip(" ()[]:").casefold()
    return _METRICS.get(leaf)


def parse_bundle(tables: list, meta: dict) -> list[dict]:
    if (not isinstance(meta, dict) or meta.get("schema") != SCHEMA
            or not _hash(meta.get("html_sha256")) or not _hash(meta.get("tables_sha256"))
            or not isinstance(tables, list) or len(tables) > MAX_TABLES):
        raise ValueError("invalid table evidence envelope")
    # Validate shape before JSON encoding; no arbitrary nested payloads.
    parsed, seen, total = [], set(), 0
    for t in tables:
        if (not isinstance(t, dict) or set(t) != {"table_index","caption","rows","cols","cells"}
                or not isinstance(t["caption"], str) or len(t["caption"]) > 500):
            raise ValueError("invalid table")
        index, rows, cols, grid = _grid(t)
        if index in seen:
            raise ValueError("duplicate table index")
        seen.add(index)
        total += len(t["cells"])
        if total > MAX_CELLS:
            raise ValueError("table cell limit")
        parsed.append((index, rows, cols, grid))
    raw = json.dumps(tables, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != meta["tables_sha256"]:
        raise ValueError("table digest mismatch")
    out = []
    for index, rows, cols, grid in parsed:
        headers = _headers(grid, rows, cols)
        if headers is None:
            continue
        band, paths = headers
        names = [p[-1]["text"].strip().casefold() for p in paths]
        models = [i for i,n in enumerate(names) if n == "model"]
        conditions = [(i,_CONDS[n]) for i,n in enumerate(names) if n in _CONDS]
        if len(models) != 1 or len({k for _,k in conditions}) != len(conditions):
            continue
        metrics = [(i, mid) for i,p in enumerate(paths) if (mid := _metric(p)) is not None]
        if len({mid for _,mid in metrics}) != len(metrics):
            continue
        for row in range(band, rows):
            subject = grid[row,models[0]]
            if subject["truncated"] or not subject["text"].strip() or subject["colspan"] != 1 or subject["row"] < band:
                continue
            conds = {}
            ambiguous = False
            for col, key in conditions:
                cell = grid[row,col]
                if cell["truncated"] or cell["colspan"] != 1 or cell["row"] < band:
                    ambiguous = True
                    break
                text = cell["text"].strip()
                if text and text.casefold() not in ("-","—","n/a","unknown","不明"):
                    # Do not claim multiple alternative settings as one condition.
                    if re.search(r"\b(?:or|either|and)\b|[／/、;；]|または|もしくは", text, re.I):
                        ambiguous = True
                        break
                    conds[key] = {"text":text,"cell":_coord(index,cell),
                                  "scope":"row" if cell["rowspan"] == 1 else "rowspan"}
            if ambiguous:
                continue
            for col, mid in metrics:
                cell = grid[row,col]
                if cell["header"] or cell["truncated"] or cell["rowspan"] != 1 or cell["colspan"] != 1:
                    continue
                try:
                    value = decimal_value(cell["text"])
                except ValueError:
                    continue
                if value < 0:
                    continue
                out.append({"subject":subject["text"].strip(),"metric_id":mid,"value":str(value),
                            "value_text":cell["text"],"unit":"tok/s",
                            "conditions":dict(conds),"evidence":{
                                "kind":"html_table","schema":SCHEMA,"html_sha256":meta["html_sha256"],
                                "tables_sha256":meta["tables_sha256"],"value_cell":_coord(index,cell),
                                "subject_cell":_coord(index,subject),
                                "headers":[_coord(index,h) for h in paths[col]]}})
                if len(out) > MAX_RECORDS:
                    raise ValueError("measurement candidate limit")
    return out
