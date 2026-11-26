"""Bridge only explicit Markdown matrix cells to verified original spans.

No inferred model aliases, statistics, units, context or external writes.
"""
from __future__ import annotations

import re
from qnapnewshub.measure_benchmark_formats import benchmark_markdown_records, _MATRIX, _RATE
from qnapnewshub.measure_tables import _cells
from qnapnewshub.measure_spans import Span, Candidate, MeasurementDraft

_NGL = re.compile(r"-1|0|[1-9][0-9]{0,7}")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _sub(cell, start, end):
    return Span(cell.start+start, cell.start+end, cell.text[start:end])


def verified_matrix_drafts(source: str) -> list[MeasurementDraft]:
    if len(source.encode("utf-8")) > 128*1024:
        raise ValueError("matrix source limit")
    rows, fenced = [], set()
    offset, fence = 0, None
    for i,line in enumerate(source.splitlines(keepends=True),1):
        text = line.rstrip("\r\n")
        if fence is not None:
            fenced.add(i)
            if re.fullmatch(r" {0,3}"+re.escape(fence[0])+"{"+str(len(fence))+r",}[ \t]*",text):
                fence = None
        elif (m := _FENCE.match(text)):
            fence = m[1]
            fenced.add(i)
        rows.append(_cells(text,offset))
        offset += len(line)
    out = []
    for record in benchmark_markdown_records(source):
        # Existing schema cannot represent stddev or depth losslessly.
        if record["stat"] != "unknown":
            continue
        ev = record["evidence"]
        h, r, c = record["header_line"],ev["line"],ev["column"]
        if h in fenced or r in fenced:
            continue
        header,row = rows[h-1],rows[r-1]
        if len(header)!=len(row) or not 0<=c<len(row):
            continue
        names = [s.text.casefold() for s in header]
        if "model" not in names:
            continue
        subject = row[names.index("model")]
        match = _MATRIX.fullmatch(header[c].text)
        cell = row[c]
        shift = 2 if cell.text.startswith("**") and cell.text.endswith("**") else 0
        raw = cell.text[shift:len(cell.text)-shift] if shift else cell.text
        rate = _RATE.fullmatch(raw)
        if not match or not rate or subject.text != record["subject"] or cell.text != record["value_text"]:
            continue
        value = _sub(cell,shift+rate.start(1),shift+rate.end(1))
        # Unit is a literal source substring, not a fabricated normalized span.
        unit_start = rate.end(1)
        while unit_start < len(raw) and raw[unit_start].isspace():
            unit_start += 1
        unit = _sub(cell,shift+unit_start,shift+len(raw))
        conds = [
            Candidate("backend",_sub(header[c],*match.span(1))),
            Candidate("prompt_tokens" if match[2].casefold()=="pp" else "gen_tokens",
                      _sub(header[c],*match.span(3))),
        ]
        if "ngl" in names:
            ngl = row[names.index("ngl")]
            if not _NGL.fullmatch(ngl.text):
                continue
            conds.append(Candidate("offload",ngl))
        draft = MeasurementDraft(subject,value,unit,record["metric_id"],
                                 f"matrix:{header[0].start}:row:{row[0].start}",tuple(conds))
        if not all(s.valid(source) for s in [subject,value,unit]+[c.span for c in conds]):
            raise ValueError("invalid matrix evidence")
        out.append(draft)
    return out
