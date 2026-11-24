"""更新の機能（docs/UPDATE.md）: 新しい Release の確認・署名つきの取得と検査・QPKG の入れ替え・前の版への戻し。

- 確認: GitHub の API（Release の一覧）を 1 日 1 回（と、手動の「今すぐ確認」）。結果はメモリだけ（ディスクへ書かない）。ETag で制限を節約する。
  試験版（prerelease）を含めるかは設定（既定は含めない。`update-settings.json`）。
- 取得: https だけ・github.com と GitHub の配布のホスト（objects / release-assets）だけ・大きさ 300MB まで・リダイレクトは各段を検査する。
- 検査: Release の `.manifest.json` を Ed25519 の署名（`release_key.PUBLIC_KEY_B64`）で確かめ、名前・版・大きさ・sha256 が .qpkg と合うときだけ入れる。
- 入れる: QPKG で動いているとき（NEWSHUB_INSTALL=qpkg）だけ。メモリの記事を書き切ってから、`updates/` に置いた .qpkg を、
  新しいセッションの子（start_new_session = setsid。サーバーが止まっても道連れにならない）で `sh <qpkg>` として流す。結果は `updates/last.json`。
- Linux（QPKG でない）では、新しい版があることと手順を出すだけ（自動では入れない）。
- 書き込みはすべて Write Coordinator（writer.run）を通す。お休みモードの間は入れない（明けてから）。
- 試験用の差し替え: NEWSHUB_INSTALL=qpkg-sim のときだけ、NEWSHUB_UPDATE_TEST_FEED（Release の一覧 releases.json と取得物を置いたディレクトリ）と
  NEWSHUB_UPDATE_TEST_PUBKEY（試験用の公開鍵）が効く。本番（qpkg）では無視する。検査の中身は同じ。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from qnapnewshub import __version__, egress, release_key, writer
from qnapnewshub.config import data_dir

logger = logging.getLogger("qnapnewshub.updater")

REPO = "souten-yd/QnapNewsHub"
API_URL = f"https://api.github.com/repos/{REPO}/releases"
API_HOSTS = frozenset({"api.github.com"})
ASSET_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
MAX_BYTES = 300 * 1024 * 1024          # .qpkg の大きさの上限
MAX_SMALL = 64 * 1024                  # manifest・署名の大きさの上限
MAX_LIST = 2 * 1024 * 1024             # Release の一覧の大きさの上限
MAX_REDIRECTS = 5
NOTES_MAX = 20000
CHECK_EVERY = 24 * 3600
FIRST_CHECK_DELAY = 120
PACKAGE = "QnapNewsHub"
INSTALL_OK_CODES = (0, 10)             # QDK の QPKG は成功で 10 を返す
UA = "QnapNewsHub-updater"

_VERSION = re.compile(r"^v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:-([0-9A-Za-z][0-9A-Za-z.-]{0,40}))?$")
_PLAIN_VERSION = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z][0-9A-Za-z.-]{0,40})?$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_QPKG_NAME = re.compile(r"^QnapNewsHub_(\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z][0-9A-Za-z.-]{0,40})?)_x86_64\.qpkg$")


class UpdateError(Exception):
    """利用者に見せてよい文で理由を言う（内部の情報・秘密は入れない）。"""


# ---- 版の比較 ----

def parse_version(text: str):
    """'0.1.1'・'v0.2.0-rc1' を (主・副・修正・試験版でないか・試験版の識別子) にする。読めなければ None。SemVer の並び（試験版は同じ版の正式より小さい）。"""
    match = _VERSION.match(str(text or "").strip())
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    idents = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return (int(major), int(minor), int(patch), 0 if pre else 1, idents)


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def plain_version(text: str) -> str:
    """先頭の v を除いた版（読めなければ空）。"""
    value = str(text or "").strip()
    return value[1:] if value[:1] == "v" and parse_version(value) else (value if parse_version(value) else "")


def current_version() -> str:
    return __version__


# ---- 環境 ----

def install_mode() -> str:
    """qpkg（本番の QPKG）・qpkg-sim（QTS の模擬）・linux（それ以外）。"""
    value = os.environ.get("NEWSHUB_INSTALL", "").strip()
    return value if value in ("qpkg", "qpkg-sim") else "linux"


def can_install() -> bool:
    return install_mode() in ("qpkg", "qpkg-sim")


def _test_feed() -> Path | None:
    """試験用の Release の置き場。qpkg-sim のときだけ（本番の qpkg では環境変数があっても無視する）。"""
    if install_mode() != "qpkg-sim":
        return None
    raw = os.environ.get("NEWSHUB_UPDATE_TEST_FEED", "").strip()
    if not raw:
        return None
    return Path(raw[7:] if raw.startswith("file://") else raw).resolve()


def public_key_b64() -> str:
    if install_mode() == "qpkg-sim":
        override = os.environ.get("NEWSHUB_UPDATE_TEST_PUBKEY", "").strip()
        if override:
            return override
    return release_key.PUBLIC_KEY_B64


def updates_dir() -> Path:
    return data_dir() / "updates"


def _settings_path() -> Path:
    return data_dir() / "update-settings.json"


def include_prerelease() -> bool:
    try:
        return bool(json.loads(_settings_path().read_text(encoding="utf-8")).get("include_prerelease"))
    except (OSError, ValueError, AttributeError):
        return False


def set_include_prerelease(value: bool) -> bool:
    writer.write_file(_settings_path(), json.dumps({"include_prerelease": bool(value)}), name="update.settings")
    return bool(value)


# ---- メモリの状態 ----

_S: dict = {}


def reset() -> None:
    """試験の後始末: メモリの状態を初期にする。"""
    _S.clear()
    _S.update(releases=[], etag="", etag_url="", checked_at="", error="", checking=False, checks=0, etag_hits=0, job=_idle(), task=None)


def _idle() -> dict:
    return {"phase": "idle", "kind": "", "target": "", "message": "", "percent": 0, "started_at": ""}


reset()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- 通信（https・ホストの許可・リダイレクトの各段・大きさの上限） ----

def _new_client() -> httpx.AsyncClient:
    """外への接続は egress（検査した IP へ固定）。試験はここを差し替える。"""
    return egress.guarded_client(timeout=30, headers={"User-Agent": UA})


def check_url(url: str, hosts: frozenset[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UpdateError("https 以外の URL からは取得しません")
    if parts.username or parts.password or not parts.hostname:
        raise UpdateError("取得先の URL が正しくありません")
    if parts.hostname.lower() not in hosts:
        raise UpdateError("許可していないホストからの取得は断りました")
    try:
        port = parts.port
    except ValueError:
        raise UpdateError("取得先の URL が正しくありません") from None
    if port not in (None, 443):
        raise UpdateError("許可していないポートへは接続しません")


async def _get(url: str, *, hosts: frozenset[str], max_bytes: int, headers: dict | None = None, sink=None, on_bytes=None) -> tuple[int, dict, bytes]:
    """GET。リダイレクトは各段の URL を検査して追う。sink（async 関数）があれば本文はそこへ流し、無ければ戻り値に入れる。"""
    current = url
    try:
        async with _new_client() as client:
            for _ in range(MAX_REDIRECTS + 1):
                check_url(current, hosts)
                async with client.stream("GET", current, headers=headers) as resp:
                    head = {k.lower(): v for k, v in resp.headers.items()}
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = head.get("location")
                        if not location:
                            raise UpdateError("リダイレクト先がありません")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code != 200:
                        return resp.status_code, head, b""
                    declared = head.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise UpdateError("ファイルが大きすぎます")
                    total, chunks = 0, []
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise UpdateError("ファイルが大きすぎます")
                        if sink is not None:
                            await sink(chunk)
                        else:
                            chunks.append(chunk)
                        if on_bytes is not None:
                            on_bytes(total)
                    return 200, head, b"".join(chunks)
    except egress.EgressRejected as exc:
        raise UpdateError(str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise UpdateError("時間内に応答がありません") from exc
    except httpx.HTTPError as exc:
        raise UpdateError(f"接続できません（{type(exc).__name__}）") from exc
    raise UpdateError("リダイレクトが多すぎます")


# ---- Release の一覧 ----

def _clean_releases(raw: object) -> list[dict]:
    out: list[dict] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        ver = plain_version(str(r.get("tag_name") or ""))
        if not ver:
            continue
        assets = []
        for a in r.get("assets") if isinstance(r.get("assets"), list) else []:
            if isinstance(a, dict) and isinstance(a.get("name"), str) and isinstance(a.get("browser_download_url"), str):
                assets.append({"name": a["name"][:200], "size": int(a.get("size") or 0), "url": a["browser_download_url"][:1000]})
        html = str(r.get("html_url") or "")
        out.append({"version": ver, "tag": str(r.get("tag_name"))[:60], "prerelease": bool(r.get("prerelease")) or "-" in ver, "notes": str(r.get("body") or "")[:NOTES_MAX],
                    "url": html if html.startswith("https://github.com/") else "", "published_at": str(r.get("published_at") or "")[:30], "assets": assets})
    return out


def asset_names(version: str) -> tuple[str, str, str]:
    q = f"QnapNewsHub_{version}_x86_64.qpkg"
    return q, q + ".manifest.json", q + ".manifest.sig"


def _assets_of(release: dict) -> dict | None:
    names = asset_names(release["version"])
    by_name = {a["name"]: a for a in release["assets"]}
    return {"qpkg": by_name[names[0]], "manifest": by_name[names[1]], "sig": by_name[names[2]]} if all(n in by_name for n in names) else None


def latest(releases: list[dict] | None = None, *, prerelease: bool | None = None) -> dict | None:
    """今の版より新しい Release のうち最も新しいもの。QPKG で入れる環境では、.qpkg・manifest・署名がそろっているものだけ。"""
    pre = include_prerelease() if prerelease is None else prerelease
    need_assets = can_install()
    best = None
    for r in releases if releases is not None else _S["releases"]:
        if r["prerelease"] and not pre:
            continue
        if not is_newer(r["version"], current_version()) or (need_assets and _assets_of(r) is None):
            continue
        if best is None or parse_version(r["version"]) > parse_version(best["version"]):
            best = r
    return best


def _read_feed_list(feed: Path) -> list:
    path = feed / "releases.json"
    if not path.is_file() or path.stat().st_size > MAX_LIST:
        raise UpdateError("試験用の Release の一覧が読めません")
    return json.loads(path.read_text(encoding="utf-8"))


async def check(*, manual: bool = False) -> dict:
    """Release の一覧を確かめる（ETag つき。結果はメモリだけ）。失敗しても例外にせず、状態に理由を残す。"""
    if _S["checking"]:
        return status()
    _S["checking"] = True
    try:
        feed = _test_feed()
        if feed is not None:
            _S["releases"] = _clean_releases(await asyncio.to_thread(_read_feed_list, feed))
            _S["error"] = ""
        else:
            url = API_URL + "?per_page=30"
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if _S["etag"] and _S["etag_url"] == url and _S["releases"] is not None:
                headers["If-None-Match"] = _S["etag"]
            code, head, body = await _get(url, hosts=API_HOSTS, max_bytes=MAX_LIST, headers=headers)
            _S["checks"] += 1
            if code == 304:
                _S["etag_hits"] += 1
                _S["error"] = ""
            elif code == 200:
                try:
                    _S["releases"] = _clean_releases(json.loads(body))
                except ValueError:
                    raise UpdateError("Release の一覧を読めませんでした") from None
                _S["etag"], _S["etag_url"], _S["error"] = head.get("etag", ""), url, ""
            elif code in (403, 429):
                raise UpdateError("GitHub の問い合わせの制限に達しました。しばらくしてからもう一度確かめてください")
            else:
                raise UpdateError(f"GitHub から Release の一覧を取れませんでした（HTTP {code}）")
        _S["checked_at"] = _now_iso()
    except UpdateError as exc:
        _S["error"] = str(exc)
        logger.warning("更新の確認に失敗しました: %s", exc)
    except Exception:  # noqa: BLE001 - 確認の失敗でアプリを落とさない
        _S["error"] = "更新の確認に失敗しました"
        logger.exception("更新の確認に失敗しました")
    finally:
        _S["checking"] = False
    return status()


async def loop() -> None:
    """起動の少しあとと、その後 1 日 1 回、自動で確認する（お休みモードの間は通信しない）。"""
    from qnapnewshub import rest

    await asyncio.sleep(FIRST_CHECK_DELAY)
    while True:
        try:
            if not rest.active():
                await check()
        except Exception:  # noqa: BLE001
            logger.exception("更新の自動確認に失敗しました")
        await asyncio.sleep(CHECK_EVERY)


# ---- 署名と中身の検査 ----

def verify_manifest(manifest: bytes, sig_text: str, version: str) -> dict:
    """署名（manifest のバイト列そのもの）と、名前・版・形を確かめる。合わなければ UpdateError。"""
    try:
        sig = base64.b64decode(sig_text.strip(), validate=True)
        if len(sig) != 64:
            raise ValueError
    except (binascii.Error, ValueError):
        raise UpdateError("署名の形が正しくありません。入れません") from None
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64(), validate=True)).verify(sig, manifest)
    except (InvalidSignature, ValueError, binascii.Error):
        raise UpdateError("署名が正しくありません。入れません") from None
    try:
        data = json.loads(manifest)
    except ValueError:
        raise UpdateError("manifest を読めません。入れません") from None
    if not isinstance(data, dict) or set(data) != {"name", "version", "sha256", "size"}:
        raise UpdateError("manifest の形が正しくありません。入れません")
    if data["name"] != PACKAGE:
        raise UpdateError("名前が違います。入れません")
    if data["version"] != version or not isinstance(data["version"], str):
        raise UpdateError("版が違います。入れません")
    if not isinstance(data["sha256"], str) or not _SHA.match(data["sha256"]):
        raise UpdateError("manifest の sha256 の形が正しくありません。入れません")
    if not isinstance(data["size"], int) or isinstance(data["size"], bool) or not 0 < data["size"] <= MAX_BYTES:
        raise UpdateError("manifest の大きさが正しくありません。入れません")
    return data


def verify_payload(path: Path, manifest: dict) -> None:
    """.qpkg の大きさと sha256 が manifest と合うか。"""
    try:
        size = path.stat().st_size
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        raise UpdateError("取得したファイルを読めません") from None
    if size != manifest["size"]:
        raise UpdateError("大きさが manifest と違います。入れません")
    if not hmac.compare_digest(h.hexdigest(), manifest["sha256"]):
        raise UpdateError("sha256 が manifest と違います。入れません")


def _small(path: Path, limit: int = MAX_SMALL) -> bytes:
    try:
        if path.stat().st_size > limit:
            raise UpdateError("manifest が大きすぎます")
        return path.read_bytes()
    except OSError:
        raise UpdateError("manifest・署名のファイルが読めません") from None


def verify_stored(version: str) -> Path:
    """updates/ に置いた版を、署名・sha256・大きさ・名前・版で確かめ直して .qpkg のパスを返す。"""
    q, m, s = asset_names(version)
    d = updates_dir()
    manifest = verify_manifest(_small(d / m), _small(d / s).decode("ascii", "replace"), version)
    verify_payload(d / q, manifest)
    return d / q


# ---- 取得 ----

async def _fetch_small(asset: dict, feed: Path | None) -> bytes:
    if feed is not None:
        name = Path(str(asset["url"])).name
        target = (feed / name).resolve()
        if feed not in target.parents or not target.is_file():
            raise UpdateError("試験用の取得物が見つかりません")
        return await asyncio.to_thread(_small, target)
    code, _, body = await _get(asset["url"], hosts=ASSET_HOSTS, max_bytes=MAX_SMALL)
    if code != 200:
        raise UpdateError(f"manifest・署名を取得できませんでした（HTTP {code}）")
    return body


async def _w(name: str, fn):
    """書き込みは書き手のスレッド（Write Coordinator）で。"""
    return await asyncio.to_thread(writer.run, name, fn)


async def _download_qpkg(asset: dict, part: Path, size: int, feed: Path | None, progress) -> None:
    handle = {}
    buf = bytearray()

    def open_part() -> None:
        part.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle["fh"] = open(part, "wb")  # noqa: SIM115 - 書き手のスレッドで開き閉じる

    def write(data: bytes) -> None:
        handle["fh"].write(data)

    def close() -> None:
        fh = handle.pop("fh", None)
        if fh is not None:
            fh.close()

    async def sink(chunk: bytes) -> None:
        buf.extend(chunk)
        if len(buf) >= (1 << 20):
            data = bytes(buf)
            buf.clear()
            await _w("update.part", lambda: write(data))

    await _w("update.part", open_part)
    try:
        if feed is not None:
            name = Path(str(asset["url"])).name
            src = (feed / name).resolve()
            if feed not in src.parents or not src.is_file():
                raise UpdateError("試験用の取得物が見つかりません")
            done = 0
            with open(src, "rb") as fh:
                while True:
                    chunk = await asyncio.to_thread(fh.read, 1 << 20)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > size:
                        raise UpdateError("大きさが manifest と違います。入れません")
                    await _w("update.part", lambda c=chunk: write(c))
                    progress(done)
        else:
            code, _, _ = await _get(asset["url"], hosts=ASSET_HOSTS, max_bytes=size, sink=sink, on_bytes=progress)
            if code != 200:
                raise UpdateError(f"取得できませんでした（HTTP {code}）")
            if buf:
                data = bytes(buf)
                buf.clear()
                await _w("update.part", lambda: write(data))
    except UpdateError:
        await _w("update.part", close)
        await _w("update.cleanup", lambda: part.unlink(missing_ok=True))
        raise
    except BaseException:
        await _w("update.part", close)
        await _w("update.cleanup", lambda: part.unlink(missing_ok=True))
        raise
    await _w("update.part", close)


def stored_versions() -> list[str]:
    """updates/ にそろっている（.qpkg・manifest・署名）版。新しい順。"""
    out = []
    try:
        names = [p.name for p in updates_dir().iterdir()]
    except OSError:
        return []
    for name in names:
        match = _QPKG_NAME.match(name)
        if match and all(n in names for n in asset_names(match[1])):
            out.append(match[1])
    return sorted(out, key=parse_version, reverse=True)


def rollback_candidate() -> str | None:
    """今の版より古くて、取得物が残っている版のうち最も新しいもの。"""
    older = [v for v in stored_versions() if parse_version(v) and is_newer(current_version(), v)]
    return older[0] if older else None


def _prune(keep: str) -> None:
    """取得物は、今入れる版と、それ以外で最も新しい 1 つ前の版だけ残す（.part も消す）。"""
    d = updates_dir()
    others = [v for v in stored_versions() if v != keep]
    keep_set = {keep, *others[:1]}
    for p in list(d.iterdir()):
        match = re.match(r"^QnapNewsHub_(.+?)_x86_64\.qpkg(\.manifest\.json|\.manifest\.sig|\.part)?$", p.name)
        if match and (match[2] == ".part" or match[1] not in keep_set):
            p.unlink(missing_ok=True)


# ---- 入れる ----

_CHILD = (
    'sh "$1" >"$2" 2>&1; rc=$?; '
    'printf \'{"kind":"%s","from":"%s","to":"%s","rc":%s,"at":"%s"}\\n\' "$4" "$5" "$6" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$3.tmp" && mv "$3.tmp" "$3"; '
    'if [ -n "$7" ] && [ -x "$7/QnapNewsHub.sh" ]; then "$7/QnapNewsHub.sh" status >/dev/null 2>&1 || "$7/QnapNewsHub.sh" start >/dev/null 2>&1; fi'
)


def _spawn(cmd: list[str], env: dict, cwd: Path) -> None:
    """インストーラーを、新しいセッションの子として流す（サーバーの停止で一緒に止まらない）。試験はここを差し替える。"""
    subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # noqa: S603
                     start_new_session=True, close_fds=True)


def _qpkg_root() -> str:
    raw = os.environ.get("NEWSHUB_QPKG_ROOT", "").strip()
    return str(Path(raw).resolve()) if raw and Path(raw).is_absolute() else ""


def _launch(qpkg: Path, kind: str, from_v: str, to_v: str) -> None:
    d = updates_dir()
    if not (_PLAIN_VERSION.match(from_v) and _PLAIN_VERSION.match(to_v) and kind in ("update", "rollback")):
        raise UpdateError("版の形が正しくありません")
    qpkg = qpkg.resolve()
    if d.resolve() not in qpkg.parents:
        raise UpdateError("取得物の置き場の外は使えません")
    path = "/usr/bin:/bin:/usr/sbin:/sbin" + (":." if install_mode() == "qpkg-sim" else "")      # ":." は QTS の busybox sh の挙動を模擬の側で合わせるため（sim だけ）
    env = {"PATH": path, "HOME": str(data_dir()), "LANG": "C.UTF-8"}
    if install_mode() == "qpkg-sim":      # 模擬では、入れ直したあとの起動台本（QnapNewsHub.sh）にも同じ試験用の差し替えを渡す
        env.update(QNH_INSTALL="qpkg-sim", QNH_UPDATE_TEST_FEED=os.environ.get("NEWSHUB_UPDATE_TEST_FEED", ""),
                   QNH_UPDATE_TEST_PUBKEY=os.environ.get("NEWSHUB_UPDATE_TEST_PUBKEY", ""))
    _spawn(["/bin/sh", "-c", _CHILD, "sh", str(qpkg), str(d / "install.log"), str(d / "last.json"), kind, from_v, to_v, _qpkg_root()], env, d)


def last_result() -> dict | None:
    """前回の更新の結果（updates/last.json を、そのつど読む）。{kind, from, to, rc, at, ok, message, log}。"""
    d = updates_dir()
    try:
        raw = json.loads((d / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("rc"), int):
        return None
    kind, from_v, to_v = str(raw.get("kind", "")), str(raw.get("from", "")), str(raw.get("to", ""))
    if kind not in ("update", "rollback") or not (_PLAIN_VERSION.match(from_v) and _PLAIN_VERSION.match(to_v)):
        return None
    ok = raw["rc"] in INSTALL_OK_CODES and current_version() == to_v
    label = "前の版に戻しました" if kind == "rollback" else "更新しました"
    if ok:
        message = f"{label}（{from_v} → {to_v}）"
    elif raw["rc"] in INSTALL_OK_CODES:
        message = f"インストーラーは終わりましたが、版が {to_v} になっていません（いまは {current_version()}）"
    else:
        message = f"{'戻す' if kind == 'rollback' else '更新'}に失敗しました（インストーラーの終了コード {raw['rc']}）"
    log = ""
    if not ok:
        try:
            from qnapnewshub import logbuf

            log = logbuf.redact("\n".join((d / "install.log").read_text(encoding="utf-8", errors="replace").splitlines()[-30:]))[-3000:]
        except OSError:
            pass
    return {"kind": kind, "from": from_v, "to": to_v, "rc": raw["rc"], "at": str(raw.get("at", ""))[:30], "ok": ok, "message": message, "log": log}


def _set_job(**kw) -> None:
    _S["job"].update(kw)


def _guard_start(kind: str) -> None:
    from qnapnewshub import rest

    if not can_install():
        raise UpdateError("この環境では自動で入れられません（QNAP の QPKG だけ）。手順に従って更新してください")
    if rest.active():
        ends = rest.state().get("ends_at") or ""
        raise UpdateError("お休みモードの間は更新しません。明けてからにしてください" + (f"（{ends[11:16]} に明けます）" if ends else ""))
    if _S["job"]["phase"] in ("downloading", "verifying", "flushing", "installing"):
        raise UpdateError("いま更新の途中です")


def start_update(version: str) -> dict:
    """「更新する」: 確認ずみの最新の版を取得・検査して入れる。裏で進め、進み具合は status() の job に出る。"""
    _guard_start("update")
    target = latest()
    if target is None or target["version"] != version:
        raise UpdateError("新しい版の情報が変わりました。もう一度確かめてください")
    _begin("update", target["version"])
    _S["task"] = asyncio.get_running_loop().create_task(_run_update(target))
    return status()


def start_rollback(version: str) -> dict:
    """「前の版に戻す」: 残っている前の版を、署名を確かめ直してから入れる。"""
    _guard_start("rollback")
    if version != rollback_candidate():
        raise UpdateError("戻せる版が変わりました。もう一度確かめてください")
    _begin("rollback", version)
    _S["task"] = asyncio.get_running_loop().create_task(_run_rollback(version))
    return status()


def _begin(kind: str, target: str) -> None:
    _S["job"] = {"phase": "downloading" if kind == "update" else "verifying", "kind": kind, "target": target,
                 "message": "取得しています" if kind == "update" else "署名を確かめています", "percent": 0, "started_at": _now_iso()}


async def _finish(qpkg: Path, kind: str, target: str) -> None:
    """検査を済ませた .qpkg を入れる: メモリの記事を書き切り、前回の結果を消して、切り離した子で流す。"""
    _set_job(phase="flushing", message="メモリの記事を書き切っています", percent=100)
    await asyncio.to_thread(writer.flush, "update")
    d = updates_dir()
    await _w("update.cleanup", lambda: [(d / n).unlink(missing_ok=True) for n in ("last.json", "install.log")])
    await _w("update.prune", lambda: _prune(target))
    _set_job(phase="installing", message="インストーラーを起動しました。サーバーが入れ替わります（数十秒）")
    logger.warning("更新を始めます: %s → %s（%s）", current_version(), target, kind)
    await asyncio.to_thread(_launch, qpkg, kind, current_version(), target)


async def _run_update(release: dict) -> None:
    version = release["version"]
    feed = _test_feed()
    try:
        assets = _assets_of(release)
        if assets is None:
            raise UpdateError("Release に .qpkg・manifest・署名がそろっていません")
        qname, mname, sname = asset_names(version)
        d = updates_dir()
        manifest_b = await _fetch_small(assets["manifest"], feed)
        sig_b = await _fetch_small(assets["sig"], feed)
        manifest = verify_manifest(manifest_b, sig_b.decode("ascii", "replace"), version)      # 大きいファイルを取る前に、署名を確かめる
        _set_job(message="取得しています")
        part = d / (qname + ".part")

        def progress(done: int) -> None:
            _set_job(percent=min(99, int(done * 100 / manifest["size"])))

        await _download_qpkg(assets["qpkg"], part, manifest["size"], feed, progress)
        _set_job(phase="verifying", message="署名と sha256 を確かめています", percent=100)
        try:
            await asyncio.to_thread(verify_payload, part, manifest)
        except UpdateError:
            await _w("update.cleanup", lambda: part.unlink(missing_ok=True))      # 合わなかった取得物は残さない
            raise

        def place() -> None:
            os.replace(part, d / qname)
            (d / mname).write_bytes(manifest_b)
            (d / sname).write_bytes(sig_b)

        await _w("update.store", place)
        qpkg = await asyncio.to_thread(verify_stored, version)           # 置いたものを確かめ直してから入れる
        await _finish(qpkg, "update", version)
    except UpdateError as exc:
        _fail(str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("更新に失敗しました")
        _fail("更新に失敗しました（ログを確かめてください）")


async def _run_rollback(version: str) -> None:
    try:
        qpkg = await asyncio.to_thread(verify_stored, version)
        await _finish(qpkg, "rollback", version)
    except UpdateError as exc:
        _fail(str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("戻すのに失敗しました")
        _fail("戻すのに失敗しました（ログを確かめてください）")


def _fail(message: str) -> None:
    logger.warning("更新を止めました: %s", message)
    _set_job(phase="error", message=message)


# ---- 状態（画面・診断・知らせ） ----

def _public(release: dict | None) -> dict | None:
    if release is None:
        return None
    qpkg = (_assets_of(release) or {}).get("qpkg")
    return {"version": release["version"], "tag": release["tag"], "prerelease": release["prerelease"], "notes": release["notes"], "url": release["url"],
            "published_at": release["published_at"], "size": qpkg["size"] if qpkg else 0}


def status() -> dict:
    from qnapnewshub import rest

    rs = rest.state()
    target = latest()
    mode = install_mode()
    out = {"version": current_version(), "install": mode, "can_install": can_install(), "include_prerelease": include_prerelease(), "checked_at": _S["checked_at"],
           "checking": _S["checking"], "error": _S["error"], "latest": _public(target), "update_available": target is not None,
           "rollback": rollback_candidate() if can_install() else None, "job": dict(_S["job"]), "last": last_result(),
           "rest": {"active": rs["active"], "ends_at": rs["ends_at"]}, "etag_hits": _S["etag_hits"], "checks": _S["checks"], "manual_steps": []}
    if not can_install():
        out["manual_steps"] = ["リポジトリを git で取り直す（git pull）", "./hub.sh setup && ./hub.sh build", "サービスを再起動する（systemctl --user restart qnapnewshub.service）"]
    return out


def notice() -> dict:
    """ホームの一行・☰ の印（新しい版があるときだけ message が入る）。"""
    target = latest()
    if target is None:
        return {"available": False, "version": "", "message": None}
    return {"available": True, "version": target["version"], "message": f"新しい版 {target['version']} があります（いまは {current_version()}）"}


def summary() -> dict:
    """診断の画面に出す更新の状態（ノートは含めない）。"""
    s = status()
    return {"version": s["version"], "install": s["install"], "can_install": s["can_install"], "checked_at": s["checked_at"], "error": s["error"],
            "latest": s["latest"]["version"] if s["latest"] else "", "include_prerelease": s["include_prerelease"], "etag_hits": s["etag_hits"],
            "checks": s["checks"], "job": s["job"]["phase"], "last": s["last"], "rollback": s["rollback"]}
