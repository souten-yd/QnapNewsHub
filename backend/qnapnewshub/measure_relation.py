"""Conservative relation gate for prose. Presence is not evidence of association."""
from __future__ import annotations

import re

# Precision-first baseline, not a semantic parser. Do not resolve alternatives,
# contrast, hypothetical claims, negation or targets by picking an arbitrary span.
_UNSAFE = re.compile(
    r"\b(?:not|never|no longer|didn't|doesn't|cannot|can't|would|could|might|"
    r"expected|estimated|target|theoretical|hypothetical|planned|instead|versus|vs|"
    r"compared to|compared with|rather than|unlike|assuming|if|up to|at least|"
    r"or|either|maybe|possibly|but|however)\b|"
    r"未達|達しな|ではなく|ではない|できない|非対応|仮定|推定|予定|目標|理論値|比較|対し|"
    r"見込み|上限|下限|最大|最低|または|もしくは|あるいは|ただし", re.I)


def safe_clause(text: str) -> bool:
    """Only a bounded local assertion; uncertain language is left for later review."""
    return bool(text.strip()) and len(text) <= 2400 and not _UNSAFE.search(text)


def overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end
