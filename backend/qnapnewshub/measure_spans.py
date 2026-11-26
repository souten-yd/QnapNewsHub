"""C3: 原文の位置を失わない軽量抽出。曖昧な文を無理に確定しない。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from qnapnewshub import bench, measure_quantity as mq
from qnapnewshub.measure_vocab import normalize_condition

@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str

    def valid(self, source: str) -> bool:
        return 0 <= self.start < self.end <= len(source) and source[self.start:self.end] == self.text


@dataclass(frozen=True)
class Candidate:
    key: str
    span: Span


@dataclass(frozen=True)
class MeasurementDraft:
    subject: Span
    value: Span
    unit: Span
    metric_id: str
    block_id: str
    conditions: tuple[Candidate, ...] = ()


_NUM_UNIT = re.compile(r"(?<![A-Za-z0-9_.+-])(?P<value>" + mq.NUMBER + r")[ \t]*(?P<unit>tokens/s|tok/s|tok/min|ms|s|GiB|GB|MiB|MB|kW|mW|W|MHz|GHz|%|USD|JPY|円)(?![A-Za-z0-9_/])")
_BOUNDARY = re.compile(r"\n+|(?<=[。！？!?])\s*|(?<=\.)\s+(?=[A-Z])")


def blocks(source: str):
    """元文字列の区間。Markdown の表行も独立させる。"""
    start = 0
    for split in _BOUNDARY.finditer(source):
        if split.start() > start:
            yield start, split.start()
        start = split.end()
    if start < len(source):
        yield start, len(source)


def condition_candidates(source: str, start: int, end: int) -> tuple[Candidate, ...]:
    text = source[start:end]
    out = []
    for key, pattern in (("quant", bench._QUANT), ("hw", bench._HW), ("engine", bench._ENGINE),
                         ("ctx", bench._CTX), ("version", bench._VERSION), ("backend", bench._BACKEND)):
        for m in pattern.finditer(text):
            out.append(Candidate(key, Span(start+m.start(), start+m.end(), m.group())))
    return tuple(out)


def unambiguous(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    by_key = {}
    for c in candidates:
        by_key.setdefault(c.key, {}).setdefault(normalize_condition(c.key, c.span.text), c)
    return tuple(next(iter(group.values())) for group in by_key.values() if len(group) == 1)


def extract_explicit(source: str, subject: str) -> list[MeasurementDraft]:
    """対象名と数値が同じ文/行に一意に出る場合だけ候補を生成。
    複数の数値を含むブロック・列見出し・共有条件は後続の処理に委ねる。
    """
    if not subject:
        return []
    pattern = re.compile(r"(?<![A-Za-z0-9_-])"+re.escape(subject)+r"(?![A-Za-z0-9_-])", re.I)
    out = []
    for start, end in blocks(source):
        text = source[start:end]
        names, nums = list(pattern.finditer(text)), list(_NUM_UNIT.finditer(text))
        if len(names) != 1 or len(nums) != 1 or text.lstrip().startswith("|"):
            continue
        name, num = names[0], nums[0]
        # 原文のオフセットを壊さず、正規化後の数値字句の検証結果も使う。
        if mq.RANGE.search(mq.normalized(text)) or "±" in text:
            continue
        prefix = text[:num.start("value")].rstrip()
        if re.search(r"(?:[<>≤≥≈~]|\babout|\baround|\bapproximately|\bup to|\bat least|約|最大|最低)$", prefix, re.I):
            continue
        try:
            mq.decimal_value(num.group("value"))
        except ValueError:
            continue
        def span(m, group=0):
            return Span(start+m.start(group), start+m.end(group), m.group(group))
        unit = span(num, "unit")
        canonical_unit = "tok/s" if unit.text in ("tokens/s", "tok/min") else unit.text
        mid = bench.metric_id("benchmark", text, canonical_unit)
        if mid not in ("llm.decode_tps", "llm.prefill_tps", "llm.throughput_total_tps", "llm.throughput_per_user_tps", "llm.throughput_per_request_tps", "llm.tps_unspecified"):
            continue
        out.append(MeasurementDraft(span(name), span(num, "value"), unit, mid, f"block:{start}",
                                    unambiguous(condition_candidates(source, start, end))))
    return out

