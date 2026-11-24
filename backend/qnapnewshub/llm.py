"""ニュースが使う外部の LLM（OpenRouter の chat completions を httpx で直接呼ぶ）。下ごしらえ・まとめ・一言要約が使う。

- ControlDeck のゲートウェイは無い。鍵は設定の API で入れ（secretbox で暗号化して置き場に）、ここで開いて Authorization に載せる。
  鍵の本文はログにも応答にも例外の文にも出さない（呼び出し側へ返すのは「鍵が未設定」「HTTP 状態」だけ）。
- 送るのは記事の題・抜粋・出どころ（公開情報）と、まとめの重点（利用者が書いた文）だけ。
- 回数の上限（無料のモデルの上限に当たる前に自分で止める）: 1 日（既定 300 回。日が変われば 0）と 1 分（既定 10 回。超えそうなら空くまで待つ）。
  HTTP の要求 1 回を 1 回と数える（混み合いのやり直しも数える）。1 日の上限に達したら LlmDailyLimit（次の日まで何も送らない）。
- 混み合い（429・temporarily rate-limited など）は 20・60・120 秒待って最大 3 回やり直す。待っても変わらない失敗（鍵が無効・モデルが無い）はやり直さない。
- 使った回数・失敗の理由は stats() で設定の画面へ出す（用途別 prep・digest・gist）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime

from qnapnewshub import egress, net, secretbox
from qnapnewshub.config import data_dir

logger = logging.getLogger("qnapnewshub.llm")

CALLER = "news"
KEY_NAME = "openrouter_key"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3.5-super-vl:free"
TIMEOUT_SECONDS = 120
DEFAULT_DAILY_LIMIT = 300
DEFAULT_MINUTE_LIMIT = 10
MAX_MINUTE_WAIT = 65.0
PURPOSES = ("prep", "digest", "gist")


class LlmNotAllowed(RuntimeError):
    """鍵が未設定など、いまは送らない（待っても変わらない）。"""


class LlmUnavailable(RuntimeError):
    """LLM が使えない・失敗した（呼び出し側は元の題と抜粋・見出しの一覧のままにする）。"""


class LlmDailyLimit(LlmUnavailable):
    """1 日の回数の上限に達した（次の日まで送らない）。"""


# ---- 鍵 ----

def key_is_set() -> bool:
    return secretbox.has_secret(KEY_NAME)


def set_key(value: str) -> None:
    value = str(value or "").strip()
    if not 8 <= len(value) <= 300 or re.search(r"\s", value) or not value.isascii():
        raise ValueError("鍵の形が違います（空白なし・8〜300 文字の英数字記号）")
    secretbox.set_secret(KEY_NAME, value)


def delete_key() -> bool:
    return secretbox.delete_secret(KEY_NAME)


# ---- 回数 ----

_lock = threading.Lock()
_recent: deque[float] = deque()
_sleep = asyncio.sleep          # 試験が差し替える
_clock = time.monotonic


def _counter_path():
    return data_dir() / "news" / "llm-calls.json"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_mem: dict[str, dict] = {}       # 置き場ごとの、今日の回数（メモリが正。ファイルへは Write Coordinator がまとめて書く）
MODEL_STATS: dict[str, dict] = {}      # モデルごとの成功・失敗・429・時間（メモリだけ。診断が見る）
USE_BY_PURPOSE_MODEL: dict[str, int] = {}


def _fresh() -> dict:
    return {"day": _today(), "prep": 0, "digest": 0, "gist": 0, "failed": 0, "last_error": ""}


def _read() -> dict:
    key = str(_counter_path())
    raw = _mem.get(key)
    if raw is None:
        try:
            raw = json.loads(_counter_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _mem[key] = raw if isinstance(raw, dict) else {}
        raw = _mem[key]
    if raw.get("day") != _today():
        raw = _mem[key] = _fresh()
    return raw


def _write(raw: dict) -> None:
    """回数はメモリに持ち、ファイルへはまとめ書きのときに（最後の値を）書く。"""
    from qnapnewshub import writer

    path = _counter_path()
    _mem[str(path)] = raw
    snapshot = json.dumps(raw, ensure_ascii=False)

    def do() -> None:
        writer.write_file(path, snapshot, name="llm.counter", user=False)

    writer.get().stage_state("llm-calls", do)


def note_model(model: str, *, ok: bool, status: int | None = None, ms: int = 0) -> None:
    """モデルごとの成否（診断）。429 は混み合いの数。"""
    row = MODEL_STATS.setdefault(model, {"ok": 0, "fail": 0, "rate_limited": 0, "ms_total": 0})
    row["ok" if ok else "fail"] += 1
    row["ms_total"] += ms
    if status == 429:
        row["rate_limited"] += 1


def calls_today(purpose: str | None = None) -> int:
    raw = _read()
    return int(raw.get(purpose, 0)) if purpose else sum(int(raw.get(p, 0)) for p in PURPOSES)


def _count(purpose: str) -> None:
    with _lock:
        raw = _read()
        raw[purpose] = int(raw.get(purpose, 0)) + 1
        _write(raw)


def _record_failure(message: str) -> None:
    with _lock:
        raw = _read()
        raw["failed"] = int(raw.get("failed", 0)) + 1
        raw["last_error"] = message[:200]
        _write(raw)


def stats(daily_limit: int = DEFAULT_DAILY_LIMIT, minute_limit: int = DEFAULT_MINUTE_LIMIT) -> dict:
    """設定の画面へ出す、今日使った回数・失敗の数と直近の理由。"""
    raw = _read()
    return {"day": raw["day"], "calls": {p: int(raw.get(p, 0)) for p in PURPOSES}, "total": calls_today(), "daily_limit": daily_limit,
            "minute_limit": minute_limit, "failed": int(raw.get("failed", 0)), "last_error": str(raw.get("last_error", "")), "key_set": key_is_set()}


async def _reserve(purpose: str, daily_limit: int, minute_limit: int) -> None:
    """要求を 1 回出す前に呼ぶ。1 日の上限なら LlmDailyLimit、1 分の上限なら空くまで待つ。通ったら数える。"""
    while True:
        wait = 0.0
        with _lock:
            raw = _read()
            if sum(int(raw.get(p, 0)) for p in PURPOSES) >= daily_limit:
                raise LlmDailyLimit(f"1 日の上限（{daily_limit} 回）に達したため、明日まで送りません")
            now = _clock()
            while _recent and now - _recent[0] >= 60:
                _recent.popleft()
            if len(_recent) < minute_limit:
                _recent.append(now)
                raw[purpose] = int(raw.get(purpose, 0)) + 1
                _write(raw)
                return
            wait = min(60 - (now - _recent[0]) + 0.1, MAX_MINUTE_WAIT)
        await _sleep(wait)


def reset_minute_window() -> None:
    with _lock:
        _recent.clear()


# ---- 呼び出し ----

RETRY_DELAYS = (20.0, 60.0, 120.0)
_BUSY = re.compile(r"429|rate[ -]?limit|temporarily|too many requests|overloaded|retry shortly|混み合|混雑|503|502", re.IGNORECASE)


def is_busy(message: str) -> bool:
    """一時的な混み合いの失敗か（待てば通る見込みがある）。"""
    return bool(_BUSY.search(message or ""))


def _limits() -> tuple[int, int]:
    from qnapnewshub import settings

    cfg = settings.load()["llm"]
    return cfg["daily_limit"], cfg["minute_limit"]


async def complete(model: str, messages: list[dict], *, max_tokens: int = 2048, temperature: float = 0.3, purpose: str = "prep") -> str:
    """1 回の生成。混み合いなら RETRY_DELAYS の間隔で待ってやり直す（最大 3 回）。鍵が無ければ LlmNotAllowed、1 日の上限なら LlmDailyLimit、それ以外の失敗は LlmUnavailable。"""
    if purpose not in PURPOSES:
        raise ValueError("purpose が違います")
    daily, per_minute = await asyncio.to_thread(_limits)
    for delay in (*RETRY_DELAYS, None):
        try:
            return await _complete_once(model, messages, max_tokens=max_tokens, temperature=temperature, purpose=purpose, daily=daily, per_minute=per_minute)
        except LlmDailyLimit:
            raise
        except LlmUnavailable as exc:
            if delay is None or not is_busy(str(exc)):
                await asyncio.to_thread(_record_failure, str(exc))
                raise
            logger.info("LLM が混み合っているので %d 秒後にやり直します（%s）: %s", int(delay), model, exc)
            await _sleep(delay)
    raise LlmUnavailable("LLM が混み合っていました")   # ここには来ない


async def _post(body: dict, key: str) -> tuple[int, str]:
    """OpenRouter へ 1 回 POST（試験が差し替える）。(HTTP の状態, 本文)。鍵はここだけで使う。"""
    import httpx

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": net.UA,
               "X-Title": "QNAP News Hub"}
    try:
        async with egress.guarded_client(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(ENDPOINT, json=body, headers=headers)
            return resp.status_code, resp.text[:200_000]
    except egress.EgressRejected as exc:
        raise LlmUnavailable(f"接続しませんでした: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise LlmUnavailable("OpenRouter が時間内に応答しませんでした") from exc
    except httpx.HTTPError as exc:
        raise LlmUnavailable(f"OpenRouter に接続できません: {type(exc).__name__}") from exc


def _error_message(status: int, text: str) -> str:
    try:
        detail = json.loads(text).get("error", {})
        reason = detail.get("message") if isinstance(detail, dict) else str(detail)
    except (ValueError, AttributeError):
        reason = ""
    reason = re.sub(r"sk-[A-Za-z0-9_-]{6,}", "[鍵]", str(reason or ""))[:150]
    return f"HTTP {status}" + (f": {reason}" if reason else "")


async def _complete_once(model: str, messages: list[dict], *, max_tokens: int, temperature: float, purpose: str, daily: int, per_minute: int) -> str:
    try:
        key = await asyncio.to_thread(secretbox.get_secret, KEY_NAME)
    except secretbox.SecretError as exc:
        raise LlmNotAllowed(str(exc)) from exc
    if not key:
        raise LlmNotAllowed("OpenRouter の鍵が未設定です（設定で入れてください）")
    await _reserve(purpose, daily, per_minute)
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "reasoning": {"enabled": False}}
    started = _clock()
    try:
        status, text = await _post(body, key)
    except LlmUnavailable:
        note_model(model, ok=False, ms=int((_clock() - started) * 1000))
        raise
    if status >= 400:
        note_model(model, ok=False, status=status, ms=int((_clock() - started) * 1000))
        raise LlmUnavailable(_error_message(status, text))
    note_model(model, ok=True, status=status, ms=int((_clock() - started) * 1000))
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):      # 200 のまま混み合いを返すことがある
            err = data["error"] if isinstance(data["error"], dict) else {}
            raise LlmUnavailable(_error_message(int(err.get("code") or 502) if str(err.get("code") or "").isdigit() else 502, text))
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmUnavailable("OpenRouter の答えの形が違います") from exc
    if not isinstance(content, str) or not content.strip():
        raise LlmUnavailable("LLM の答えが空でした")
    return content
