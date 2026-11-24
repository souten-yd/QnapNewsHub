"""お休みモード（設定 settings.json の "rest"）の判定。ARCHITECTURE 9 章。

- 開始・終了の時刻（HH:MM。日をまたげる）の間が「休止中」。既定は切。
- 休止中の動き（mode）:
  - keep_memory   集め続けてメモリに保持。DB・checkpoint は書かない（メモリの安全の上限に達したら集めるのを止める）。
  - stop_all      集める・分析・保存をすべて休む。
  - flush_at_limit 集め続けて、バッファの上限（件数・容量）に達したときだけ一括で保存する。
  どの動きでも、下ごしらえ・画像・まとめ・集計・checkpoint・バックアップは休む。
- 利用者の操作（設定の保存・好み・ログイン）は休止中でも書く（そうしないと休止を解けない）。ただし書いた処理名を記録して診断に出す。
- 設定の読み込みは短く覚える（休止の判定は何度も呼ばれる）。settings.save が invalidate() を呼ぶ。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

MODES = ("keep_memory", "stop_all", "flush_at_limit")
MODE_LABELS = {"keep_memory": "集め続けてメモリに保持", "stop_all": "集める・分析・保存をすべて休む", "flush_at_limit": "集め続けて上限のときだけ一括で保存"}
DEFAULT = {"enabled": False, "start": "01:00", "end": "06:00", "mode": "keep_memory"}
_TTL = 2.0
_lock = threading.Lock()
_cache: dict = {"key": None, "at": 0.0, "cfg": None}


def now() -> datetime:
    """いまの地域の時刻（試験が差し替える）。"""
    return datetime.now()


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0


def config() -> dict:
    """設定の "rest" の節（短く覚える）。読めなければ既定（休止しない）。"""
    from qnapnewshub import settings
    from qnapnewshub.config import data_dir

    key = str(data_dir())
    with _lock:
        if _cache["key"] == key and time.monotonic() - _cache["at"] < _TTL and _cache["cfg"] is not None:
            return _cache["cfg"]
    try:
        cfg = settings.load().get("rest") or dict(DEFAULT)
    except Exception:  # noqa: BLE001 - 設定が読めなくても休止の判定で落とさない
        cfg = dict(DEFAULT)
    with _lock:
        _cache.update(key=key, at=time.monotonic(), cfg=cfg)
    return cfg


def _minutes(clock: str) -> int:
    return int(clock[:2]) * 60 + int(clock[3:])


def in_window(cfg: dict, at: datetime) -> bool:
    """開始は含み、終わりは含まない。開始 > 終了は日をまたぐ（22:00〜06:00）。開始 = 終了は休止しない。"""
    if not cfg.get("enabled"):
        return False
    start, end, cur = _minutes(cfg["start"]), _minutes(cfg["end"]), at.hour * 60 + at.minute
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def next_end(cfg: dict, at: datetime) -> datetime | None:
    """休止中なら、次の終了の時刻（このあと最初に来る end）。休止中でなければ None。"""
    if not in_window(cfg, at):
        return None
    end = _minutes(cfg["end"])
    day = at.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    return day if day > at else day + timedelta(days=1)


def state(at: datetime | None = None) -> dict:
    at = at or now()
    cfg = config()
    active = in_window(cfg, at)
    ends = next_end(cfg, at)
    return {"enabled": bool(cfg.get("enabled")), "active": active, "mode": cfg.get("mode", "keep_memory"), "start": cfg.get("start"), "end": cfg.get("end"),
            "ends_at": ends.strftime("%Y-%m-%dT%H:%M:%S") if ends else None}


def active() -> bool:
    return in_window(config(), now())


def mode() -> str:
    return str(config().get("mode", "keep_memory"))


def blocks_fetch() -> bool:
    """休止中の「すべて休む」では、集めない。"""
    return active() and mode() == "stop_all"


def blocks_analysis() -> bool:
    """休止中は、下ごしらえ・一言要約・まとめ・画像・集計を休む（どの動きでも）。"""
    return active()
