"""LAN での発見の応答（UDP）の試験。

目的: ControlDeck の「探す」が使う発見の応答が、(1) 正しい問いにだけ答え、(2) 秘密を出さず、(3) 127.0.0.1 待ち受けでは答えず、(4) 増幅の悪用に使えないこと。
根拠: 2026-11-25 利用者「公開されている場合自動でアドレスを検知できないか？」・docs/DESIGN.md 3.5・AGENTS.md「秘密値をログ・応答へ出さない」。
"""
from __future__ import annotations

import asyncio
import json
import socket

import pytest

from qnapnewshub import discovery

QUERY = json.dumps({"q": discovery.MAGIC_Q, "v": discovery.PROTOCOL}).encode()


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, data: bytes, addr: tuple) -> None:
        self.sent.append((data, addr))


def responder(host: str = "0.0.0.0", port: int = 8787, clock=None) -> tuple[discovery.Responder, FakeTransport]:  # noqa: ANN001
    r = discovery.Responder(host, port, lambda: "1a2b-3c4d-5e6f-7a8b-9c0d", clock or (lambda: 100.0))
    t = FakeTransport()
    r.connection_made(t)
    return r, t


def test_answers_a_correct_query_with_urls_and_fingerprint(monkeypatch):
    monkeypatch.setattr(discovery, "interface_addresses", lambda: ["192.0.2.10", "198.51.100.7"])
    monkeypatch.setattr(discovery, "route_address", lambda peer: "198.51.100.7")
    r, t = responder()
    r.datagram_received(QUERY, ("198.51.100.99", 40000))
    body = json.loads(t.sent[0][0])
    assert body["q"] == discovery.MAGIC_A and body["v"] == 1 and body["name"] and body["version"]
    assert body["urls"] == ["http://198.51.100.7:8787", "http://192.0.2.10:8787"]      # 送り元へ向かう道のアドレスが先頭
    assert body["fingerprint"] == "1a2b-3c4d-5e6f-7a8b-9c0d"
    assert t.sent[0][1] == ("198.51.100.99", 40000)


@pytest.mark.parametrize("data", [b"", b"hello", b"[]", b'{"q":"other","v":1}', b'{"q":"qnapnewshub-discover","v":2}', b'{"q":"qnapnewshub-discover"}',
                                  b"\xff\xfe", b'{"q":"qnapnewshub-discover","v":1,"pad":"' + b"x" * 300 + b'"}'])
def test_ignores_malformed_queries(monkeypatch, data):
    monkeypatch.setattr(discovery, "interface_addresses", lambda: ["192.0.2.10"])
    r, t = responder()
    r.datagram_received(data, ("192.0.2.5", 1))
    assert t.sent == []


def test_reply_never_contains_secrets(monkeypatch, tmp_path):
    """答えの鍵は決まった 6 つだけ。トークン・番号・ユーザー名・データの置き場のパス・秘密の値を含まない。"""
    monkeypatch.setattr(discovery, "interface_addresses", lambda: ["192.0.2.10"])
    from qnapnewshub import link, secretbox

    secretbox.set_secret("openrouter", "sk-or-v1-" + "q" * 24)
    link.start_pairing(1)
    r, t = responder()
    r.datagram_received(QUERY, ("192.0.2.5", 1))
    raw = t.sent[0][0].decode()
    assert set(json.loads(raw)) == {"q", "v", "name", "version", "urls", "fingerprint"}
    assert "sk-or-v1" not in raw and str(tmp_path) not in raw and "token" not in raw.lower()
    link.cancel_pairing()


def test_reply_size_is_capped():
    urls = [f"http://192.0.2.{i}:8787" for i in range(1, 200)]
    assert len(discovery.build_reply(urls, "1a2b-3c4d-5e6f-7a8b-9c0d")) <= discovery.MAX_REPLY


def test_rate_limit_per_source_and_global(monkeypatch):
    """増幅の悪用を防ぐ: 同じ送り元は 1 秒に 1 通・全体で 5 通。次の秒には答える。"""
    monkeypatch.setattr(discovery, "interface_addresses", lambda: ["192.0.2.10"])
    now = [100.0]
    r, t = responder(clock=lambda: now[0])
    for _ in range(5):
        r.datagram_received(QUERY, ("192.0.2.5", 1))
    assert len(t.sent) == 1
    for i in range(20):
        r.datagram_received(QUERY, (f"192.0.2.{50 + i}", 1))
    assert len(t.sent) == discovery.GLOBAL_PER_SEC
    now[0] = 101.0
    r.datagram_received(QUERY, ("192.0.2.5", 1))
    assert len(t.sent) == discovery.GLOBAL_PER_SEC + 1


def test_loopback_listen_does_not_answer(monkeypatch):
    monkeypatch.setenv("NEWSHUB_HOST", "127.0.0.1")
    monkeypatch.setenv("NEWSHUB_DISCOVERY_PORT", "18788")
    assert asyncio.run(discovery.start()) is None
    assert discovery.status() == {"lan": False, "discovery_port": 18788, "answering": False, "urls": []}
    assert discovery.candidate_urls("127.0.0.1", 8787) == []


def test_discovery_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("NEWSHUB_HOST", "0.0.0.0")
    monkeypatch.setenv("NEWSHUB_DISCOVERY_PORT", "off")
    assert asyncio.run(discovery.start()) is None
    assert discovery.status()["answering"] is False


def test_hubconf_discovery_port_validation():
    from qnapnewshub import hubconf
    from qnapnewshub.sources import ConfigError

    assert hubconf.normalize({})["discovery_port"] == 8788
    assert hubconf.normalize({"discovery_port": 0})["discovery_port"] == 0
    for bad in (80, 70000, "x"):
        with pytest.raises(ConfigError):
            hubconf.normalize({"discovery_port": bad})


def test_real_udp_round_trip_on_loopback_address(monkeypatch):
    """実際の UDP の往復: 127.0.0.2 に待ち受けを張り、問い → 答え。違う形には答えない（1 秒待っても来ない）。"""
    monkeypatch.setenv("NEWSHUB_HOST", "127.0.0.2")      # 127.0.0.1 以外なので「LAN に出している」扱い
    monkeypatch.setenv("NEWSHUB_PORT", "18787")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.2", 0))
        udp_port = probe.getsockname()[1]
    monkeypatch.setenv("NEWSHUB_DISCOVERY_PORT", "off")
    monkeypatch.setattr(discovery, "configured_port", lambda: udp_port if udp_port >= 1024 else 0)

    async def run() -> tuple[dict, bool]:
        transport = await discovery.start()
        assert transport is not None
        loop = asyncio.get_running_loop()
        got: asyncio.Queue = asyncio.Queue()

        class Client(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):  # noqa: ANN001
                got.put_nowait(data)

        client, _ = await loop.create_datagram_endpoint(Client, local_addr=("127.0.0.3", 0))
        try:
            client.sendto(b"garbage", ("127.0.0.2", udp_port))
            client.sendto(QUERY, ("127.0.0.2", udp_port))
            first = await asyncio.wait_for(got.get(), 3)
            extra = False
            try:
                await asyncio.wait_for(got.get(), 0.3)
                extra = True
            except asyncio.TimeoutError:
                pass
            return json.loads(first), extra
        finally:
            client.close()
            transport.close()

    body, extra = asyncio.run(run())
    assert body["q"] == discovery.MAGIC_A and body["urls"] == ["http://127.0.0.2:18787"] and not extra
    assert len(body["fingerprint"]) == 24


def test_link_screen_shows_lan_urls(admin_client, monkeypatch):
    """「つなぐ」の画面（GET /link）に、LAN の URL の候補と発見の応答の状態が出る。127.0.0.1 待ち受けなら候補は空。"""
    admin = admin_client
    assert admin.get("/api/v1/link").json()["lan"]["urls"] == []
    monkeypatch.setenv("NEWSHUB_HOST", "0.0.0.0")
    monkeypatch.setenv("NEWSHUB_PORT", "8787")
    monkeypatch.setattr(discovery, "interface_addresses", lambda: ["192.0.2.10"])
    lan = admin.get("/api/v1/link").json()["lan"]
    assert lan["urls"] == ["http://192.0.2.10:8787"] and lan["answering"] is True and lan["discovery_port"] == 8788
