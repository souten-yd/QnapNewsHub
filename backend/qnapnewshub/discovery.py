"""LAN での発見の応答（UDP）。ControlDeck の「探す」が、ハブの URL を入れなくても見つけられるようにする。DESIGN 3.5。

2026-11-25 利用者「公開されている場合自動でアドレスを検知できないか？」（ControlDeck の設定でハブの URL を間違えてつながらなかった）。

- **答えるのは、LAN に出しているときだけ**: 待ち受けが 127.0.0.1 なら起こさない（LAN には何も出ない）。0.0.0.0 か 127.0.0.1 以外のアドレスなら、UDP の既定 8788 で待つ。
  ポートは hub.json の discovery_port か NEWSHUB_DISCOVERY_PORT で変える。0（か off）で切。
- **問いの形**: UTF-8 の JSON `{"q": "qnapnewshub-discover", "v": 1}`（256 バイトまで。ほかの鍵は無視）。形が違えば**何も返さない**（ポートスキャンに「いる」と教えない）。
- **答え**: `{"q":"qnapnewshub-here","v":1,"name","version","urls":[…],"fingerprint"}`。urls は自分の LAN のアドレス × 待ち受けのポート（0.0.0.0 なら各インターフェース。
  問いの送り元へ向かう道のアドレスを先頭に）。fingerprint は link の X25519 公開鍵の指紋（公開してよい）。**秘密・トークン・番号・ユーザー名は一切入れない**。
  ControlDeck は答えの URL のうち、送り元の IP と一致するものだけを採る（なりすまし・SSRF の誘導を防ぐ）。発見は情報だけで、つなぐには番号と指紋の見比べが要る。
- **悪用の防止**: 送り元の IP を偽った UDP を使った増幅攻撃を防ぐため、答えは MAX_REPLY（512 バイト）まで（urls は 6 個まで）、全体で 1 秒に 5 通・同じ送り元から 1 秒に 1 通まで。超えた問いは黙って捨てる。
- 書かない（Write Coordinator は不要）。asyncio の DatagramProtocol を lifespan に載せる。待ち受けを張れなかったら警告だけ出して続ける（API は動く）。
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import socket
import struct
import time

from qnapnewshub import __version__, config

logger = logging.getLogger("qnapnewshub.discovery")

DEFAULT_PORT = 8788
MAGIC_Q = "qnapnewshub-discover"
MAGIC_A = "qnapnewshub-here"
PROTOCOL = 1
MAX_QUERY = 256
MAX_REPLY = 512
MAX_URLS = 6
GLOBAL_PER_SEC = 5
PER_SOURCE_PER_SEC = 1
NAME = "QNAP News Hub"
_SIOCGIFADDR = 0x8915


def configured_port() -> int:
    """発見の応答のポート。0 は切。NEWSHUB_DISCOVERY_PORT（数字か off）が hub.json より先。"""
    from qnapnewshub import hubconf

    raw = os.environ.get("NEWSHUB_DISCOVERY_PORT", "").strip().lower()
    if raw in ("off", "0"):
        return 0
    if raw.isdigit() and 1024 <= int(raw) <= 65535:
        return int(raw)
    return int(hubconf.load()["discovery_port"])


def is_lan_host(host: str) -> bool:
    return host not in ("127.0.0.1", "localhost", "::1", "")


def parse_query(data: bytes) -> bool:
    """決まった形の問いか。形が違えば False（返事をしない）。"""
    if not 0 < len(data) <= MAX_QUERY:
        return False
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(body, dict) and body.get("q") == MAGIC_Q and body.get("v") == PROTOCOL


def interface_addresses() -> list[str]:
    """この機器の IPv4 アドレス（ループバック・リンクローカルを除く）。Linux の ioctl（依存を足さない）。取れなければ空。"""
    found: list[str] = []
    try:
        names = [name for _, name in socket.if_nameindex()]
    except OSError:
        return found
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for name in names:
            try:
                packed = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
            except OSError:
                continue
            addr = socket.inet_ntoa(packed[20:24])
            if addr.startswith(("127.", "169.254.", "0.")) or addr in found:
                continue
            found.append(addr)
    return found


def route_address(peer: str) -> str:
    """peer へ向かうときの自分のアドレス（何も送らない。UDP の connect は道を決めるだけ）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((peer, 9))
            return sock.getsockname()[0]
    except OSError:
        return ""


def candidate_addresses(host: str, peer: str = "") -> list[str]:
    if host not in ("0.0.0.0", ""):
        return [host] if is_lan_host(host) else []
    addrs = interface_addresses()
    first = route_address(peer) if peer else ""
    if first and not first.startswith("127.") and first in addrs:
        addrs.remove(first)
        addrs.insert(0, first)
    return addrs


def candidate_urls(host: str | None = None, port: int | None = None, peer: str = "") -> list[str]:
    """LAN の URL の候補（http://<アドレス>:<ポート>）。127.0.0.1 待ち受けなら空。"""
    if host is None or port is None:
        host, port = config.listen()
    return [f"http://{a}:{port}" for a in candidate_addresses(host, peer)][:MAX_URLS]


def build_reply(urls: list[str], fingerprint: str) -> bytes:
    body = {"q": MAGIC_A, "v": PROTOCOL, "name": NAME, "version": __version__, "urls": urls[:MAX_URLS], "fingerprint": fingerprint}
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    while len(data) > MAX_REPLY and body["urls"]:
        body["urls"].pop()
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return data


class Limiter:
    """1 秒の窓での上限（全体と送り元ごと）。時計は差し替えられる（試験）。"""

    def __init__(self, clock=time.monotonic) -> None:  # noqa: ANN001
        self.clock = clock
        self.window = 0
        self.total = 0
        self.per: dict[str, int] = {}

    def allow(self, source: str) -> bool:
        second = int(self.clock())
        if second != self.window:
            self.window, self.total, self.per = second, 0, {}
        if self.total >= GLOBAL_PER_SEC or self.per.get(source, 0) >= PER_SOURCE_PER_SEC:
            return False
        self.total += 1
        self.per[source] = self.per.get(source, 0) + 1
        return True


class Responder(asyncio.DatagramProtocol):
    def __init__(self, host: str, port: int, fingerprint_fn, clock=time.monotonic) -> None:  # noqa: ANN001
        self.host, self.port = host, port
        self.fingerprint_fn = fingerprint_fn
        self.limiter = Limiter(clock)
        self.transport: asyncio.DatagramTransport | None = None
        self._fingerprint = ""

    def connection_made(self, transport) -> None:  # noqa: ANN001
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if self.transport is None or not parse_query(data) or not self.limiter.allow(addr[0]):
            return
        if not self._fingerprint:
            try:
                self._fingerprint = self.fingerprint_fn()
            except Exception:  # noqa: BLE001
                logger.warning("発見の応答: 指紋を取れませんでした（空で答えます）")
        urls = candidate_urls(self.host, self.port, addr[0])
        if urls:
            self.transport.sendto(build_reply(urls, self._fingerprint), addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug("発見の応答の送受信の失敗: %s", exc)


def _fingerprint() -> str:
    from qnapnewshub import link

    return link.hub_identity()["fingerprint"]


async def start() -> asyncio.DatagramTransport | None:
    """LAN に出しているときだけ待ち受けを張る。張らない・張れないときは None。"""
    host, port = config.listen()
    udp = configured_port()
    if not udp or not is_lan_host(host):
        return None
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(lambda: Responder(host, port, _fingerprint), local_addr=(host, udp), reuse_port=False)
    except OSError as exc:
        logger.warning("発見の応答の UDP %s を待てません（%s）。ほかのことは動きます", udp, exc.strerror or exc)
        return None
    logger.info("発見の応答を UDP %s で待ち受けます", udp)
    return transport


def status() -> dict:
    """「つなぐ」の画面用: 発見の応答の状態と LAN の URL の候補。"""
    host, port = config.listen()
    udp = configured_port()
    lan = is_lan_host(host)
    return {"lan": lan, "discovery_port": udp, "answering": bool(lan and udp), "urls": candidate_urls(host, port) if lan else []}
