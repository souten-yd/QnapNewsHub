"""Strict decimal syntax shared by legacy and span-based measurement extraction."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d{1,3})?"
TOKEN = re.compile(r"(?<![A-Za-z0-9_.+\-])" + NUMBER + r"(?!\d|[.,]\d|[eE][+-]?\d)")
RANGE = re.compile(r"(" + NUMBER + r")\s*(?:[–—−~〜～-]|\bto\b)\s*(" + NUMBER + r")", re.I)
QUALIFIED = re.compile(r"^\s*(?:[<>≤≥≈~±]|about\b|around\b|approximately\b|up to\b|at least\b|約|最大|最低)", re.I)


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).replace("−", "-")


def decimal_value(text: str) -> Decimal:
    s = normalized(text).strip()
    if not re.fullmatch(NUMBER, s):
        raise ValueError("not a scalar")
    try:
        value = Decimal(s.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("invalid number") from exc
    if not value.is_finite() or (value and abs(value.adjusted()) > 300):
        raise ValueError("number out of bounds")
    return value


def interval_value(text: str) -> tuple[Decimal, Decimal] | None:
    m = RANGE.fullmatch(normalized(text).strip())
    if not m:
        return None
    low, high = (decimal_value(x) for x in m.groups())
    if low > high:
        raise ValueError("reversed interval")
    return low, high


def scalar_tokens(text: str):
    """Exclude both endpoints of ranges and explicitly qualified quantities.
    Returns matches on the normalized string (not raw-source character offsets).
    """
    s = normalized(text)
    excluded = [m.span() for m in RANGE.finditer(s)]
    for m in TOKEN.finditer(s):
        if any(a < m.end() and m.start() < b for a, b in excluded):
            continue
        prefix = s[max(0, m.start()-32):m.start()]
        if re.search(r"(?:[<>≤≥≈~±]|about\b|around\b|approximately\b|up to\b|at least\b|約|最大|最低)\s*$", prefix, re.I):
            continue
        if re.match(r"\s*±", s[m.end():]):
            continue
        yield m
