"""RSS の取得はリダイレクトを追う（各段を検査）。"""
from __future__ import annotations

import asyncio

import pytest

from qnapnewshub import net


def _fake(routes: dict[str, tuple[int, dict, bytes]], seen: list):
    async def _read(client, method, url, params=None, headers=None):
        seen.append((url, dict(headers or {})))
        return routes[url]
    return _read


def test_feed_fetch_follows_redirects_and_keeps_conditional_headers_on_the_same_host(monkeypatch):
    """目的: 移転した RSS（301 など）を取れるようにする。根拠: 2026-11-24 の Linux の評価で AWS の公式ブログの RSS が
    「リダイレクトは追いません」で失敗した。条件付きの見出し（If-None-Match）は別のホストへ送らない。"""
    seen: list = []
    routes = {"https://a.test/feed": (301, {"location": "https://b.test/rss"}, b""),
              "https://b.test/rss": (200, {"etag": '"x"'}, b"<rss/>")}
    monkeypatch.setattr(net, "_read", _fake(routes, seen))
    status, _, body = asyncio.run(net.aget_conditional("https://a.test/feed", etag='"old"'))
    assert status == 200 and body == b"<rss/>"
    assert seen[0][1].get("If-None-Match") == '"old"' and "If-None-Match" not in seen[1][1]
    seen.clear()
    assert asyncio.run(net.aget_bytes("https://a.test/feed")) == b"<rss/>"


def test_feed_redirect_to_a_private_address_is_refused(monkeypatch):
    """目的: リダイレクトの先も各段で検査する（SSRF の守り）。根拠: 追うようにしたので、内部の宛先へ誘導されないこと。"""
    real = net._read
    seen: list = []

    async def _read(client, method, url, params=None, headers=None):
        seen.append(url)
        if url == "https://a.test/feed":      # 1 段目だけ模擬。2 段目は本物の守りつきの接続（egress）に通す
            return 302, {"location": "http://127.0.0.1:9/api/v1/health"}, b""
        return await real(client, method, url, params=params, headers=headers)

    monkeypatch.setattr(net, "_read", _read)
    with pytest.raises(net.NetError):
        asyncio.run(net.aget_bytes("https://a.test/feed"))
    assert seen == ["https://a.test/feed", "http://127.0.0.1:9/api/v1/health"]
