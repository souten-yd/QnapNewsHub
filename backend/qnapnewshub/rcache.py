"""検索の道具（/retrieve/*・/graph/*・/research）の共通の土台: メモリのキャッシュ（版つき）・時間の上限・応答の大きさの上限。v0.6。

- **キャッシュ**: 同じ要求（道具の名前・区分の範囲・本文の JSON）の応答を、JSON の bytes のままメモリに持つ（LRU・全体で 32MB・1 件 1MB まで・5 分）。
  **版**は SQLite の `PRAGMA data_version`（別の接続がコミットすると値が変わる）。キャッシュ専用の接続を 1 本だけ持ち、書き込みは 1 度もしない。
  Write Coordinator のまとめ書き・仕事の取り込み・画面の操作など、**どの経路で DB が変わっても**版が変わり、古い応答は返らない（版の違うキーは使われず、次の追加で捨てる）。
  まだ保存していない（バッファの）変更は DB にまだ無いので、応答にも入らない（DB の中身と同じ）。
  区分の範囲（scopes）はキーに入る＝別の区分の応答を取り違えない。認証はキャッシュの前（依存）で済んでいる。
- **時間の上限**: `Budget`（時計の期限）。段の間・ループの中で `expired()` を見て、いまの結果で止める（応答に truncated と理由）。
  それとは別に、SQLite が 1 つの SQL に長くかかったら止める（progress handler。期限の 3 倍で 503）＝重い問い合わせが居座らない。
- **応答の大きさ**: 道具ごとの件数の上限（各 API の定数）と、応答の JSON が MAX_RESPONSE_BYTES（1MB）を超えたら 413 にせず、件数を減らして truncated を立てる（`fit`）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager

from fastapi import HTTPException
from fastapi.responses import Response

from qnapnewshub import db as news_db

MAX_RESPONSE_BYTES = 1_000_000
CACHE_TOTAL_BYTES = 32_000_000
CACHE_TTL = 300.0
BUDGET_DEFAULT_MS, BUDGET_MAX_MS = 400, 3000
HARD_FACTOR = 3.0

_lock = threading.Lock()
_probe: dict = {"path": "", "conn": None}
_store: OrderedDict[tuple, tuple[float, bytes]] = OrderedDict()
_bytes = [0]
stats = {"hit": 0, "miss": 0, "evicted": 0, "uncacheable": 0}


# ---- 版 ----

def data_version() -> int:
    """DB の版（別の接続のコミットで変わる整数）。キャッシュ専用の接続で読む（その接続は書かない）。DB のファイルが変わったら（試験・作り直し）開き直す。"""
    path = str(news_db.path())
    with _lock:
        if _probe["path"] != path or _probe["conn"] is None:
            if _probe["conn"] is not None:
                try:
                    _probe["conn"].close()
                except sqlite3.Error:
                    pass
            news_db.engine()      # ファイルと表を用意してから開く
            _probe["conn"] = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
            _probe["path"] = path
            _store.clear()
            _bytes[0] = 0
        return int(_probe["conn"].execute("PRAGMA data_version").fetchone()[0])


def reset() -> None:
    """試験用: キャッシュと版の接続を捨てる。"""
    with _lock:
        if _probe["conn"] is not None:
            try:
                _probe["conn"].close()
            except sqlite3.Error:
                pass
        _probe.update(path="", conn=None)
        _store.clear()
        _bytes[0] = 0
        for k in stats:
            stats[k] = 0


def snapshot() -> dict:
    with _lock:
        return {**stats, "entries": len(_store), "bytes": _bytes[0]}


# ---- 時間 ----

class Budget:
    """時計の期限。ms ミリ秒後。`expired()` を見て止める。`hit` は期限で止めた印（応答の truncated の理由）。"""

    def __init__(self, ms: int) -> None:
        self.ms = max(10, min(int(ms), BUDGET_MAX_MS))
        self.start = time.monotonic()
        self.deadline = self.start + self.ms / 1000
        self.hit = False

    def expired(self) -> bool:
        if time.monotonic() > self.deadline:
            self.hit = True
        return self.hit

    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self.start) * 1000, 1)


@contextmanager
def hard_limit(db, budget: Budget):
    """SQLite の 1 つの SQL が期限の HARD_FACTOR 倍を超えたら止める（OperationalError の interrupted を 503 にする）。接続は元に戻す。"""
    raw = db.connection().connection.driver_connection
    limit = budget.start + budget.ms / 1000 * HARD_FACTOR
    raw.set_progress_handler(lambda: 1 if time.monotonic() > limit else 0, 20000)
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        if "interrupt" in str(exc).lower():
            raise HTTPException(status_code=503, detail=f"時間の上限（{budget.ms}ms の {HARD_FACTOR:g} 倍）を超えたので止めました。条件を絞ってください") from None
        raise
    finally:
        raw.set_progress_handler(None, 0)


# ---- 応答の大きさ ----

def fit(payload: dict, lists: list[tuple[dict, str]], max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
    """payload の JSON が max_bytes を超えるなら、lists（(辞書, キー)＝長い配列の置き場）の最も長いものから半分ずつ減らして収める。減らしたら payload["truncated"]・
    payload["size_limited"] を立てる。配列の先頭ほど良い順＝減らすのは後ろ。"""
    guard = 0
    while guard < 40:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size <= max_bytes:
            return payload
        holder, key = max(lists, key=lambda hk: len(hk[0].get(hk[1], [])))
        n = len(holder.get(key, []))
        if n <= 1:
            break
        holder[key] = holder[key][: max(1, n // 2)]
        payload["truncated"] = True
        payload["size_limited"] = True
        guard += 1
    return payload


# ---- キャッシュつきの実行 ----

def cached(tool: str, scopes: list[str], body: dict, compute: Callable[[], dict]) -> Response:
    """compute() の結果（JSON にできる辞書）を、(道具・区分・本文・DB の版) をキーにキャッシュして JSON で返す。
    応答のヘッダ: X-Cache（hit・miss）・X-Data-Version。本文の meta は計算したときのもの（elapsed_ms は計算にかかった時間）。"""
    version = data_version()
    key = (str(news_db.path()), tool, tuple(scopes), json.dumps(body, sort_keys=True, ensure_ascii=False, default=str), version)
    now = time.monotonic()
    with _lock:
        found = _store.get(key)
        if found is not None and now - found[0] < CACHE_TTL:
            _store.move_to_end(key)
            stats["hit"] += 1
            return Response(found[1], media_type="application/json", headers={"X-Cache": "hit", "X-Data-Version": str(version)})
    payload = compute()
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    with _lock:
        stats["miss"] += 1
        # 計算のあいだに DB が変わっていたら（版が違う）入れない＝古い応答を新しい版の名前で持たない
        cacheable = len(data) <= MAX_RESPONSE_BYTES and not payload.get("no_cache")
    if cacheable and data_version() == version:
        with _lock:
            old = _store.pop(key, None)
            if old is not None:
                _bytes[0] -= len(old[1])
            for k in [k for k in _store if k[-1] != version]:      # 版の古いものは使われない
                _bytes[0] -= len(_store.pop(k)[1])
                stats["evicted"] += 1
            _store[key] = (now, data)
            _bytes[0] += len(data)
            while _bytes[0] > CACHE_TOTAL_BYTES and _store:
                _, (_, dropped) = _store.popitem(last=False)
                _bytes[0] -= len(dropped)
                stats["evicted"] += 1
    else:
        stats["uncacheable"] += 1
    return Response(data, media_type="application/json", headers={"X-Cache": "miss", "X-Data-Version": str(version)})
