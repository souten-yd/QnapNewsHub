"""無料モデルの順番と切り替え（ARCHITECTURE 5.1）。メモリだけで持つ（書き込みを増やさない）。1 日の要約だけ、日が変わったとき 1 回保存する。

- 並び（chain）= 設定の [下ごしらえのモデル, *次のモデル]。1 番目が通常。
- モデルごとに直近 WINDOW（5）回の結果を持ち、そのうち FAIL_LIMIT（3）回が失敗（429・空の答え・検査落ち・通信の失敗）なら、いま使っているモデルを次へ移す。
  次のモデルの履歴は空から始める。RETURN_SECONDS（30 分）たったら 1 番目へ戻す（履歴も空にして、また試す）。
- 失敗の記録は 2 か所: llm.py（HTTP の失敗・空の答え）と prep.py（1 回の呼び出しの結果が検査に落ちた／通った）。成功は prep.py だけが記録する（二重に数えない）。
- 有料のモデル（名前が :free で終わらない）も選べる。画面で注意を出す。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime

from qnapnewshub.config import data_dir

logger = logging.getLogger("qnapnewshub.llm_models")

WINDOW = 5
FAIL_LIMIT = 3
RETURN_SECONDS = 30 * 60
MAX_CHAIN = 6
KEEP_DAYS = 30

_lock = threading.Lock()
_clock = time.monotonic        # 試験が差し替える
_state: dict = {"index": 0, "since": None, "chain": []}
_windows: dict[str, deque] = {}
events: deque = deque(maxlen=30)       # 切り替え・戻りの記録（診断）


def is_free(model: str) -> bool:
    return model.endswith(":free") or model.startswith("gateway:")      # ゲートウェイ（ControlDeck のローカル LLM）は OpenRouter の残高を使わない


def build_chain(cfg: dict) -> list[str]:
    """設定の llm から、使う順の行き先＋モデル。OpenRouter はモデル名のまま、ゲートウェイは "gateway:<名前>"。重複は 1 回だけ。
    ゲートウェイは、有効で URL があり、つながらないと分かった冷却の間でなければ入る（gateway.first なら先頭、でなければ最後）。"""
    out: list[str] = []
    for name in [cfg["prep_model"], *cfg.get("fallback_models", [])]:
        if name and name not in out:
            out.append(name)
    gw = cfg.get("gateway") or {}
    if gw.get("enabled") and gw.get("base_url") and not gateway_is_down():
        name = f"gateway:{gw.get('model') or 'auto'}"
        out = [name, *out] if gw.get("first", True) else [*out, name]
    return out[:MAX_CHAIN]


_gw: dict = {"down_until": None, "cooldown": 10 * 60}


def gateway_is_down() -> bool:
    with _lock:
        until = _gw["down_until"]
        if until is not None and _clock() >= until:
            _gw["down_until"] = None
            return False
        return until is not None


def gateway_down(reason: str = "") -> None:
    """ゲートウェイがつながらないと分かった。しばらく（既定 10 分）順番から外す。"""
    try:
        from qnapnewshub import settings

        minutes = settings.load()["llm"]["gateway"]["cooldown_minutes"]
    except Exception:  # noqa: BLE001
        minutes = 10
    with _lock:
        _gw["down_until"] = _clock() + minutes * 60
        _event("down", "gateway", f"{minutes} 分は使いません（{reason}）")


def reset() -> None:
    with _lock:
        _state.update(index=0, since=None, chain=[])
        _windows.clear()
        events.clear()
        _gw["down_until"] = None


def _event(kind: str, model: str, reason: str = "") -> None:
    events.append({"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "model": model, "reason": reason})
    logger.info("モデルの%s: %s %s", {"switch": "切り替え", "return": "戻し", "down": "ゲートウェイの除外"}.get(kind, kind), model, reason)


def current(chain: list[str]) -> str:
    """いま使うモデル。切り替えて 30 分たっていれば 1 番目へ戻す。並びが変わっていれば先頭から。"""
    with _lock:
        if chain != _state["chain"]:
            _state.update(chain=list(chain), index=0, since=None)
            _windows.clear()
        if _state["index"] > 0 and _state["since"] is not None and _clock() - _state["since"] >= RETURN_SECONDS:
            _state.update(index=0, since=None)
            _windows.clear()
            _event("return", chain[0], "30 分たったので 1 番目へ戻します")
        return chain[_state["index"]]


def switched_away(model: str) -> bool:
    """model はもう現在のモデルではないか（待っている間に切り替わった）。"""
    with _lock:
        chain = _state["chain"]
        return bool(chain) and chain[_state["index"]] != model


def record(model: str, ok: bool, reason: str = "") -> None:
    """1 回の結果（成功・失敗）。現在のモデルで直近 5 回に 3 回失敗が出たら次へ移す。"""
    with _lock:
        win = _windows.setdefault(model, deque(maxlen=WINDOW))
        win.append(bool(ok))
        chain = _state["chain"]
        if ok or not chain or chain[_state["index"]] != model:
            return
        if sum(1 for x in win if not x) >= FAIL_LIMIT and _state["index"] + 1 < len(chain):
            _state["index"] += 1
            _state["since"] = _clock()
            _windows.pop(chain[_state["index"]], None)
            _event("switch", chain[_state["index"]], f"{model} が直近 {len(win)} 回で {sum(1 for x in win if not x)} 回失敗（{reason or '失敗'}）")


def status() -> dict:
    with _lock:
        chain = list(_state["chain"])
        idx = _state["index"]
        left = None
        if idx > 0 and _state["since"] is not None:
            left = max(0, int(RETURN_SECONDS - (_clock() - _state["since"])))
        gw_left = None if _gw["down_until"] is None else max(0, int(_gw["down_until"] - _clock()))
        return {"gateway_down_in_seconds": gw_left, "chain": chain, "current": chain[idx] if chain else "", "index": idx, "return_in_seconds": left,
                "windows": {m: list(w) for m, w in _windows.items()}, "events": list(events)[-10:], "paid": [m for m in chain if not is_free(m)]}


# ---- 1 日の要約（日が変わったとき 1 回だけ保存） ----

def daily_path():
    return data_dir() / "news" / "llm-models-daily.json"


def load_daily() -> dict:
    try:
        raw = json.loads(daily_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_daily(day: str, models: dict[str, dict]) -> None:
    """前の日のモデルごとの要約を、最新 KEEP_DAYS 日ぶんだけ残して保存する（まとめ書きに載せる）。"""
    if not models:
        return
    from qnapnewshub import writer

    path = daily_path()
    merged = load_daily()
    merged[day] = models
    merged = {d: merged[d] for d in sorted(merged)[-KEEP_DAYS:]}
    snapshot = json.dumps(merged, ensure_ascii=False)
    writer.get().stage_state("llm-models-daily", lambda: writer.write_file(path, snapshot, name="llm.models_daily", user=False))
