"""Read-only candidate contract v1. Source-relative evidence, never persistence authority."""
from __future__ import annotations
import re
from qnapnewshub.measure_benchmark_formats import benchmark_markdown_records, _cells

VERSION = 'benchmark-candidate-v1'
_ALIASES = {'n_prompt':'prompt_tokens', 'n_gen':'gen_tokens', 'n_batch':'batch',
            'threads':'cpu_threads', 'ngl':'offload'}
_FOOTNOTE = re.compile(r'([¹²³⁴⁵⁶⁷⁸⁹⁰]+)$')


def candidates(raw: str) -> list[dict]:
    """Re-derive from exact supplied Markdown, never accept externally forged records.

    Footnote aliases are document-local retrieval hints, not entity identity.
    Source-relative line/column evidence is not an authoritative character span.
    Unknown conditions stay raw; n_depth is not context length.
    """
    records = benchmark_markdown_records(raw)
    lines = raw.splitlines()
    labels = {r['subject'] for r in records}
    out = []
    for r in records:
        ev = r['evidence']
        row, header = _cells(lines[ev['line']-1]), _cells(lines[r['header_line']-1])
        if row[ev['column']] != r['value_text']:
            raise ValueError('candidate evidence mismatch')
        conditions = {}
        for key, val in r['conditions_raw'].items():
            if key == 'backend' and isinstance(val,str) and val.strip():
                conditions[key] = val.casefold()
            elif key in _ALIASES and re.fullmatch(r'-?\d{1,8}',str(val)):
                n = int(val)
                if n > 0 or (key == 'ngl' and n >= -1):
                    conditions[_ALIASES[key]] = str(n)
        subject = r['subject']
        aliases = []
        m = _FOOTNOTE.search(subject)
        if m and subject[:m.start()] not in labels:
            stem = subject[:m.start()].strip()
            notes = [i+1 for i,line in enumerate(lines) if line.startswith(m[1]+' ')]
            # No alias if ambiguous same-document labels share the stem.
            if stem and len(notes)==1 and sum(_FOOTNOTE.sub('',v).strip()==stem for v in labels)==1:
                aliases.append({'text':stem,'note_line':notes[0]})
        out.append({**r, 'schema':VERSION, 'conditions':conditions, 'subject_aliases':aliases,
                    'evidence':{**ev,'kind':'markdown_cell','header_line':r['header_line'],
                                'header_text':header[ev['column']], 'cell_text':row[ev['column']]},
                    'status':'candidate_only'})
    return out
