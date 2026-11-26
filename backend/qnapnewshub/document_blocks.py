"""C6e: read-only structural change candidates, never a measurement reuse decision."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import re

MAX_BYTES = 128 * 1024
MAX_BLOCKS = 2048
VERSION = "markdown-block-v1"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+")


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    kind: str
    digest: str
    headings: tuple[str, ...]


@dataclass(frozen=True)
class Match:
    old: int
    new: int
    context_changed: bool


@dataclass(frozen=True)
class Delta:
    old_hash: str
    new_hash: str
    matches: tuple[Match, ...]
    removed_or_ambiguous: tuple[int, ...]
    added_or_ambiguous: tuple[int, ...]
    # This is a diagnostic only; shared prose/footnotes/entities can affect any block.
    requires_full_extraction: bool


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def blocks(source: str) -> tuple[Block, ...]:
    if len(source.encode("utf-8")) > MAX_BYTES:
        raise ValueError("document size limit")
    out: list[Block] = []
    headings: list[tuple[int, str]] = []
    start = None
    offset = 0
    fence = None

    def emit(a: int, b: int, kind: str):
        out.append(Block(a, b, kind, _hash(source[a:b]), tuple(h for _, h in headings)))
        if len(out) > MAX_BLOCKS:
            raise ValueError("block count limit")

    def flush(end: int):
        nonlocal start
        if start is not None:
            emit(start, end, "text")
            start = None

    for line in source.splitlines(keepends=True):
        end = offset + len(line)
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", line.rstrip("\r\n")):
                emit(start, end, "fence")
                start = fence = None
        elif (m := _FENCE.match(line)):
            flush(offset)
            start, fence = offset, m[1]
        elif (m := _HEADING.match(line)):
            flush(offset)
            level = len(m[1])
            headings = [(n, h) for n, h in headings if n < level]
            headings.append((level, _hash(line)))
            emit(offset, end, "heading")
        elif not line.strip():
            flush(offset)
        elif start is None:
            start = offset
        offset = end
    if fence is not None:
        emit(start, offset, "unclosed_fence")
    else:
        flush(offset)
    return tuple(out)


def compare(old: str, new: str) -> Delta:
    before, after = blocks(old), blocks(new)
    old_keys = [(b.kind, b.digest) for b in before]
    new_keys = [(b.kind, b.digest) for b in after]
    counts_a, counts_b = Counter(old_keys), Counter(new_keys)
    unique = {key: i for i, key in enumerate(old_keys) if counts_a[key] == 1}
    matches = []
    def context(seq, keys, i):
        return (seq[i].headings, keys[i-1] if i else None,
                keys[i+1] if i+1 < len(keys) else None)
    for j, key in enumerate(new_keys):
        if counts_b[key] != 1 or key not in unique:
            continue
        i = unique[key]
        # Compare original text too: even a hash match is not authority.
        if old[before[i].start:before[i].end] != new[after[j].start:after[j].end]:
            continue
        matches.append(Match(i, j, context(before, old_keys, i) != context(after, new_keys, j)))
    old_matched = {m.old for m in matches}
    new_matched = {m.new for m in matches}
    return Delta(_hash(old), _hash(new), tuple(matches),
                 tuple(i for i in range(len(before)) if i not in old_matched),
                 tuple(j for j in range(len(after)) if j not in new_matched), old != new)
