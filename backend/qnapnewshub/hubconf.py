"""ニュースハブ本体の設定（data_dir/hub.json。待ち受けの host・port、cookie の secure）。変えたら再起動で効く。

既定は 127.0.0.1（この PC だけ）。LAN に出すときだけ利用者が 0.0.0.0 にする（外へ出すなら QNAP の逆プロキシと TLS を使う。設計 2.3）。
"""
from __future__ import annotations

import json
import os
import threading

from qnapnewshub.config import DEFAULT_HOST, DEFAULT_PORT, data_dir
from qnapnewshub.sources import ConfigError

HOSTS = ("127.0.0.1", "0.0.0.0")
_lock = threading.Lock()
DEFAULTS = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "cookie_secure": False}


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
        tmp = _path().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=1)
        os.replace(tmp, _path())
    return cfg
