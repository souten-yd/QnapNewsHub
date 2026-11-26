"""Strict decimal syntax shared by legacy and span-based measurement extraction."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d{1,3})?"
TOKEN = re.compile(r"(?<![A-Za-z0-9_.,+\-])" + NUMBER + r"(?!\d|[.,]\d|[eE][+-]?\d)")
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


def scalar_tokens(text: str, *, allow_qualified: bool = False):
    """Exclude both endpoints of ranges and explicitly qualified quantities.
    Returns matches on the normalized string (not raw-source character offsets).
    """
    s = normalized(text)
    # 「from X to Y」は範囲ではなく変化（前と後の 2 つの値）なので、どちらの数値も残す（2026-11-26 実データ:
    # 「cut one block from 2,397 ms to 725 ms」の 725 ms が消えていた）。
    def is_change(m) -> bool:
        return bool(re.search(r"\bfrom\s*$", s[max(0, m.start() - 12):m.start()], re.I))

    excluded = [m.span() for m in RANGE.finditer(s) if not is_change(m)]
    # A range may repeat its unit on both endpoints; neither is a scalar.
    repeated_unit = re.compile(r"(" + NUMBER + r")\s*(?:tok/s|tokens/s|tok/min|ms|s|GB|GiB|W|%|USD|円)"
                               r"\s*(?:[–—~〜～±-]|\bto\b)\s*(" + NUMBER + r")", re.I)
    excluded.extend(m.span() for m in repeated_unit.finditer(s) if not is_change(m))
    for m in TOKEN.finditer(s):
        if any(a < m.end() and m.start() < b for a, b in excluded):
            continue
        prefix = s[max(0, m.start()-32):m.start()]
        if allow_qualified:      # 「約」「最大」「up to」つきも値として残す（範囲・誤差つきは除く）
            yield m
            continue
        if re.search(r"(?:[<>≤≥≈~±]|about\b|around\b|approximately\b|up to\b|at least\b|約|最大|最低)\s*$", prefix, re.I):
            continue
        if re.match(r"\s*(?:tok/s|tokens/s|tok/min|ms|s|GB|GiB|W|%|USD|円)?\s*(?:±|以上|以下|未満|超|程度|前後|くらい)", s[m.end():], re.I):
            continue
        yield m


def is_scalar_occurrence(text: str, start: int, end: int) -> bool:
    """Map original offsets to normalized offsets; never use them to edit source."""
    if not 0 <= start < end <= len(text):
        return False
    a = len(normalized(text[:start]))
    b = a + len(normalized(text[start:end]))
    return any(m.span() == (a, b) for m in scalar_tokens(text))
