#!/usr/bin/env python3
"""Release の .qpkg に署名する（Ed25519）。

使い方: sign.py <qpkg> [--out-dir DIR]
  <qpkg>.manifest.json  {"name","version","sha256","size"}（キーを並べ替えた 1 行の JSON）
  <qpkg>.manifest.sig   manifest.json のバイト列そのものへの署名（base64）
秘密鍵は環境変数 NEWSHUB_SIGNING_KEY（既定 ~/.keys/qnapnewshub-release.pem。PEM・暗号化なし）から**読むだけ**。
中身を出力・コピーしない。署名のあと、リポジトリの公開鍵（backend/qnapnewshub/release_key.py）で検証し、合わなければ失敗にする
（鍵の取り違えのまま Release しない）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

NAME = "QnapNewsHub"
_FILE = re.compile(r"^QnapNewsHub_(?P<ver>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)_x86_64\.qpkg$")
DEFAULT_KEY = "~/.keys/qnapnewshub-release.pem"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(qpkg: Path) -> bytes:
    match = _FILE.match(qpkg.name)
    if not match:
        raise ValueError(f"ファイル名が QnapNewsHub_<版>_x86_64.qpkg の形ではありません: {qpkg.name}")
    manifest = {"name": NAME, "version": match["ver"], "sha256": sha256_file(qpkg), "size": qpkg.stat().st_size}
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Ed25519 の秘密鍵ではありません")
    return key


def sign_bytes(key: Ed25519PrivateKey, data: bytes) -> str:
    return base64.b64encode(key.sign(data)).decode("ascii")


def verify(public_b64: str, data: bytes, sig_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64, validate=True)).verify(base64.b64decode(sig_b64, validate=True), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def sign_qpkg(qpkg: Path, key: Ed25519PrivateKey, out_dir: Path | None = None, public_b64: str | None = None) -> tuple[Path, Path]:
    """manifest と署名を書いて (manifest のパス, 署名のパス) を返す。public_b64 があれば、書く前にその公開鍵で検証する。"""
    out_dir = out_dir or qpkg.parent
    data = build_manifest(qpkg)
    sig = sign_bytes(key, data)
    if public_b64 is not None and not verify(public_b64, data, sig):
        raise ValueError("この秘密鍵は、リポジトリの公開鍵（release_key.py）と対になっていません。鍵を取り違えていないか確かめてください")
    manifest_path, sig_path = out_dir / (qpkg.name + ".manifest.json"), out_dir / (qpkg.name + ".manifest.sig")
    manifest_path.write_bytes(data)
    sig_path.write_text(sig + "\n", encoding="ascii")
    return manifest_path, sig_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("qpkg", type=Path)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    key_path = Path(os.environ.get("NEWSHUB_SIGNING_KEY") or DEFAULT_KEY).expanduser()
    if not key_path.is_file():
        print(f"署名の秘密鍵が見つかりません: {key_path.name}（NEWSHUB_SIGNING_KEY で場所を指定）", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    from qnapnewshub import release_key

    try:
        manifest, sig = sign_qpkg(args.qpkg.resolve(), load_private_key(key_path), args.out_dir, release_key.PUBLIC_KEY_B64)
    except (ValueError, OSError) as exc:
        print(f"署名できませんでした: {exc}", file=sys.stderr)
        return 1
    print(f"署名しました: {manifest.name} / {sig.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
