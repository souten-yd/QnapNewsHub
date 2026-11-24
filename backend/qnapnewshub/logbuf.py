"""ログはメモリのリングバッファ（ファイルへは既定で書かない）。設定 storage.log_file が入のときだけ、置き場の logs/ へ回す（再起動で効く）。

- 診断の束へは、秘密を伏せた直近の行だけ出す（鍵らしい文字列・URL の問い合わせ部分を伏せる）。
- アクセスログは既定で切（cli が uvicorn へ access_log=False）。
"""
from __future__ import annotations

import logging
import re
from collections import deque
from logging.handlers import RotatingFileHandler

from qnapnewshub.config import data_dir

RING_LINES = 1000
_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{6,}|nh_[A-Za-z0-9_-]{8,}|(?i:bearer)\s+\S+|(?i:token|key|password|secret)=\S+)")
_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")


def redact(text: str) -> str:
    return _QUERY.sub(r"\1?…", _SECRET.sub("[伏せ字]", str(text)))


class RingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: deque[dict] = deque(maxlen=RING_LINES)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append({"t": record.created, "level": record.levelname, "name": record.name, "msg": redact(record.getMessage())[:400]})
        except Exception:  # noqa: BLE001 - ログで落ちない
            pass


_ring = RingHandler()
_file: RotatingFileHandler | None = None


def install(file_log: bool = False) -> RingHandler:
    """リングバッファを qnapnewshub のログへ付ける（何度呼んでもよい）。file_log なら置き場の logs/hub.log（2MB × 2 世代）にも。"""
    global _file
    root = logging.getLogger("qnapnewshub")
    if _ring not in root.handlers:
        root.addHandler(_ring)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    if file_log and _file is None:
        folder = data_dir() / "logs"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        _file = RotatingFileHandler(folder / "hub.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _file.addFilter(lambda r: True)
        root.addHandler(_file)
    return _ring


def recent(limit: int = 200) -> list[dict]:
    return list(_ring.lines)[-limit:]
