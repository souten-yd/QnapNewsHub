"""ニュースハブ本体の設定（data_dir/hub.json。待ち受けの host・port、cookie の secure）。変えたら再起動で効く。

既定は 127.0.0.1（この PC だけ）。LAN に出すときだけ利用者が 0.0.0.0 にする。discovery_port は LAN の発見の応答（UDP。0 で切。discovery.py）（外へ出すなら QNAP の逆プロキシと TLS を使う。設計 2.3）。
"""
from __future__ import annotations

import json
import os
import threading

from qnapnewshub import writer
from qnapnewshub.config import DEFAULT_HOST, DEFAULT_PORT, data_dir
from qnapnewshub.sources import ConfigError

HOSTS = ("127.0.0.1", "0.0.0.0")
_lock = threading.Lock()
DEFAULTS = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "cookie_secure": False, "discovery_port": 8788}


def _path():
    return data_dir() / "hub.json"


def normalize(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("設定の形が違います")
    out = dict(DEFAULTS)
    host = str(raw.get("host", out["host"]))
    if host not in HOSTS:
        raise ConfigError("待ち受けは 127.0.0.1（この PC だけ）か 0.0.0.0（LAN に出す）です")
    out["host"] = host
    try:
        port = int(raw.get("port", out["port"]))
    except (TypeError, ValueError):
        raise ConfigError("ポートは整数です") from None
    if not 1024 <= port <= 65535:
        raise ConfigError("ポートは 1024〜65535 です")
    out["port"] = port
    try:
        udp = int(raw.get("discovery_port", out["discovery_port"]))
    except (TypeError, ValueError):
        raise ConfigError("発見のポートは整数です") from None
    if udp != 0 and not 1024 <= udp <= 65535:
        raise ConfigError("発見のポートは 0（切）か 1024〜65535 です")
    out["discovery_port"] = udp
    out["cookie_secure"] = bool(raw.get("cookie_secure", out["cookie_secure"]))
    return out


def load() -> dict:
    try:
        return normalize(json.loads(_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(raw: object) -> dict:
    cfg = normalize(raw)
    with _lock:
        writer.write_file(_path(), json.dumps(cfg, indent=1), name="hubconf.save")
    return cfg
