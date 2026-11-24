"""ニュースが使う外部の LLM（OpenRouter の chat completions を httpx で直接呼ぶ）。下ごしらえ・まとめ・一言要約が使う。

- ControlDeck のゲートウェイは無い。鍵は設定の API で入れ（secretbox で暗号化して置き場に）、ここで開いて Authorization に載せる。
  鍵の本文はログにも応答にも例外の文にも出さない（呼び出し側へ返すのは「鍵が未設定」「HTTP 状態」だけ）。
- 送るのは記事の題・抜粋・出どころ（公開情報）と、まとめの重点（利用者が書いた文）だけ。
- 回数の上限（無料のモデルの上限に当たる前に自分で止める）: 1 日（既定 800 回。日が変われば 0）と 1 分（既定 15 回。超えそうなら空くまで待つ）。
  HTTP の要求 1 回を 1 回と数える（混み合いのやり直しも数える）。1 日の上限に達したら LlmDailyLimit（次の日まで何も送らない）。
- 用途ごとの割り当て（設定 llm.quota。既定: 下ごしらえと取り出し prep 300・要約 gist 150・まとめ digest 20・知識カード card 150・傾向の解説 trend 10・イベントの要約 event 100・予備 spare 70。
  合計が 1 日の上限を超えない）。自分の割り当てを使い切った用途は予備から借りる。予備も尽きたら LlmQuotaLimit。
  夜（night_from 時以降）は、使い残した割り当ての合計までを、後回しの仕事 backfill（古い記事の取り出し直し）が使える。
- 混み合い（429・temporarily rate-limited など）は 20・60・120 秒待って最大 3 回やり直す。待っても変わらない失敗（鍵が無効・モデルが無い）はやり直さない。
- **ControlDeck のゲートウェイ**（OpenAI 互換。設定 llm.gateway・鍵 gateway_key）も行き先の 1 つ（モデル名は "gateway:<名前>"）。1 日の上限・用途ごとの割り当ては OpenRouter にだけ掛け、
  ゲートウェイには 1 分の上限（gateway.minute_limit）と同時 1 本だけを掛ける。URL は設定の値だけを使う（記事の中の URL からは決して呼ばない。LAN の私的な IP も許す）。
  つながらない（接続できない・時間切れ・5xx）と分かったら gateway.cooldown_minutes の間は順番から外す（llm_models.gateway_down）。
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
DEFAULT_DAILY_LIMIT = 800
DEFAULT_MINUTE_LIMIT = 15
DEFAULT_QUOTA = {"prep": 300, "gist": 150, "digest": 20, "card": 150, "trend": 10, "event": 100, "spare": 70}
QUOTA_LABELS = {"prep": "下ごしらえと取り出し", "gist": "要約", "digest": "まとめ", "card": "知識カード", "trend": "傾向の解説", "event": "イベントの要約", "spare": "予備"}
DEFAULT_NIGHT_FROM = 21
GATEWAY_KEY_NAME = "gateway_key"
GATEWAY_PREFIX = "gateway:"
DEFAULT_GATEWAY = {"enabled": False, "base_url": "", "model": "auto", "first": True, "batch": 10, "timeout_seconds": 300, "minute_limit": 6, "cooldown_minutes": 10}
MAX_MINUTE_WAIT = 65.0
PURPOSES = ("prep", "digest", "gist", "card", "trend", "event", "backfill")
QUOTA_PURPOSES = ("prep", "gist", "digest", "card", "trend", "event")


class LlmNotAllowed(RuntimeError):
    """鍵が未設定など、いまは送らない（待っても変わらない）。"""


class LlmUnavailable(RuntimeError):
    """LLM が使えない・失敗した（呼び出し側は元の題と抜粋・見出しの一覧のままにする）。"""


class LlmDailyLimit(LlmUnavailable):
    """1 日の回数の上限に達した（次の日まで送らない）。"""


class LlmQuotaLimit(LlmDailyLimit):
    """その用途の割り当て（と予備）を使い切った・夜の後回しの枠が無い（呼び出し側は、1 日の上限と同じく「後で」とする）。"""


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


def is_gateway(model: str) -> bool:
    return model.startswith(GATEWAY_PREFIX)


def gateway_key_is_set() -> bool:
    return secretbox.has_secret(GATEWAY_KEY_NAME)


def set_gateway_key(value: str) -> None:
    value = str(value or "").strip()
    if not 4 <= len(value) <= 400 or re.search(r"\s", value) or not value.isascii():
        raise ValueError("鍵の形が違います（空白なし・4〜400 文字の英数字記号）")
    secretbox.set_secret(GATEWAY_KEY_NAME, value)


def delete_gateway_key() -> bool:
    return secretbox.delete_secret(GATEWAY_KEY_NAME)


# ---- 回数 ----

_lock = threading.Lock()
_recent: deque[float] = deque()
_gw_recent: deque[float] = deque()      # ゲートウェイの 1 分の窓（OpenRouter の窓とは別）
_sleep = asyncio.sleep          # 試験が差し替える
_clock = time.monotonic


def _counter_path():
    return data_dir() / "news" / "llm-calls.json"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_mem: dict[str, dict] = {}       # 置き場ごとの、今日の回数（メモリが正。ファイルへは Write Coordinator がまとめて書く）
MODEL_STATS: dict[str, dict] = {}      # モデルごとの今日の成功・失敗・429・時間・日本語の率（メモリだけ。診断が見る。日が変わると前の日の要約を 1 回保存して空にする）
_model_day = [_today()]
USE_BY_PURPOSE_MODEL: dict[str, int] = {}


def _fresh() -> dict:
    return {"day": _today(), **{p: 0 for p in PURPOSES}, "spare_used": 0, "gateway": 0, "failed": 0, "last_error": ""}


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


def _roll_models() -> None:
    """日が変わっていたら、前の日のモデルごとの要約を 1 回だけ保存して、今日の分を空にする。"""
    today = _today()
    if _model_day[0] == today:
        return
    old, _model_day[0] = _model_day[0], today
    snapshot = {m: dict(row) for m, row in MODEL_STATS.items()}
    MODEL_STATS.clear()
    try:
        from qnapnewshub import llm_models

        llm_models.save_daily(old, snapshot)
    except Exception:  # noqa: BLE001 - 診断の保存の失敗で本線を止めない
        logger.exception("モデルごとの 1 日の要約を保存できませんでした")


def _row(model: str) -> dict:
    _roll_models()
    return MODEL_STATS.setdefault(model, {"ok": 0, "fail": 0, "rate_limited": 0, "ms_total": 0, "ja_ok": 0, "ja_total": 0, "quality_fail": 0})


def note_model(model: str, *, ok: bool, status: int | None = None, ms: int = 0) -> None:
    """モデルごとの成否（診断）。429 は混み合いの数。"""
    row = _row(model)
    row["ok" if ok else "fail"] += 1
    row["ms_total"] += ms
    if ok:
        row["last_ok"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if status == 429:
        row["rate_limited"] += 1


def note_quality(model: str, *, ja_ok: int, ja_total: int, batch_ok: bool) -> None:
    """検査の結果（下ごしらえ）。日本語で返った記事の数・送った記事の数。1 回の呼び出しが検査に落ちたら quality_fail。"""
    row = _row(model)
    row["ja_ok"] += ja_ok
    row["ja_total"] += ja_total
    if not batch_ok:
        row["quality_fail"] += 1


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


def default_quota(daily_limit: int) -> dict[str, int]:
    """設定に割り当てが無いとき（古い設定）: 既定の割合を 1 日の上限に合わせて縮める。合計は上限を超えない。"""
    if daily_limit >= sum(DEFAULT_QUOTA.values()):
        return dict(DEFAULT_QUOTA)
    scaled = {k: DEFAULT_QUOTA[k] * daily_limit // sum(DEFAULT_QUOTA.values()) for k in DEFAULT_QUOTA}
    scaled["spare"] += daily_limit - sum(scaled.values())
    return scaled


def leftover(raw: dict, quota: dict[str, int]) -> int:
    """使い残した割り当ての合計（予備の残りを含む）。夜の後回しの仕事が使える上限。"""
    free = sum(max(0, int(quota.get(p, 0)) - int(raw.get(p, 0))) for p in QUOTA_PURPOSES)
    return free + max(0, int(quota.get("spare", 0)) - int(raw.get("spare_used", 0)))


def check_budget(raw: dict, purpose: str, *, daily: int, quota: dict[str, int], hour: int, night_from: int) -> tuple[bool, bool, str]:
    """(通すか, 予備から借りるか, 断る理由)。純粋な判定（試験が直接呼ぶ）。"""
    if sum(int(raw.get(p, 0)) for p in PURPOSES) >= daily:
        return False, False, f"1 日の上限（{daily} 回）に達したため、明日まで送りません"
    if purpose == "backfill":
        if hour < night_from:
            return False, False, f"後回しの仕事は {night_from} 時以降にだけ使います"
        if int(raw.get("backfill", 0)) >= leftover(raw, quota):
            return False, False, "使い残した枠を使い切りました"
        return True, False, ""
    if int(raw.get(purpose, 0)) < int(quota.get(purpose, 0)):
        return True, False, ""
    if int(raw.get("spare_used", 0)) < int(quota.get("spare", 0)):
        return True, True, ""
    return False, False, f"{QUOTA_LABELS.get(purpose, purpose)}の 1 日の割り当て（{quota.get(purpose, 0)} 回）と予備を使い切ったため、明日まで送りません"


def stats(daily_limit: int | None = None, minute_limit: int | None = None) -> dict:
    """設定の画面へ出す、今日使った回数（用途ごと）・割り当て・失敗の数と直近の理由。"""
    _roll_models()
    cfg = _cfg()
    daily_limit = cfg["daily"] if daily_limit is None else daily_limit
    minute_limit = cfg["per_minute"] if minute_limit is None else minute_limit
    raw = _read()
    quota = cfg["quota"] if daily_limit == cfg["daily"] else default_quota(daily_limit)
    return {"day": raw["day"], "calls": {p: int(raw.get(p, 0)) for p in PURPOSES}, "total": calls_today(), "daily_limit": daily_limit,
            "minute_limit": minute_limit, "failed": int(raw.get("failed", 0)), "last_error": str(raw.get("last_error", "")), "key_set": key_is_set(),
            "quota": quota, "gateway_calls": int(raw.get("gateway", 0)), "gateway_key_set": gateway_key_is_set(), "spare_used": int(raw.get("spare_used", 0)), "night_from": cfg["night_from"], "leftover": leftover(raw, quota)}


async def _reserve(purpose: str, cfg: dict) -> None:
    """要求を 1 回出す前に呼ぶ。1 日の上限・用途の割り当てなら例外、1 分の上限なら空くまで待つ。通ったら数える。"""
    while True:
        wait = 0.0
        with _lock:
            raw = _read()
            ok, spare, why = check_budget(raw, purpose, daily=cfg["daily"], quota=cfg["quota"], hour=datetime.now().hour, night_from=cfg["night_from"])
            if not ok:
                total = sum(int(raw.get(p, 0)) for p in PURPOSES)
                raise (LlmDailyLimit if total >= cfg["daily"] else LlmQuotaLimit)(why)
            now = _clock()
            while _recent and now - _recent[0] >= 60:
                _recent.popleft()
            if len(_recent) < cfg["per_minute"]:
                _recent.append(now)
                raw[purpose] = int(raw.get(purpose, 0)) + 1
                if spare:
                    raw["spare_used"] = int(raw.get("spare_used", 0)) + 1
                _write(raw)
                return
            wait = min(60 - (now - _recent[0]) + 0.1, MAX_MINUTE_WAIT)
        await _sleep(wait)


def reset_minute_window() -> None:
    with _lock:
        _recent.clear()
        _gw_recent.clear()


# ---- 呼び出し ----

RETRY_DELAYS = (20.0, 60.0, 120.0)
_BUSY = re.compile(r"429|rate[ -]?limit|temporarily|too many requests|overloaded|retry shortly|混み合|混雑|503|502", re.IGNORECASE)


def is_busy(message: str) -> bool:
    """一時的な混み合いの失敗か（待てば通る見込みがある）。"""
    return bool(_BUSY.search(message or ""))


def _cfg() -> dict:
    """設定から 1 日・1 分の上限・用途ごとの割り当て・夜の始まりの時。"""
    from qnapnewshub import settings

    cfg = settings.load()["llm"]
    return {"daily": cfg["daily_limit"], "per_minute": cfg["minute_limit"], "quota": cfg["quota"], "night_from": cfg["night_from"], "gateway": cfg["gateway"]}


async def complete(model: str, messages: list[dict], *, max_tokens: int = 2048, temperature: float = 0.3, purpose: str = "prep", abandon=None) -> str:
    """1 回の生成。混み合いなら RETRY_DELAYS の間隔で待ってやり直す（最大 3 回）。鍵が無ければ LlmNotAllowed、1 日の上限なら LlmDailyLimit、それ以外の失敗は LlmUnavailable。"""
    if purpose not in PURPOSES:
        raise ValueError("purpose が違います")
    cfg = await asyncio.to_thread(_cfg)
    for delay in (*RETRY_DELAYS, None):
        try:
            return await _complete_once(model, messages, max_tokens=max_tokens, temperature=temperature, purpose=purpose, cfg=cfg)
        except LlmDailyLimit:
            raise
        except LlmUnavailable as exc:
            if delay is None or not is_busy(str(exc)) or (abandon is not None and abandon()):      # 待っている間にモデルが切り替わったら、待たずに呼び出し側へ返す
                await asyncio.to_thread(_record_failure, str(exc))
                raise
            logger.info("LLM が混み合っているので %d 秒後にやり直します（%s）: %s", int(delay), model, exc)
            await _sleep(delay)
    raise LlmUnavailable("LLM が混み合っていました")   # ここには来ない


def _chain_fail(model: str, reason: str) -> None:
    from qnapnewshub import llm_models

    llm_models.record(model, False, reason)
    if is_gateway(model) and (reason == "通信の失敗" or reason.startswith("HTTP 5")):      # つながらない（電源が切れている・時間切れ・5xx）→ しばらく順番から外す
        llm_models.gateway_down(reason)


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


def _content_from(model: str, text: str) -> str:
    """OpenAI 互換の答え（choices[0].message.content）を取り出す。形が違う・空なら LlmUnavailable。"""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):      # 200 のまま混み合いを返すことがある
            err = data["error"] if isinstance(data["error"], dict) else {}
            _chain_fail(model, "答えの中の混み合い")
            raise LlmUnavailable(_error_message(int(err.get("code") or 502) if str(err.get("code") or "").isdigit() else 502, text))
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmUnavailable("OpenRouter の答えの形が違います" if not is_gateway(model) else "ゲートウェイの答えの形が違います") from exc
    if not isinstance(content, str) or not content.strip():
        _chain_fail(model, "空の答え")
        raise LlmUnavailable("LLM の答えが空でした")
    return content


async def _complete_once(model: str, messages: list[dict], *, max_tokens: int, temperature: float, purpose: str, cfg: dict) -> str:
    if is_gateway(model):
        return await _complete_gateway(model, messages, max_tokens=max_tokens, temperature=temperature, gw=cfg["gateway"])
    try:
        key = await asyncio.to_thread(secretbox.get_secret, KEY_NAME)
    except secretbox.SecretError as exc:
        raise LlmNotAllowed(str(exc)) from exc
    if not key:
        raise LlmNotAllowed("OpenRouter の鍵が未設定です（設定で入れてください）")
    await _reserve(purpose, cfg)
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "reasoning": {"enabled": False}}
    started = _clock()
    try:
        status, text = await _post(body, key)
    except LlmUnavailable:
        note_model(model, ok=False, ms=int((_clock() - started) * 1000))
        _chain_fail(model, "通信の失敗")
        raise
    if status >= 400:
        note_model(model, ok=False, status=status, ms=int((_clock() - started) * 1000))
        _chain_fail(model, "429" if status == 429 else f"HTTP {status}")
        raise LlmUnavailable(_error_message(status, text))
    note_model(model, ok=True, status=status, ms=int((_clock() - started) * 1000))
    return _content_from(model, text)


# ---- ControlDeck のゲートウェイ（OpenAI 互換） ----

_gw_sems: dict[int, asyncio.Semaphore] = {}


def _gw_sem() -> asyncio.Semaphore:
    """同時 1 本（ControlDeck のチャット・開発を邪魔しない）。イベントループごとに 1 つ。"""
    loop = id(asyncio.get_running_loop())
    if loop not in _gw_sems:
        _gw_sems.clear()
        _gw_sems[loop] = asyncio.Semaphore(1)
    return _gw_sems[loop]


async def _gw_reserve(per_minute: int) -> None:
    while True:
        with _lock:
            now = _clock()
            while _gw_recent and now - _gw_recent[0] >= 60:
                _gw_recent.popleft()
            if len(_gw_recent) < per_minute:
                _gw_recent.append(now)
                return
            wait = min(60 - (now - _gw_recent[0]) + 0.1, MAX_MINUTE_WAIT)
        await _sleep(wait)


def _count_gateway() -> None:
    with _lock:
        raw = _read()
        raw["gateway"] = int(raw.get("gateway", 0)) + 1
        _write(raw)


def _gw_headers(key: str) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": net.UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def _post_gateway(base_url: str, body: dict, key: str, timeout: float) -> tuple[int, str]:
    """ゲートウェイへ 1 回 POST（試験が差し替える）。URL は設定の値だけ（LAN の私的な IP も許す。リダイレクトは追わない）。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False) as client:
            resp = await client.post(base_url.rstrip("/") + "/chat/completions", json=body, headers=_gw_headers(key))
            return resp.status_code, resp.text[:200_000]
    except httpx.TimeoutException as exc:
        raise LlmUnavailable("ゲートウェイが時間内に応答しませんでした") from exc
    except httpx.HTTPError as exc:
        raise LlmUnavailable(f"ゲートウェイに接続できません: {type(exc).__name__}") from exc


async def get_gateway_models(base_url: str, key: str, timeout: float = 15.0) -> tuple[int, str]:
    """GET {base}/models（接続を試す・モデルの一覧。試験が差し替える）。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=8.0), follow_redirects=False) as client:
            resp = await client.get(base_url.rstrip("/") + "/models", headers=_gw_headers(key))
            return resp.status_code, resp.text[:500_000]
    except httpx.TimeoutException as exc:
        raise LlmUnavailable("ゲートウェイが時間内に応答しませんでした") from exc
    except httpx.HTTPError as exc:
        raise LlmUnavailable(f"ゲートウェイに接続できません: {type(exc).__name__}") from exc


async def _complete_gateway(model: str, messages: list[dict], *, max_tokens: int, temperature: float, gw: dict) -> str:
    if not gw["enabled"] or not gw["base_url"]:
        raise LlmNotAllowed("ゲートウェイが設定されていません")
    try:
        key = await asyncio.to_thread(secretbox.get_secret, GATEWAY_KEY_NAME) or ""
    except secretbox.SecretError as exc:
        raise LlmNotAllowed(str(exc)) from exc
    await _gw_reserve(gw["minute_limit"])
    body = {"model": model[len(GATEWAY_PREFIX):], "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    async with _gw_sem():
        started = _clock()
        try:
            status, text = await _post_gateway(gw["base_url"], body, key, gw["timeout_seconds"])
        except LlmUnavailable:
            note_model(model, ok=False, ms=int((_clock() - started) * 1000))
            _chain_fail(model, "通信の失敗")
            raise
    await asyncio.to_thread(_count_gateway)
    ms = int((_clock() - started) * 1000)
    if status >= 400:
        note_model(model, ok=False, status=status, ms=ms)
        _chain_fail(model, f"HTTP {status}")
        raise LlmUnavailable(_error_message(status, text).replace(key, "[鍵]") if key else _error_message(status, text))
    note_model(model, ok=True, status=status, ms=ms)
    return _content_from(model, text)
