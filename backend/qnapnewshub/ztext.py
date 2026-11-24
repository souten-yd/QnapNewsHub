"""文字列の Zstandard 圧縮と展開（本文・古い記事の抜粋）。細かいファイルを作らず、DB の BLOB に持つ。

展開は大きさの上限つき（壊れた・悪意のあるデータで膨らませない）。"""
from __future__ import annotations

import zstandard

LEVEL = 9
MAX_RAW_BYTES = 4 * 1024 * 1024


def pack(text: str) -> bytes:
    return zstandard.ZstdCompressor(level=LEVEL).compress(text.encode("utf-8"))


def unpack(blob: bytes | None) -> str:
    """展開して文字列に。壊れていれば ValueError。"""
    if not blob:
        return ""
    try:
        return zstandard.ZstdDecompressor().decompress(bytes(blob), max_output_size=MAX_RAW_BYTES).decode("utf-8")
    except (zstandard.ZstdError, UnicodeDecodeError) as exc:
        raise ValueError("圧縮したデータを展開できません") from exc
