"""ControlDeck とつなぐ（ペアリング）・鍵の封じた受け渡し・つないだ相手の管理。DESIGN 3.1・3.2。

- **つなぐ**: 管理者が画面で番号（6 桁・10 分で失効・1 回だけ）を出す → ControlDeck が POST /link/pair で番号・名前・自分の公開鍵（X25519）を送る →
  scope=link のトークン（読み取り＋仕事を返す＋生成物を送る。管理はできない）を受け取る。番号は **メモリだけ**に持つ（再起動で消える）。
  総当たりの対策: 同じ番号への間違いが 5 回で番号を無効にする・同じ IP は 10 分に 8 回間違えると 429。
- **ニュースハブの鍵**: X25519（secretbox に暗号化して保存。最初に要るとき作る）。指紋は公開鍵の SHA-256 の先頭 80 ビットを 4 文字ごとに区切ったもの。
  利用者が両方の画面で見比べる（途中のなりすましの確認）。
- **封じた鍵**（ECIES）: ControlDeck が使い捨ての X25519 鍵 e を作り、ECDH(e, ニュースハブの公開鍵) → HKDF-SHA256（salt = e の公開鍵 ‖ ニュースハブの公開鍵、info = "qnapnewshub-link-v1"）
  → AES-256-GCM（nonce 12 バイト、追加データ = "qnapnewshub-link-v1|<名前>"）。送る形: {"v":1, "epk": base64(e の公開鍵 32 バイト), "n": base64(nonce), "c": base64(暗号文 + タグ)}。
  受け取れる名前は ALLOWED_SECRETS だけ（いまは openrouter）。開けたら secretbox に保存（平文はログ・応答・画面に出さない）。名前と時刻と相手だけを残す。
- 読み書きは auth.py と同じ作り（auth.sqlite3・書き込みは Write Coordinator）。同期の関数なので async からは asyncio.to_thread で呼ぶ。
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from qnapnewshub import auth, llm, secretbox, writer

logger = logging.getLogger("qnapnewshub.link")

CODE_TTL = 600.0
CODE_MAX_WRONG = 5            # 同じ番号への間違いがこの回数で、その番号は無効
IP_WINDOW, IP_MAX_WRONG = 600.0, 8
KEY_NAME = "link_x25519"
INFO = b"qnapnewshub-link-v1"
ALLOWED_SECRETS = {"openrouter": llm.KEY_NAME}      # 受け取れる名前 → secretbox の名前
SECRET_MAX = 512

_lock = threading.Lock()
_pairing: dict | None = None
_ip_wrong: dict[str, list[float]] = {}


class LinkError(Exception):
    """利用者に見せてよい理由つきの失敗（status は HTTP の状態）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- ニュースハブの鍵 ----

def _private() -> X25519PrivateKey:
    with _lock:
        raw = secretbox.get_secret(KEY_NAME)
        if raw:
            return X25519PrivateKey.from_private_bytes(base64.b64decode(raw))
        key = X25519PrivateKey.generate()
        secretbox.set_secret(KEY_NAME, base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode())
        return key


def public_bytes(key: X25519PrivateKey | X25519PublicKey | None = None) -> bytes:
    key = key or _private()
    pub = key.public_key() if isinstance(key, X25519PrivateKey) else key
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(public: bytes) -> str:
    """公開鍵の指紋（短い表示）: SHA-256 の先頭 80 ビットを 4 文字ごと。例 1a2b-3c4d-5e6f-7a8b-9c0d。"""
    digest = hashlib.sha256(public).hexdigest()[:20]
    return "-".join(digest[i:i + 4] for i in range(0, 20, 4))


def hub_identity() -> dict:
    pub = public_bytes()
    return {"public_key": base64.b64encode(pub).decode(), "fingerprint": fingerprint(pub)}


def _decode_pub(value: str) -> bytes:
    try:
        raw = base64.b64decode(str(value), validate=True)
    except ValueError:
        raise LinkError(422, "公開鍵の形が正しくありません") from None
    if len(raw) != 32:
        raise LinkError(422, "公開鍵は X25519 の 32 バイトです")
    return raw


# ---- つなぐ番号 ----

def start_pairing(user_id: int) -> dict:
    """新しい番号を出す（前の番号は無効）。"""
    global _pairing  # noqa: PLW0603
    code = f"{secrets.randbelow(10 ** 6):06d}"
    with _lock:
        _pairing = {"hash": hashlib.sha256(code.encode()).hexdigest(), "until": time.monotonic() + CODE_TTL, "wrong": 0, "user_id": user_id,
                    "expires_at": datetime.fromtimestamp(time.time() + CODE_TTL, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        expires_at = _pairing["expires_at"]
    return {"code": code, "expires_at": expires_at, "expires_in": int(CODE_TTL), **hub_identity()}


def cancel_pairing() -> None:
    global _pairing  # noqa: PLW0603
    with _lock:
        _pairing = None


def pairing_state() -> dict | None:
    with _lock:
        if _pairing is None or time.monotonic() >= _pairing["until"]:
            return None
        return {"expires_at": _pairing["expires_at"], "wrong": _pairing["wrong"]}


def _ip_blocked(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _ip_wrong.get(ip, []) if now - t < IP_WINDOW]
    _ip_wrong[ip] = recent
    return len(recent) >= IP_MAX_WRONG


def pair(code: str, name: str, public_key: str, ip: str) -> dict:
    """番号が合えばトークン（scope=link）を発行する。合わない・失効・使用済み・多すぎる試行は LinkError。"""
    global _pairing  # noqa: PLW0603
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise LinkError(422, "ControlDeck の名前を入れてください")
    pub = _decode_pub(public_key)
    with _lock:
        if _ip_blocked(ip):
            raise LinkError(429, "番号の間違いが続いたため、しばらく待ってください")
        cur = _pairing
        ok = False
        if cur is not None and time.monotonic() < cur["until"] and cur["wrong"] < CODE_MAX_WRONG:
            ok = hmac.compare_digest(hashlib.sha256(str(code).strip().encode()).hexdigest(), cur["hash"])
            if not ok:
                cur["wrong"] += 1
        if not ok:
            _ip_wrong.setdefault(ip, []).append(time.monotonic())
            raise LinkError(403, "番号が違うか、期限が切れています。ニュースハブの画面で新しい番号を出してください")
        _pairing = None      # 1 回だけ使える
        user_id = cur["user_id"]
    token = auth.create_token(user_id, f"ControlDeck: {name}", scope="link")
    fp = fingerprint(pub)

    def save() -> int:
        return int(auth._db().execute("INSERT INTO link_peers (token_id, name, public_key, fingerprint, created_at) VALUES (?, ?, ?, ?, ?)",  # noqa: SLF001
                                      (token["id"], name, base64.b64encode(pub).decode(), fp, _now())).lastrowid)
    peer_id = writer.run("auth.link_peer", save)
    logger.info("ControlDeck とつなぎました: 相手 #%s", peer_id)
    return {"token": token["token"], "peer_id": peer_id, **hub_identity()}


# ---- つないだ相手 ----

def list_peers() -> list[dict]:
    rows = auth._db().execute("SELECT p.id, p.name, p.fingerprint, p.created_at, t.last_used_at, t.revoked_at, t.id FROM link_peers p JOIN api_tokens t ON t.id = p.token_id ORDER BY p.id DESC").fetchall()  # noqa: SLF001
    staged = writer.get().buf.token_used
    return [{"id": r[0], "name": r[1], "fingerprint": r[2], "created_at": r[3], "last_seen_at": staged.get(r[6]) or r[4] or "", "revoked": bool(r[5])} for r in rows]


def revoke_peer(peer_id: int) -> bool:
    row = auth._db().execute("SELECT token_id FROM link_peers WHERE id = ?", (peer_id,)).fetchone()  # noqa: SLF001
    if row is None:
        return False
    cur = writer.run("auth.link_revoke", lambda: auth._db().execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_now(), row[0])))  # noqa: SLF001
    return cur.rowcount > 0


def peer_of_token(token_id: int) -> int:
    row = auth._db().execute("SELECT id FROM link_peers WHERE token_id = ?", (token_id,)).fetchone()  # noqa: SLF001
    return int(row[0]) if row else 0


def list_secrets() -> list[dict]:
    """受け取った鍵の名前と時刻（値は出さない）。"""
    rows = auth._db().execute("SELECT s.name, s.received_at, s.peer_id, p.name FROM link_secrets s LEFT JOIN link_peers p ON p.id = s.peer_id ORDER BY s.name").fetchall()  # noqa: SLF001
    return [{"name": r[0], "received_at": r[1], "peer_id": r[2], "peer": r[3] or "", "present": secretbox.has_secret(ALLOWED_SECRETS.get(r[0], "?"))} for r in rows]


# ---- 封じた鍵（ECIES） ----

def _derive(shared: bytes, epk: bytes, hub_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + hub_pub, info=INFO).derive(shared)


def _aad(name: str) -> bytes:
    return INFO + b"|" + name.encode("utf-8")


def seal(hub_public: bytes, name: str, plaintext: str) -> dict:
    """ControlDeck 側の封じ方（試験の ControlDeck 役と、ControlDeck 側の実装の見本）。"""
    eph = X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _derive(eph.exchange(X25519PublicKey.from_public_bytes(hub_public)), epk, hub_public)
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _aad(name))
    return {"v": 1, "epk": base64.b64encode(epk).decode(), "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(sealed).decode()}


def open_sealed(name: str, box: object) -> str:
    """封じたものを開く。形が違う・改ざん・別の名前の枠への貼り替え・別の宛先は LinkError(422)。"""
    try:
        if not isinstance(box, dict) or box.get("v") != 1:
            raise ValueError
        epk, nonce, cipher = (base64.b64decode(str(box[k]), validate=True) for k in ("epk", "n", "c"))
        if len(epk) != 32 or len(nonce) != 12 or not 16 < len(cipher) <= SECRET_MAX + 16:
            raise ValueError
        priv = _private()
        key = _derive(priv.exchange(X25519PublicKey.from_public_bytes(epk)), epk, public_bytes(priv))
        return AESGCM(key).decrypt(nonce, cipher, _aad(name)).decode("utf-8")
    except (ValueError, KeyError, InvalidTag, TypeError):
        raise LinkError(422, "封じた鍵を開けませんでした（形が違う・改ざん・宛先違い）") from None


def receive_secret(peer_id: int, name: str, box: object) -> dict:
    if name not in ALLOWED_SECRETS:
        raise LinkError(422, "受け取れる鍵の名前ではありません")
    value = open_sealed(name, box).strip()
    if not value or len(value) > SECRET_MAX or any(c.isspace() or ord(c) < 32 for c in value):
        raise LinkError(422, "鍵の形が正しくありません")
    secretbox.set_secret(ALLOWED_SECRETS[name], value)
    writer.run("auth.link_secret", lambda: auth._db().execute("INSERT INTO link_secrets (name, peer_id, received_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET peer_id = excluded.peer_id, received_at = excluded.received_at",  # noqa: SLF001
                                                              (name, peer_id, _now())))
    logger.info("封じた鍵を受け取りました: %s（相手 #%s。値は記録しません）", name, peer_id)
    return {"ok": True, "name": name}


def summary() -> dict:
    return {"peers": list_peers(), "secrets": list_secrets(), "pairing": pairing_state()}


def reset_for_tests() -> None:
    global _pairing  # noqa: PLW0603
    with _lock:
        _pairing = None
        _ip_wrong.clear()

