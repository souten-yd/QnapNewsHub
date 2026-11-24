"""秘密（OpenRouter の鍵など）の暗号化した置き場（data_dir/secrets.json）。

- 暗号化の鍵は data_dir/secret.key（32 バイト・0600・最初の使用時に作る）。AES-256-GCM（cryptography）。名前を認証付きの追加データに入れるので、
  別の名前の枠へ貼り替えても開けない。
- 平文はファイルにもログにも応答にも出さない。外へ返すのは「入っているか」だけ（has）。
- 鍵ファイルごとデータの置き場が漏れたら破られる（置き場の保護は OS の権限）。鍵を別の場所に分けるのは QPKG の段で検討する。
"""
from __future__ import annotations

import base64
import json
import os
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from qnapnewshub.config import data_dir

_lock = threading.Lock()


class SecretError(RuntimeError):
    pass


def _write_private(path, data: bytes) -> None:
    from qnapnewshub import writer

    writer.write_file(path, data, name="secretbox.write")


def _key() -> bytes:
    path = data_dir() / "secret.key"
    try:
        raw = path.read_bytes()
        if len(raw) == 32:
            return raw
        raise SecretError("暗号化の鍵のファイルが壊れています（secret.key）")
    except FileNotFoundError:
        raw = os.urandom(32)
        _write_private(path, raw)
        return raw


def _load() -> dict:
    try:
        raw = json.loads((data_dir() / "secrets.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SecretError("秘密の置き場を読めません（secrets.json）") from exc
    return raw if isinstance(raw, dict) else {}


def set_secret(name: str, value: str) -> None:
    value = str(value)
    with _lock:
        nonce = os.urandom(12)
        sealed = AESGCM(_key()).encrypt(nonce, value.encode("utf-8"), name.encode("utf-8"))
        table = _load()
        table[name] = {"v": 1, "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(sealed).decode()}
        _write_private(data_dir() / "secrets.json", json.dumps(table).encode("utf-8"))


def get_secret(name: str) -> str | None:
    with _lock:
        row = _load().get(name)
        if not isinstance(row, dict):
            return None
        try:
            plain = AESGCM(_key()).decrypt(base64.b64decode(row["n"]), base64.b64decode(row["c"]), name.encode("utf-8"))
        except (InvalidTag, KeyError, ValueError) as exc:
            raise SecretError(f"秘密 {name} を開けません（鍵が違う・壊れています）") from exc
        return plain.decode("utf-8")


def has_secret(name: str) -> bool:
    with _lock:
        return isinstance(_load().get(name), dict)


def delete_secret(name: str) -> bool:
    with _lock:
        table = _load()
        if name not in table:
            return False
        del table[name]
        _write_private(data_dir() / "secrets.json", json.dumps(table).encode("utf-8"))
        return True
