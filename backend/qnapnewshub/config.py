"""ニュースハブの設定の入口（データの置き場・待ち受け）。

- データの置き場は環境変数 NEWSHUB_DATA（既定 ~/.local/share/qnapnewshub）。呼ぶたびに読む（試験が差し替えられる）。
- 置き場の外へは書かない: パスは resolve() で正規化し、置き場の中かを確かめる（inside）。
- 秘密（OpenRouter の鍵・暗号化の鍵）は置き場の中のファイル（0600）に置く。環境変数・ログには出さない。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def data_dir() -> Path:
    raw = os.environ.get("NEWSHUB_DATA", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".local" / "share" / "qnapnewshub"
    base = base.resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def inside(path: Path) -> Path:
    """path を resolve() して、データの置き場の中なら返す。外なら ValueError。"""
    root = data_dir()
    resolved = Path(path).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("データの置き場の外は使えません")
    return resolved


def listen() -> tuple[str, int]:
    """待ち受けの host・port。環境変数 NEWSHUB_HOST・NEWSHUB_PORT、無ければ hub.json（設定の API で変える）、無ければ 127.0.0.1:8787。"""
    from qnapnewshub import hubconf

    conf = hubconf.load()
    host = os.environ.get("NEWSHUB_HOST") or conf["host"]
    port = int(os.environ.get("NEWSHUB_PORT") or conf["port"])
    return host, port
