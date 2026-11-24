"""認証: 利用者（管理者）・cookie の session（ログイン）・ControlDeck 用の API トークン。

- 最初の起動では利用者がいない。POST /api/v1/auth/setup（または ./hub.sh create-admin）で最初の管理者を作る（利用者がいる間は使えない）。
- パスワードは scrypt（標準ライブラリ。1 人ごとの塩）で保存。session の印・API トークンは **ハッシュだけ** を保存する（本体は作ったときに 1 度だけ見せる）。
- API ごとの認証: current_user は session の cookie か「Authorization: Bearer <トークン>」。トークンは **読み取り（GET・HEAD）だけ**。
  書き込み・設定・トークンの管理は require_admin（session だけ）。cookie での書き込みは Origin が同じホストのときだけ（CSRF）。
- ログインの失敗は IP ごとに数え、5 分に 8 回で 429（総当たりの対策）。応答は「利用者名かパスワードが違います」だけ（どちらが違うかは言わない）。
- 保存先: data_dir/auth.sqlite3（記事の DB とは別のファイル）。同期の sqlite3 なので async の中では asyncio.to_thread で呼ぶ。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response

from qnapnewshub import writer
from qnapnewshub.config import data_dir

logger = logging.getLogger("qnapnewshub.auth")

COOKIE = "newshub_session"
SESSION_DAYS = 30
TOKEN_PREFIX = "nhub_"
MIN_PASSWORD = 12
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
LOGIN_WINDOW = 300.0
LOGIN_MAX_FAILURES = 8
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,40}$")
_SAFE = ("GET", "HEAD", "OPTIONS")

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
# sqlite3 の接続は 1 本を複数のスレッドで同時に使えない（2026-11-24 E2E で、同時の GET が InterfaceError の 500 になった）。スレッドごとに持つ
_local = threading.local()


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    via: str          # "session" か "token"
    scope: str = ""   # トークンの権限: read（読み取りだけ）・link（つないだ ControlDeck: 読み取り＋仕事を返す＋生成物を送る。管理はできない）
    token_id: int = 0
    sens: str = "public"      # トークンに許した機密の区分（カンマ区切り。既定 public だけ。link ではつないだときに設定で明示した場合だけ confidential を足す）。session は全部


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _db() -> sqlite3.Connection:
    path = data_dir() / "auth.sqlite3"
    key = str(path)
    conns: dict[str, sqlite3.Connection] = _local.__dict__.setdefault("conns", {})
    with _lock:
        conn = conns.get(key)
        if conn is None or not path.exists():
            if conn is not None:
                conn.close()
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            conn = sqlite3.connect(path, timeout=10, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")      # checkpoint は Write Coordinator が行う
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  hint TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS link_peers (id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES api_tokens(id) ON DELETE CASCADE, name TEXT NOT NULL,
  public_key TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS link_secrets (name TEXT PRIMARY KEY, peer_id INTEGER NOT NULL, received_at TEXT NOT NULL);
""")
            if "sens" not in {r[1] for r in conn.execute("PRAGMA table_info(api_tokens)")}:      # 許した機密の区分（v0.6）。前の版の表にも足す（既定は public だけ＝fail-closed）
                conn.execute("ALTER TABLE api_tokens ADD COLUMN sens TEXT NOT NULL DEFAULT 'public'")
            if "scope" not in {r[1] for r in conn.execute("PRAGMA table_info(api_tokens)")}:      # 権限の区分（read: 読み取りだけ・link: つないだ ControlDeck）。前の版の表にも足す
                conn.execute("ALTER TABLE api_tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'read'")
            conns[key] = conn
        return conn


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---- パスワード ----

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def check_new_credentials(username: str, password: str) -> None:
    if not _USERNAME.match(username or ""):
        raise ValueError("利用者名は 3〜40 文字の英数字と . _ - です")
    if len(password or "") < MIN_PASSWORD or len(password) > 200:
        raise ValueError(f"パスワードは {MIN_PASSWORD} 文字以上です")


# ---- 利用者 ----

def has_users() -> bool:
    return _db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


# ---- 最初の管理者の一回限りの番号（QPKG など LAN に出すとき） ----
# 2026-11-24: QPKG は既定で LAN（0.0.0.0）に待ち受ける。利用者がいない間は誰でも setup できてしまうので、
# NEWSHUB_SETUP_TOKEN=1 のときは、データの置き場の setup-token.txt（0600）にある番号を知っている人だけが最初の管理者を作れる。
# QPKG の起動台本は、この番号を QTS のシステムのログ（管理者だけが見られる）にも出す。

SETUP_TOKEN_FILE = "setup-token.txt"


def setup_token_required() -> bool:
    return os.environ.get("NEWSHUB_SETUP_TOKEN", "") == "1"


def setup_token_path() -> Path:
    return data_dir() / SETUP_TOKEN_FILE


def ensure_setup_token() -> str | None:
    """番号が要る設定で、まだ利用者がいなければ番号を用意する（あれば読む）。要らなければ None。"""
    if not setup_token_required() or has_users():
        return None
    path = setup_token_path()
    if path.is_file():
        old = path.read_text(encoding="utf-8").strip()
        if old:
            return old
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # 読み違えやすい 0/O・1/I を除く
    token = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
    writer.write_file(path, token + "\n", name="auth.setup_token", mode=0o600)
    return token


def check_setup_token(given: str | None) -> None:
    """番号が要る設定なら照合する。合わなければ PermissionError。"""
    if not setup_token_required():
        return
    path = setup_token_path()
    want = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    norm = (given or "").strip().upper().replace(" ", "")
    if not want or not hmac.compare_digest(norm.encode(), want.encode()):
        raise PermissionError("最初の管理者を作る番号が違います")


def clear_setup_token() -> None:
    path = setup_token_path()
    if path.exists():
        writer.run("auth.setup_token_clear", lambda: path.unlink(missing_ok=True))


def create_admin(username: str, password: str) -> int:
    """最初の管理者を作る。すでに利用者がいれば PermissionError。"""
    check_new_credentials(username, password)
    pw = hash_password(password)

    def do() -> int:
        conn = _db()
        with _lock:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise PermissionError("管理者はすでにいます")
            cur = conn.execute("INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)", (username, pw, _iso(_now())))
            return int(cur.lastrowid)
    return writer.run("auth.create_admin", do)


def create_user(username: str, password: str) -> int:
    """利用者を足す（管理者。いまは API に出さない: 最初の管理者は create_admin。複数の利用者の管理画面は後の段）。"""
    check_new_credentials(username, password)
    pw = hash_password(password)
    return writer.run("auth.create_user", lambda: int(_db().execute("INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)", (username, pw, _iso(_now()))).lastrowid))


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    with _lock:
        recent = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
        _failures[ip] = recent
        return len(recent) >= LOGIN_MAX_FAILURES


def _fail(ip: str) -> None:
    with _lock:
        _failures.setdefault(ip, []).append(time.monotonic())


def login(username: str, password: str, ip: str) -> tuple[str, AuthUser]:
    """成功なら (session の印, 利用者)。失敗は PermissionError（理由は 1 種類）、総当たりは RuntimeError。"""
    if _throttled(ip):
        raise RuntimeError("ログインの失敗が続いたため、しばらく待ってください")
    row = _db().execute("SELECT id, username, pw_hash, active FROM users WHERE username = ?", (str(username)[:80],)).fetchone()
    ok = verify_password(str(password)[:300], row[2] if row else hash_password("dummy"))      # 利用者がいなくても同じ時間をかける
    if not row or not ok or not row[3]:
        _fail(ip)
        raise PermissionError("利用者名かパスワードが違います")
    token = secrets.token_urlsafe(32)
    now = _now()
    def save() -> None:
        _db().execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (_sha(token), row[0], _iso(now), _iso(now + timedelta(days=SESSION_DAYS))))
        _db().execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
    writer.run("auth.login", save)
    return token, AuthUser(row[0], row[1], "session")


def logout(token: str) -> None:
    writer.run("auth.logout", lambda: _db().execute("DELETE FROM sessions WHERE token_hash = ?", (_sha(token),)))


def session_user(token: str) -> AuthUser | None:
    row = _db().execute("SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
                        (_sha(token), _iso(_now()))).fetchone()
    return AuthUser(row[0], row[1], "session") if row else None


# ---- API トークン（ControlDeck 用。読み取りだけ） ----

SCOPES = ("read", "link")


def create_token(user_id: int, name: str, scope: str = "read", sens: str = "public") -> dict:
    """トークンを作る。本体（"token"）は戻り値でだけ見える（保存はハッシュ）。scope は read（既定）か link（link.py のつなぐ操作だけが作る）。
    sens は許す機密の区分（public・public,confidential）。read のトークンは常に public だけ（confidential を許せるのは、つないだ link だけ）。"""
    if scope not in SCOPES:
        raise ValueError("トークンの権限が正しくありません")
    sens = "public,confidential" if scope == "link" and sens == "public,confidential" else "public"
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise ValueError("トークンの名前を入れてください")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    cur = writer.run("auth.create_token", lambda: _db().execute("INSERT INTO api_tokens (user_id, name, token_hash, hint, created_at, scope, sens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                                                 (user_id, name, _sha(token), token[:9] + "…", _iso(_now()), scope, sens)))
    return {"id": int(cur.lastrowid), "name": name, "token": token, "hint": token[:9] + "…", "scope": scope, "sens": sens}


def list_tokens() -> list[dict]:
    rows = _db().execute("SELECT id, name, hint, created_at, last_used_at, revoked_at FROM api_tokens WHERE scope = 'read' ORDER BY id DESC").fetchall()      # つないだ ControlDeck は link.py の一覧
    staged = writer.get().buf.token_used      # まだ書いていない最終使用の時刻があれば、それが新しい
    return [{"id": r[0], "name": r[1], "hint": r[2], "created_at": r[3], "last_used_at": staged.get(r[0]) or r[4] or "", "revoked": bool(r[5])} for r in rows]


def revoke_token(token_id: int) -> bool:
    cur = writer.run("auth.revoke_token", lambda: _db().execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL AND scope = 'read'", (_iso(_now()), token_id)))      # link は link.revoke_peer
    return cur.rowcount > 0


def token_user(token: str) -> AuthUser | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    conn = _db()
    row = conn.execute("SELECT t.id, u.id, u.username, t.scope, t.sens FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.revoked_at IS NULL AND u.active = 1",
                       (_sha(token),)).fetchone()
    if not row:
        return None
    writer.get().stage_token_used(row[0], _iso(_now()))      # 最終使用の時刻は、まとめ書きのときに（API を呼ぶたびに書かない）
    return AuthUser(row[1], row[2], "token", row[3], row[0], row[4] or "public")


def apply_token_used(rows: dict[int, str]) -> None:
    """まとめ書きから（書き手のスレッド）: トークンの最終使用の時刻。"""
    conn = _db()
    for token_id, at in rows.items():
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (at, token_id))


def checkpoint() -> None:
    """認証 DB の WAL を戻す（書き手のスレッドで）。ファイルが無ければ何もしない。"""
    if (data_dir() / "auth.sqlite3-wal").exists():
        _db().execute("PRAGMA wal_checkpoint(PASSIVE)")


# ---- FastAPI の依存 ----

def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True      # ブラウザ以外（curl など）は Origin を付けない。cookie を使うブラウザは書き込みで必ず付ける
    return urlsplit(origin).netloc == request.headers.get("host", "")


def _bearer(request: Request) -> str:
    head = request.headers.get("authorization", "")
    return head[7:].strip() if head.lower().startswith("bearer ") else ""


async def current_user(request: Request) -> AuthUser:
    """session か Bearer トークン。トークンは読み取り（GET・HEAD）だけ。"""
    import asyncio

    token = _bearer(request)
    if token:
        user = await asyncio.to_thread(token_user, token)
        if user is None:
            raise HTTPException(status_code=401, detail="トークンが正しくありません")
        if request.method not in _SAFE:
            raise HTTPException(status_code=403, detail="API トークンは読み取り専用です")
        return user
    cookie = request.cookies.get(COOKIE, "")
    user = await asyncio.to_thread(session_user, cookie) if cookie else None
    if user is None:
        raise HTTPException(status_code=401, detail="ログインしてください")
    if request.method not in _SAFE and not _same_origin(request):
        raise HTTPException(status_code=403, detail="別のサイトからの操作は受け付けません")
    return user


async def read_user(request: Request) -> AuthUser:
    """読むだけの API（POST でも副作用が無いもの。検索の道具: /retrieve/*・/graph/*）。session か Bearer トークン（read・link）。
    current_user との違いは、トークンの POST を通すこと（読むだけの POST。書き込みは通さない＝この依存を付けるのは副作用の無い API だけ）。"""
    import asyncio

    token = _bearer(request)
    if token:
        user = await asyncio.to_thread(token_user, token)
        if user is None:
            raise HTTPException(status_code=401, detail="トークンが正しくありません")
        return user
    cookie = request.cookies.get(COOKIE, "")
    user = await asyncio.to_thread(session_user, cookie) if cookie else None
    if user is None:
        raise HTTPException(status_code=401, detail="ログインしてください")
    if request.method not in _SAFE and not _same_origin(request):
        raise HTTPException(status_code=403, detail="別のサイトからの操作は受け付けません")
    return user


def allowed_sensitivities(user: AuthUser) -> frozenset[str]:
    """この利用者が読んでよい機密の区分。session（管理者）は全部、トークンは作ったときに許した範囲だけ（既定 public）。"""
    if user.via == "session":
        return frozenset({"public", "confidential"})
    return frozenset(x for x in (user.sens or "public").split(",") if x in ("public", "confidential")) or frozenset({"public"})


async def link_user(request: Request) -> AuthUser:
    """つないだ ControlDeck（scope=link のトークン）だけ。仕事を借りる・返す・生成物を送る・鍵を渡すなど、**この依存を付けた API だけ**が書き込める。
    session・読み取りだけのトークンは 403。取り消したトークンは 401。管理の API（require_admin）には、この権限では入れない。"""
    import asyncio

    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="トークンが要ります")
    user = await asyncio.to_thread(token_user, token)
    if user is None:
        raise HTTPException(status_code=401, detail="トークンが正しくありません（取り消されたかもしれません）")
    if user.scope != "link":
        raise HTTPException(status_code=403, detail="つないだ ControlDeck のトークンではありません")
    return user


async def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    """設定・書き込み・トークンの管理。session のログインだけ（API トークンは通さない）。利用者はすべて管理者。"""
    if user.via != "session":
        raise HTTPException(status_code=403, detail="この操作はログインした管理者だけです")
    return user


def set_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")
