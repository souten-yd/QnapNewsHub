"""診断の数値（メモリのリングバッファ）。ARCHITECTURE 9.5。

- 5 分ごとに /proc/self/io・CPU 時間・RSS を 1 点とって、直近 24 時間（288 点）だけ覚える（ディスクへは書かない）。
- 1 日の要約だけを、日が変わったとき 1 回ファイルへ（Write Coordinator 経由。お休み中は休止が明けてから）。
- 取得の成否・まとめ書きの回数などの、その日のカウンタもここ（メモリ）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from qnapnewshub.config import data_dir

logger = logging.getLogger("qnapnewshub.metrics")

SAMPLE_SECONDS = 300
KEEP_POINTS = 288 + 4
DAILY_KEEP = 90
STARTED = time.time()
_lock = threading.Lock()
_points: deque = deque(maxlen=KEEP_POINTS)
_counters: dict = {}
_pending_daily: list[dict] = []
_day = ""


def _fresh_counters() -> dict:
    return {"fetch_ok": 0, "fetch_fail": 0, "flushes": 0, "flush_items": 0, "flush_write_bytes": 0, "flush_fail": 0, "llm_calls": 0}


def reset() -> None:
    global _day
    with _lock:
        _points.clear()
        _counters.clear()
        _counters.update(_fresh_counters())
        _pending_daily.clear()
        _day = ""


def _count(key: str, n: int = 1) -> None:
    with _lock:
        if not _counters:
            _counters.update(_fresh_counters())
        _counters[key] = _counters.get(key, 0) + n


def note_fetch(results: list) -> None:
    for _, _, info in results:
        if info.get("skipped"):
            continue
        _count("fetch_ok" if info["ok"] else "fetch_fail")


def note_flush(info: dict) -> None:
    if not info.get("items") and not info.get("updates") and not info.get("patches") and not info.get("fetchlog"):
        return
    _count("flushes")
    _count("flush_items", int(info.get("items", 0)))
    _count("flush_write_bytes", int(info.get("write_bytes", 0)))
    if not info.get("ok", True):
        _count("flush_fail")


def read_io() -> dict:
    out: dict = {}
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            key, _, value = line.partition(":")
            out[key.strip()] = int(value)
    except (OSError, ValueError):
        pass
    return out


def rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system


def loadavg() -> list[float]:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return []


def sample(now: float | None = None) -> dict:
    """1 点とる（読むだけ。ディスクへは書かない）。"""
    io = read_io()
    point = {"t": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"), "mono": time.monotonic(), "read_bytes": io.get("read_bytes", 0),
             "write_bytes": io.get("write_bytes", 0), "rchar": io.get("rchar", 0), "wchar": io.get("wchar", 0), "syscw": io.get("syscw", 0),
             "cpu": cpu_seconds(), "rss": rss_bytes()}
    with _lock:
        _points.append(point)
    _check_day()
    return point


def series() -> list[dict]:
    """直近 24 時間の推移（点どうしの差。書き込み・読み込みのバイト・CPU の %）。"""
    with _lock:
        pts = list(_points)
    out = []
    for a, b in zip(pts, pts[1:]):
        dt = max(0.001, b["mono"] - a["mono"])
        out.append({"t": b["t"], "write_bytes": max(0, b["write_bytes"] - a["write_bytes"]), "read_bytes": max(0, b["read_bytes"] - a["read_bytes"]),
                    "wchar": max(0, b["wchar"] - a["wchar"]), "cpu_pct": round(max(0.0, (b["cpu"] - a["cpu"]) / dt * 100), 2), "rss": b["rss"]})
    return out


def cpu_pct_recent() -> float:
    s = series()
    return s[-1]["cpu_pct"] if s else 0.0


def counters() -> dict:
    with _lock:
        return dict(_counters) if _counters else _fresh_counters()


# ---- 1 日の要約（1 日 1 回だけ保存） ----

def _daily_path() -> Path:
    return data_dir() / "news" / "diag-daily.json"


def _summary(day: str) -> dict:
    s = series()
    c = counters()
    from qnapnewshub import llm

    return {"day": day, "write_bytes": sum(p["write_bytes"] for p in s), "read_bytes": sum(p["read_bytes"] for p in s),
            "rss_max": max((p["rss"] for p in s), default=0), "cpu_avg_pct": round(sum(p["cpu_pct"] for p in s) / len(s), 2) if s else 0.0,
            **c, "llm_calls": llm.calls_today()}


def _check_day() -> None:
    """日が変わっていたら、前の日の要約を積む。保存は flush_daily（休止中でなければ）。"""
    global _day
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        if not _day:
            _day = today
            return
        if today == _day:
            return
        prev = _day
        _day = today
    summary = _summary(prev)
    with _lock:
        _counters.clear()
        _counters.update(_fresh_counters())
        _pending_daily.append(summary)


def flush_daily() -> bool:
    """積んだ前日の要約を 1 回で保存する（お休み中は保存しない。明けてから）。保存したら True。"""
    from qnapnewshub import rest, writer

    with _lock:
        if not _pending_daily or rest.active():
            return False
        rows = list(_pending_daily)
        _pending_daily.clear()
    path = _daily_path()
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        old = old if isinstance(old, list) else []
    except (OSError, ValueError):
        old = []
    merged = [r for r in old if r.get("day") not in {x["day"] for x in rows}] + rows
    writer.write_file(path, json.dumps(merged[-DAILY_KEEP:], ensure_ascii=False), name="diag.daily", user=False)
    return True


def daily_history() -> list[dict]:
    try:
        old = json.loads(_daily_path().read_text(encoding="utf-8"))
        return old[-30:] if isinstance(old, list) else []
    except (OSError, ValueError):
        return []


def machine() -> dict:
    from qnapnewshub import __version__
    import platform

    return {"cpu_pct": cpu_pct_recent(), "load": loadavg(), "rss": rss_bytes(), "uptime_seconds": int(time.time() - STARTED), "version": __version__,
            "python": platform.python_version(), "cpus": os.cpu_count() or 1}
