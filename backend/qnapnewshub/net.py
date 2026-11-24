"""外への取得の入口（ControlDeck の knowledge.sources.http_get・workflows.research_tools の取得を置き換える小さな実装）。

- 接続はすべて egress（名前を解決 → 全アドレスを検査 → 検査した IP へ接続。私的・ループバック・リンクローカルへは行かない）。
- 大きさ（MAX_BYTES）・時間（timeout）の上限。リダイレクトは自動で追わず、追うときは各段の URL を検査する（http/https・認証情報なし）。
- http_get（RSS など）は https だけ・リダイレクトを追わない（ControlDeck の本番と同じ。probe の確認が同じ条件で判定する）。
- 失敗は HttpError（HTTP の 4xx/5xx）か NetError（それ以外。利用者に見せてよい文）。URL の query・鍵は文に入れない。
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from qnapnewshub import egress



UA = "Mozilla/5.0 (compatible; QnapNewsHub/0.1)"
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3

_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}


class NetError(RuntimeError):
    """通信の失敗（内部のアドレス・大きすぎる・接続できない・https でない）。"""


class HttpError(RuntimeError):
    """HTTP の 4xx・5xx。status で言い分ける。"""

    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after      # 秒（Retry-After ヘッダー。無ければ None）


def _check_url(url: str, *, https_only: bool) -> None:
    parts = urlsplit(url)
    ok_scheme = parts.scheme == "https" or (not https_only and parts.scheme == "http")
    if not ok_scheme or not parts.hostname or parts.username or parts.password:
        raise NetError("https の URL だけを使えます" if https_only else "http/https の URL だけを使えます")


async def _read(client: httpx.AsyncClient, method: str, url: str, *, max_bytes: int = MAX_BYTES, **kwargs) -> tuple[int, dict, bytes]:
    try:
        async with client.stream(method, url, **kwargs) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, b""
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise NetError("応答が大きすぎます")
                chunks.append(chunk)
            return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, b"".join(chunks)
    except egress.EgressRejected as exc:
        raise NetError(str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise NetError("時間内に応答がありません") from exc
    except httpx.HTTPError as exc:
        raise NetError(f"接続できません: {type(exc).__name__}") from exc


async def get(url: str, *, params: dict | None = None, accept: str = "application/json", headers: dict | None = None,
              timeout: float = 25, follow: bool = True, max_bytes: int = MAX_BYTES) -> tuple[int, dict, bytes]:
    """GET 1 回。follow なら 3 段までリダイレクトを追う（各段を検査。認証のヘッダーは別のホストへ付けない）。(status, headers, 本文)。"""
    _check_url(url, https_only=False)
    current = url
    async with egress.guarded_client(timeout=timeout, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "ja,en;q=0.8"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            same = urlsplit(current).hostname == urlsplit(url).hostname
            status, head, body = await _read(client, "GET", current, params=params if current == url else None, headers=headers if same else None, **({} if max_bytes == MAX_BYTES else {"max_bytes": max_bytes}))
            if follow and status in (301, 302, 303, 307, 308) and head.get("location"):
                current = urljoin(current, head["location"])
                _check_url(current, https_only=False)
                continue
            return status, head, body
    raise NetError("リダイレクトが多すぎます")


async def get_json(url: str, **kwargs):
    status, _, body = await get(url, **kwargs)
    if status >= 400:
        raise HttpError(status)
    import json

    try:
        return json.loads(body)
    except ValueError as exc:
        raise NetError("JSON として読めません") from exc


async def reddit_oauth(path: str, params: dict):
    """Reddit の API（鍵が要る）。ニュースハブは Reddit の鍵を持たないので、常に「未設定」（None）。ソース側が取得しない注記を出す。"""
    return None


async def aget_bytes(url: str, *, timeout: float = 30) -> bytes:
    """RSS などの取得（https だけ・リダイレクトは 3 段まで各段を検査して追う・大きさの上限）。
    2026-11-24: Linux の評価で AWS の公式ブログの RSS が「リダイレクトは追いません」で取れなかったため、追うように変えた。"""
    _check_url(url, https_only=True)
    status, _, body = await get(url, accept="*/*", timeout=timeout, follow=True)
    if 300 <= status < 400:
        raise NetError("リダイレクトは追いません")
    if status >= 400:
        raise NetError(f"HTTP {status}")
    return body


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Retry-After（秒数か HTTP の日時）を、待つ秒数にする。読めなければ None。負の値は 0。"""
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit():
        return float(text)
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(text)
        return max(0.0, when.timestamp() - (now if now is not None else time.time()))
    except (TypeError, ValueError, OverflowError):
        return None


async def aget_conditional(url: str, *, etag: str = "", last_modified: str = "", timeout: float = 30) -> tuple[int, dict, bytes]:
    """条件付きの GET（https だけ・リダイレクトは 3 段まで各段を検査して追う）。ETag・Last-Modified があれば If-None-Match・If-Modified-Since を付ける。
    304 は (304, 応答の見出し, b"") で返す（本文は読まない）。4xx・5xx は HttpError（Retry-After つき）。"""
    _check_url(url, https_only=True)
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    status, head, body = await get(url, accept="*/*", timeout=timeout, follow=True, headers=headers or None)
    if status == 304:
        return status, head, b""
    if 300 <= status < 400:
        raise NetError("リダイレクトは追いません")
    if status >= 400:
        raise HttpError(status, parse_retry_after(head.get("retry-after")))
    return status, head, body


def http_get_conditional(url: str, *, etag: str = "", last_modified: str = "", pace: float = 1.0, timeout: int = 30) -> tuple[int, dict, bytes]:
    """同期の条件付き取得（呼び出し側は asyncio.to_thread）。同じホストへは pace 秒あける。"""
    _check_url(url, https_only=True)
    host = urlsplit(url).hostname or ""
    with _pace_lock:
        wait = pace - (time.time() - _last_call.get(host, 0))
        _last_call[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(min(wait, 10))
    return asyncio.run(aget_conditional(url, etag=etag, last_modified=last_modified, timeout=timeout))


def http_get(url: str, *, pace: float = 1.0, timeout: int = 30) -> bytes:
    """同期の取得（呼び出し側は asyncio.to_thread）。同じホストへは pace 秒あけて取る。"""
    _check_url(url, https_only=True)
    host = urlsplit(url).hostname or ""
    with _pace_lock:
        wait = pace - (time.time() - _last_call.get(host, 0))
        _last_call[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(min(wait, 10))
    return asyncio.run(aget_bytes(url, timeout=timeout))


_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def valid_domain(text: str) -> str:
    """ドメインの形だけ許す。正規化した名前を返し、IP・localhost・形が違うものは ValueError。"""
    domain = re.sub(r"^[a-z]+://", "", str(text or "").strip().lower(), flags=re.I).split("/", 1)[0].split(":", 1)[0]
    domain = domain.removeprefix("www.")
    if re.fullmatch(r"[0-9.]+", domain) or domain.endswith((".local", ".localhost", ".internal", ".lan")) or not _DOMAIN.match(domain):
        raise ValueError(f"フォーラムのドメインとして使えません: {str(text)[:60]}")
    return domain


async def trending_gguf(limit: int = 30) -> list[dict]:
    """Hugging Face の話題の GGUF（trendingScore 順）。ニュースの「モデル」の新着。1 行は {id, url, created, downloads, likes, pipeline, params_b}。"""
    data = await get_json("https://huggingface.co/api/models",
                          params={"filter": "gguf", "sort": "trendingScore", "direction": "-1", "limit": max(1, min(limit, 100)),
                                  "expand[]": ["downloads", "likes", "createdAt", "pipeline_tag", "safetensors", "gguf"]})
    out = []
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        total = (raw.get("safetensors") or {}).get("total") or (raw.get("gguf") or {}).get("total")
        out.append({"id": str(raw["id"]), "url": f"https://huggingface.co/{raw['id']}", "created": str(raw.get("createdAt") or "")[:10],
                    "downloads": int(raw.get("downloads") or 0), "likes": int(raw.get("likes") or 0),
                    "pipeline": str(raw.get("pipeline_tag") or ""), "params_b": round(total / 1e9, 2) if total else None})
    return out[:limit]
