"""更新の機能の試験（docs/UPDATE.md）。

目的: 署名つきの Release だけを入れる・合わないものは必ず断る・外への取得は https と許可したホストだけ・お休み中と Linux では入れない・
管理者だけが操作できる・前回の結果が画面に出る、を壊さないこと。
根拠: 2026-11-24 利用者の構想（QNAP の上のアプリを GitHub の Release で更新する。公開のリポジトリなので、改ざんされた .qpkg を入れさせない）。
AGENTS.md 7（外の取得: 各段の検査・大きさの上限）。外への通信は全部模擬（httpx.MockTransport・一時の鍵）。本物の秘密鍵は使わない。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from qnapnewshub import release_key, rest, updater, writer
from tests.conftest import ADMIN_NAME, ADMIN_PASSWORD

_spec = importlib.util.spec_from_file_location("sign", Path(__file__).resolve().parents[2] / "packaging" / "release" / "sign.py")
sign = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sign)

HDR = {"X-Requested-With": "NewsHub"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    updater.reset()
    monkeypatch.delenv("NEWSHUB_INSTALL", raising=False)
    monkeypatch.delenv("NEWSHUB_UPDATE_TEST_FEED", raising=False)
    monkeypatch.delenv("NEWSHUB_UPDATE_TEST_PUBKEY", raising=False)
    monkeypatch.setattr(updater, "current_version", lambda: "0.1.0")
    yield
    updater.reset()


@pytest.fixture()
def key(monkeypatch):
    k = Ed25519PrivateKey.generate()
    pub = base64.b64encode(k.public_key().public_bytes_raw()).decode()
    monkeypatch.setattr(release_key, "PUBLIC_KEY_B64", pub)
    return k, pub


def make_qpkg(tmp_path: Path, version="0.1.1", body=b"qpkg-bytes" * 100) -> Path:
    d = tmp_path / f"art-{version}"
    d.mkdir(exist_ok=True)
    p = d / f"QnapNewsHub_{version}_x86_64.qpkg"
    p.write_bytes(body)
    return p


def sign_into(qpkg: Path, k) -> tuple[bytes, str]:
    m, s = sign.sign_qpkg(qpkg, k)
    return m.read_bytes(), s.read_text().strip()


def release(version="0.1.1", pre=False, assets=True, base="https://github.com/o/r/releases/download"):
    names = updater.asset_names(version)
    return {"tag_name": f"v{version}", "prerelease": pre, "draft": False, "body": "## 変更\n- 更新の機能", "html_url": "https://github.com/o/r/releases/tag/x",
            "published_at": "2026-11-24T00:00:00Z",
            "assets": [{"name": n, "size": 10, "browser_download_url": f"{base}/v{version}/{n}"} for n in names] if assets else []}


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(updater, "_new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))


def run(coro):
    import asyncio

    return asyncio.run(coro)


# ---- 版の比較・試験版 ----

def test_version_comparison():
    """目的: 0.1.0・0.1.1・0.2.0-rc1 などの並びを SemVer どおりに比べる。根拠: 文字列比較だと 0.10.0 < 0.9.0 になる。"""
    order = ["0.1.0", "0.1.1", "0.2.0-rc1", "0.2.0-rc2", "0.2.0", "0.10.0", "1.0.0-alpha.1", "1.0.0-alpha.2", "1.0.0-beta", "1.0.0"]
    keys = [updater.parse_version(v) for v in order]
    assert all(k is not None for k in keys)
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert updater.is_newer("v0.1.1", "0.1.0") and not updater.is_newer("0.1.0", "0.1.0") and not updater.is_newer("0.2.0-rc1", "0.2.0")
    assert updater.parse_version("latest") is None and updater.parse_version("0.1") is None and updater.parse_version("0.1.0; rm") is None


def test_prerelease_is_excluded_by_default(monkeypatch):
    """目的: 試験版は設定で含めたときだけ候補にする（既定は含めない）。"""
    rels = updater._clean_releases([release("0.2.0-rc1", pre=True), release("0.1.1"), release("0.1.0")])
    assert updater.latest(rels)["version"] == "0.1.1"
    assert updater.latest(rels, prerelease=True)["version"] == "0.2.0-rc1"
    only_pre = updater._clean_releases([release("0.2.0-rc1", pre=True)])
    assert updater.latest(only_pre) is None
    assert updater._clean_releases([{**release("0.3.0"), "draft": True}]) == []


def test_release_without_assets_is_skipped_only_where_installing(monkeypatch):
    """目的: QPKG で入れる環境では、.qpkg・manifest・署名がそろった Release だけを候補にする。Linux は知らせるだけなのでそろわなくても出す。"""
    rels = updater._clean_releases([release("0.1.2", assets=False), release("0.1.1")])
    assert updater.latest(rels)["version"] == "0.1.2"
    monkeypatch.setenv("NEWSHUB_INSTALL", "qpkg")
    assert updater.latest(rels)["version"] == "0.1.1"


# ---- 署名と中身の検査 ----

def test_signature_valid_and_every_tamper_is_refused(tmp_path, key):
    """目的: 正しい署名だけを通し、署名が違う・無い・manifest の改ざん・sha256・大きさ・名前・版の不一致は入れない。"""
    k, _ = key
    q = make_qpkg(tmp_path)
    manifest, sig = sign_into(q, k)
    data = updater.verify_manifest(manifest, sig, "0.1.1")
    updater.verify_payload(q, data)
    other = Ed25519PrivateKey.generate()
    for bad_sig in (sign.sign_bytes(other, manifest), "", "not-base64!!", base64.b64encode(b"short").decode()):
        with pytest.raises(updater.UpdateError):
            updater.verify_manifest(manifest, bad_sig, "0.1.1")
    tampered = manifest.replace(b'"size":1000', b'"size":1001')
    assert tampered != manifest
    with pytest.raises(updater.UpdateError, match="署名"):
        updater.verify_manifest(tampered, sig, "0.1.1")

    def resign(**over):
        m = {**json.loads(manifest), **over}
        raw = json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
        return raw, sign.sign_bytes(k, raw)

    with pytest.raises(updater.UpdateError, match="名前"):
        updater.verify_manifest(*resign(name="Other"), "0.1.1")
    with pytest.raises(updater.UpdateError, match="版"):
        updater.verify_manifest(*resign(version="0.1.2"), "0.1.1")
    with pytest.raises(updater.UpdateError, match="形"):
        updater.verify_manifest(*resign(extra=1), "0.1.1")
    bad_sha = updater.verify_manifest(*resign(sha256="0" * 64), "0.1.1")      # 署名は正しいが sha256 が中身と違う
    with pytest.raises(updater.UpdateError, match="sha256"):
        updater.verify_payload(q, bad_sha)
    bad_size = updater.verify_manifest(*resign(size=data["size"] + 1), "0.1.1")
    with pytest.raises(updater.UpdateError, match="大きさ"):
        updater.verify_payload(q, bad_size)
    with pytest.raises(updater.UpdateError):
        updater.verify_manifest(*resign(size=updater.MAX_BYTES + 1), "0.1.1")


def test_sign_script_checks_repo_public_key(tmp_path, key):
    """目的: sign.py は、リポジトリの公開鍵と対でない秘密鍵では署名を出さない（鍵の取り違えのまま Release しない）。また本物の公開鍵は決めた値である。"""
    assert release_key.PUBLIC_KEY_B64 == key[1]       # fixture で差し替え済み
    q = make_qpkg(tmp_path)
    sign.sign_qpkg(q, key[0], public_b64=key[1])
    with pytest.raises(ValueError, match="対になっていません"):
        sign.sign_qpkg(q, Ed25519PrivateKey.generate(), public_b64=key[1])
    assert json.loads(q.with_name(q.name + ".manifest.json").read_text())["name"] == "QnapNewsHub"
    with pytest.raises(ValueError):
        sign.build_manifest(tmp_path / "evil.qpkg")


def test_real_public_key_is_32_bytes():
    """目的: リポジトリに置いた公開鍵（本物）が Ed25519 の 32 バイトの形であること（鍵ファイルの書き損じを防ぐ）。"""
    import re

    value = re.search(r'^PUBLIC_KEY_B64 = "([^"]+)"', Path(release_key.__file__).read_text(), re.M).group(1)
    assert len(base64.b64decode(value, validate=True)) == 32


# ---- 取得の制限 ----

def test_https_and_host_restrictions(monkeypatch):
    """目的: https 以外・許可していないホスト・ユーザー情報つき・別のポートは取得しない。"""
    ok = ["https://github.com/a/b", "https://objects.githubusercontent.com/x", "https://release-assets.githubusercontent.com/x"]
    for u in ok:
        updater.check_url(u, updater.ASSET_HOSTS)
    for u in ["http://github.com/a", "https://evil.example/a", "https://github.com.evil.test/a", "https://u:p@github.com/a", "https://github.com:8443/a", "ftp://github.com/a",
              "https://api.github.com/a", "file:///etc/passwd"]:
        with pytest.raises(updater.UpdateError):
            updater.check_url(u, updater.ASSET_HOSTS)


def test_redirect_each_hop_is_checked_and_size_capped(monkeypatch):
    """目的: リダイレクトは各段を検査する（許可外・http への転送は断る）。大きさは申告と実際の両方で上限を守る。"""
    def handler(req: httpx.Request):
        if req.url.host == "github.com":
            return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/f"})
        if req.url.host == "release-assets.githubusercontent.com":
            return httpx.Response(200, content=b"x" * 50)
        return httpx.Response(404)

    mock_client(monkeypatch, handler)
    code, _, body = run(updater._get("https://github.com/a", hosts=updater.ASSET_HOSTS, max_bytes=100))
    assert code == 200 and len(body) == 50
    with pytest.raises(updater.UpdateError, match="大きすぎ"):
        run(updater._get("https://github.com/a", hosts=updater.ASSET_HOSTS, max_bytes=10))

    for target in ("https://evil.example/f", "http://github.com/f", "https://github.com:444/f"):
        mock_client(monkeypatch, lambda req, t=target: httpx.Response(302, headers={"location": t}) if req.url.host == "github.com" and req.url.path == "/a" else httpx.Response(200, content=b"x"))
        with pytest.raises(updater.UpdateError):
            run(updater._get("https://github.com/a", hosts=updater.ASSET_HOSTS, max_bytes=100))

    mock_client(monkeypatch, lambda req: httpx.Response(200, headers={"content-length": str(updater.MAX_BYTES + 1)}, content=b"x"))
    with pytest.raises(updater.UpdateError, match="大きすぎ"):
        run(updater._get("https://github.com/a", hosts=updater.ASSET_HOSTS, max_bytes=updater.MAX_BYTES))

    mock_client(monkeypatch, lambda req: httpx.Response(302, headers={"location": "/a"}))
    with pytest.raises(updater.UpdateError, match="多すぎ"):
        run(updater._get("https://github.com/a", hosts=updater.ASSET_HOSTS, max_bytes=100))


# ---- 確認・ETag ----

def test_check_uses_etag_and_keeps_result_on_304(monkeypatch):
    """目的: 確認は ETag を付けて GitHub の制限を節約する（304 なら前の結果のまま）。結果はメモリだけ。"""
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.headers.get("if-none-match"))
        if req.headers.get("if-none-match") == '"abc"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"etag": '"abc"'}, json=[release("0.1.1")])

    mock_client(monkeypatch, handler)
    s1 = run(updater.check(manual=True))
    s2 = run(updater.check(manual=True))
    assert seen == [None, '"abc"']
    assert s1["latest"]["version"] == s2["latest"]["version"] == "0.1.1" and s2["etag_hits"] == 1 and s2["error"] == ""
    assert not (updater.data_dir() / "updates").exists()          # ディスクへは何も書かない


def test_check_failures_are_reported_not_raised(monkeypatch):
    """目的: GitHub の制限・障害・壊れた応答でも落とさず、利用者向けの文を状態に残す。前の結果は残す。"""
    mock_client(monkeypatch, lambda req: httpx.Response(200, json=[release("0.1.1")]))
    run(updater.check())
    for resp, text in ((httpx.Response(403), "制限"), (httpx.Response(500), "HTTP 500"), (httpx.Response(200, content=b"<html>"), "読めません")):
        mock_client(monkeypatch, lambda req, r=resp: r)
        s = run(updater.check())
        assert text in s["error"] and s["latest"]["version"] == "0.1.1"


def test_check_never_goes_to_other_host(monkeypatch):
    """目的: API の確認は api.github.com 以外へ行かない（転送されても断る）。"""
    mock_client(monkeypatch, lambda req: httpx.Response(302, headers={"location": "https://evil.example/x"}))
    assert "許可していない" in run(updater.check())["error"]


def test_test_feed_only_in_sim(tmp_path, monkeypatch):
    """目的: 試験用の差し替えは NEWSHUB_INSTALL=qpkg-sim のときだけ効く。本番の qpkg・Linux では、環境変数があっても無視する。"""
    (tmp_path / "releases.json").write_text(json.dumps([release("0.1.1")]))
    monkeypatch.setenv("NEWSHUB_UPDATE_TEST_FEED", f"file://{tmp_path}")
    monkeypatch.setenv("NEWSHUB_UPDATE_TEST_PUBKEY", "AAAA")
    for mode in ("qpkg", "linux", ""):
        monkeypatch.setenv("NEWSHUB_INSTALL", mode)
        assert updater._test_feed() is None and updater.public_key_b64() == release_key.PUBLIC_KEY_B64
    monkeypatch.setenv("NEWSHUB_INSTALL", "qpkg-sim")
    assert updater._test_feed() == tmp_path.resolve() and updater.public_key_b64() == "AAAA"
    assert run(updater.check())["latest"]["version"] == "0.1.1"


# ---- API ----

def api_release_feed(monkeypatch, rels):
    mock_client(monkeypatch, lambda req: httpx.Response(200, json=rels))


def test_only_admin_session_can_operate(client, monkeypatch):
    """目的: 確認・設定・更新・戻すは管理者の session だけ。未ログインは 401、API トークン（読み取り専用）は 403。状態の読み取りはログインした人。"""
    api_release_feed(monkeypatch, [release("0.1.1")])
    anon = TestClient(client.app)
    for method, path, body in (("get", "", None), ("post", "/check", None), ("put", "/settings", {"include_prerelease": True}),
                               ("post", "/install", {"version": "0.1.1"}), ("post", "/rollback", {"version": "0.1.0"}), ("get", "/notice", None)):
        r = getattr(anon, method)(f"/api/v1/update{path}", **({"json": body} if body else {}))
        assert r.status_code == 401, (method, path)
    token = client.post("/api/v1/auth/tokens", json={"name": "ControlDeck"}).json()["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    for path, body in (("/check", None), ("/install", {"version": "0.1.1"}), ("/rollback", {"version": "0.1.0"})):
        assert anon.post(f"/api/v1/update{path}", json=body, headers=bearer).status_code == 403
    assert anon.put("/api/v1/update/settings", json={"include_prerelease": True}, headers=bearer).status_code == 403
    assert client.post("/api/v1/update/check").status_code == 200


def test_status_notice_and_prerelease_setting(client, monkeypatch):
    """目的: 確認の結果・ホームの知らせ・試験版の設定が API に出る。設定は update-settings.json に残り、既定は含めない。"""
    api_release_feed(monkeypatch, [release("0.2.0-rc1", pre=True), release("0.1.1")])
    assert client.get("/api/v1/update/notice").json() == {"available": False, "version": "", "message": None}
    s = client.post("/api/v1/update/check").json()
    assert s["version"] == "0.1.0" and s["latest"]["version"] == "0.1.1" and s["include_prerelease"] is False and s["install"] == "linux"
    assert "更新の機能" in s["latest"]["notes"] and s["can_install"] is False and s["manual_steps"]
    assert "0.1.1" in client.get("/api/v1/update/notice").json()["message"]
    s = client.put("/api/v1/update/settings", json={"include_prerelease": True}).json()
    assert s["latest"]["version"] == "0.2.0-rc1" and s["latest"]["prerelease"] is True
    assert json.loads((updater.data_dir() / "update-settings.json").read_text()) == {"include_prerelease": True}
    assert client.get("/api/v1/diagnostics").json()["update"]["latest"] == "0.2.0-rc1"


def test_linux_does_not_install(client, monkeypatch):
    """目的: QPKG でない環境（Linux）では自動で入れない。新しい版と手順だけを出す。"""
    api_release_feed(monkeypatch, [release("0.1.1")])
    client.post("/api/v1/update/check")
    r = client.post("/api/v1/update/install", json={"version": "0.1.1"})
    assert r.status_code == 409 and "QPKG" in r.json()["detail"]
    assert client.post("/api/v1/update/rollback", json={"version": "0.0.9"}).status_code == 409


def test_rest_mode_blocks_install(client, monkeypatch):
    """目的: お休みモードの間は更新しない（明けてから）。根拠: ARCHITECTURE 9（休止中は書き込みを起こさない）。"""
    monkeypatch.setenv("NEWSHUB_INSTALL", "qpkg")
    api_release_feed(monkeypatch, [release("0.1.1")])
    client.post("/api/v1/update/check")
    monkeypatch.setattr(rest, "active", lambda: True)
    monkeypatch.setattr(rest, "state", lambda at=None: {"enabled": True, "active": True, "mode": "stop_all", "start": "01:00", "end": "06:00", "ends_at": "2026-11-25T06:00:00"})
    r = client.post("/api/v1/update/install", json={"version": "0.1.1"})
    assert r.status_code == 409 and "お休みモード" in r.json()["detail"] and "06:00" in r.json()["detail"]
    assert not (updater.data_dir() / "updates").exists()


# ---- 更新の実行 ----

class Box:
    """Release の置き場の模擬: manifest・署名・.qpkg を返す transport。"""

    def __init__(self, tmp_path, k, version="0.1.1", body=b"Q" * 5000):
        self.qpkg = make_qpkg(tmp_path, version, body)
        self.manifest, self.sig = sign_into(self.qpkg, k)
        self.version = version
        self.files = {
            updater.asset_names(version)[0]: self.qpkg.read_bytes(),
            updater.asset_names(version)[1]: self.manifest,
            updater.asset_names(version)[2]: (self.sig + "\n").encode(),
        }

    def handler(self, req: httpx.Request):
        if req.url.host == "api.github.com":
            rel = release(self.version)
            for a in rel["assets"]:
                a["size"] = len(self.files[a["name"]])
            return httpx.Response(200, json=[rel])
        return httpx.Response(200, content=self.files[req.url.path.rsplit("/", 1)[1]])


@pytest.fixture()
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_spawn", lambda cmd, env, cwd: calls.append((cmd, env, cwd)))
    return calls


def wait_job(client, phases=("installing", "error"), seconds=10):
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        s = client.get("/api/v1/update").json()
        if s["job"]["phase"] in phases:
            return s
        time.sleep(0.05)
    raise AssertionError(client.get("/api/v1/update").json())


def test_full_update_verifies_flushes_then_launches_detached(client, tmp_path, key, monkeypatch, spawned):
    """目的: QPKG で、取得 → 署名と sha256 の検査 → メモリの記事を書き切る → 取得物を updates/ に置く → 切り離した子で sh <qpkg>、の順で進む。"""
    monkeypatch.setenv("NEWSHUB_INSTALL", "qpkg")
    monkeypatch.setenv("NEWSHUB_QPKG_ROOT", str(tmp_path))
    box = Box(tmp_path, key[0])
    mock_client(monkeypatch, box.handler)
    flushed = []
    monkeypatch.setattr(writer, "flush", lambda reason="manual": flushed.append(reason) or {})
    client.post("/api/v1/update/check")
    assert client.post("/api/v1/update/install", json={"version": "0.9.9"}).status_code == 409      # 確認した版と違う
    assert client.post("/api/v1/update/install", json={"version": "0.1.1"}).status_code == 200
    s = wait_job(client)
    assert s["job"]["phase"] == "installing", s["job"]
    assert flushed == ["update"]
    (cmd, env, cwd), = spawned
    d = updater.updates_dir()
    assert cmd[:2] == ["/bin/sh", "-c"] and cmd[3] == "sh" and cmd[4] == str(d / "QnapNewsHub_0.1.1_x86_64.qpkg") and cmd[7:10] == ["update", "0.1.0", "0.1.1"] and cmd[10] == str(tmp_path)
    assert (d / "QnapNewsHub_0.1.1_x86_64.qpkg").read_bytes() == box.files["QnapNewsHub_0.1.1_x86_64.qpkg"]
    assert not list(d.glob("*.part")) and env["PATH"].count(":.") == 0
    assert (d / "QnapNewsHub_0.1.1_x86_64.qpkg.manifest.json").is_file()


@pytest.mark.parametrize("mutate", ["sig_other_key", "sig_missing", "manifest_tampered", "payload_tampered", "size_wrong", "name_wrong", "version_wrong"])
def test_bad_release_is_never_installed(client, tmp_path, key, monkeypatch, spawned, mutate):
    """目的: 署名が違う・無い・manifest の改ざん・中身の改ざん（sha256）・大きさ・名前・版が合わない Release は、取得物を残さず入れない。"""
    monkeypatch.setenv("NEWSHUB_INSTALL", "qpkg")
    k = key[0]
    box = Box(tmp_path, k)
    qn, mn, sn = updater.asset_names("0.1.1")
    m = json.loads(box.manifest)

    def resign(**over):
        raw = json.dumps({**m, **over}, sort_keys=True, separators=(",", ":")).encode()
        box.files[mn], box.files[sn] = raw, (sign.sign_bytes(k, raw) + "\n").encode()

    if mutate == "sig_other_key":
        box.files[sn] = (sign.sign_bytes(Ed25519PrivateKey.generate(), box.manifest) + "\n").encode()
    elif mutate == "sig_missing":
        box.files[sn] = b""
    elif mutate == "manifest_tampered":
        box.files[mn] = box.manifest.replace(b"QnapNewsHub", b"QnapNewsHub ", 1)
    elif mutate == "payload_tampered":
        box.files[qn] = b"E" * len(box.files[qn])
    elif mutate == "size_wrong":
        resign(size=m["size"] + 7)
    elif mutate == "name_wrong":
        resign(name="Other")
    elif mutate == "version_wrong":
        resign(version="0.1.9")
    mock_client(monkeypatch, box.handler)
    client.post("/api/v1/update/check")
    assert client.post("/api/v1/update/install", json={"version": "0.1.1"}).status_code == 200
    s = wait_job(client)
    assert s["job"]["phase"] == "error" and s["job"]["message"], s["job"]
    assert spawned == []
    d = updater.updates_dir()
    assert not d.exists() or not [p for p in d.iterdir()]


def test_oversized_download_is_stopped(client, tmp_path, key, monkeypatch, spawned):
    """目的: manifest の大きさを超えて流れてくる .qpkg は途中で止め、残さない。"""
    monkeypatch.setenv("NEWSHUB_INSTALL", "qpkg")
    box = Box(tmp_path, key[0])
    qn = updater.asset_names("0.1.1")[0]
    box.files[qn] = box.files[qn] + b"extra" * 100
    mock_client(monkeypatch, box.handler)
    client.post("/api/v1/update/check")
    client.post("/api/v1/update/install", json={"version": "0.1.1"})
    s = wait_job(client)
    assert s["job"]["phase"] == "error" and "大きす" in s["job"]["message"] and spawned == []
    assert not list(updater.updates_dir().glob("*.part"))


def test_retention_rollback_and_last_result(client, tmp_path, key, monkeypatch, spawned):
    """目的: 古い取得物は 1 つ前の版だけ残す・前の版へ戻せる（署名を確かめ直す。壊れていたら断る）・last.json の結果を画面用に読む（成功／版が違う／失敗とログ）。"""
    monkeypatch.setenv("NEWSHUB_INSTALL", "qpkg")
    d = updater.updates_dir()
    d.mkdir(parents=True)
    for v in ("0.0.8", "0.0.9"):
        q = make_qpkg(tmp_path, v)
        m, s = sign_into(q, key[0])
        for n, data in zip(updater.asset_names(v), (q.read_bytes(), m, (s + "\n").encode())):
            (d / n).write_bytes(data)
    assert updater.stored_versions() == ["0.0.9", "0.0.8"] and updater.rollback_candidate() == "0.0.9"
    updater._prune("0.1.1")
    assert updater.stored_versions() == ["0.0.9"]            # 今入れる版（まだ無い）と、それ以外で最も新しい 1 つだけ
    assert client.get("/api/v1/update").json()["rollback"] == "0.0.9"
    assert client.post("/api/v1/update/rollback", json={"version": "0.0.8"}).status_code == 409
    assert client.post("/api/v1/update/rollback", json={"version": "0.0.9"}).status_code == 200
    s = wait_job(client)
    assert s["job"]["phase"] == "installing" and spawned[0][0][7:10] == ["rollback", "0.1.0", "0.0.9"]
    # 取得物が壊れていたら戻さない
    updater.reset()
    (d / "QnapNewsHub_0.0.9_x86_64.qpkg").write_bytes(b"broken")
    assert client.post("/api/v1/update/rollback", json={"version": "0.0.9"}).status_code == 200
    assert wait_job(client)["job"]["phase"] == "error"

    (d / "last.json").write_text(json.dumps({"kind": "update", "from": "0.0.9", "to": "0.1.0", "rc": 10, "at": "2026-11-24T01:02:03Z"}))
    r = client.get("/api/v1/update").json()["last"]
    assert r["ok"] and "更新しました" in r["message"]
    (d / "last.json").write_text(json.dumps({"kind": "update", "from": "0.1.0", "to": "0.1.1", "rc": 10, "at": "x"}))
    assert not client.get("/api/v1/update").json()["last"]["ok"]      # 版が変わっていない
    (d / "install.log").write_text("line1\nERROR: disk full\n")
    (d / "last.json").write_text(json.dumps({"kind": "update", "from": "0.1.0", "to": "0.1.1", "rc": 3, "at": "x"}))
    r = client.get("/api/v1/update").json()["last"]
    assert not r["ok"] and "失敗" in r["message"] and "disk full" in r["log"]
    diag = client.get("/api/v1/diagnostics").json()
    assert any(p["area"] == "update" for p in diag["problems"])
    (d / "last.json").write_text("{broken")
    assert client.get("/api/v1/update").json()["last"] is None


def test_child_script_writes_last_json(tmp_path, monkeypatch):
    """目的: 切り離した子の台本が、インストーラーの終了コード（QDK の成功は 10）を last.json に書き、ログを残す。実際に sh で流す。"""
    d = tmp_path / "up"
    d.mkdir()
    fake = d / "fake.qpkg"
    fake.write_text("echo installing; exit 10\n")
    subprocess.run(["/bin/sh", "-c", updater._CHILD, "sh", str(fake), str(d / "install.log"), str(d / "last.json"), "update", "0.1.0", "0.1.1", ""], check=True, timeout=30)
    data = json.loads((d / "last.json").read_text())
    assert data["rc"] == 10 and data["kind"] == "update" and data["to"] == "0.1.1" and "installing" in (d / "install.log").read_text()
    monkeypatch.setattr(updater, "updates_dir", lambda: d)
    monkeypatch.setattr(updater, "current_version", lambda: "0.1.1")
    assert updater.last_result()["ok"] is True


def test_launch_is_detached_session(tmp_path, monkeypatch):
    """目的: インストーラーはサーバーと別のセッションの子（start_new_session）で流す。サーバーが止まっても道連れにならない。"""
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.update(kw=kw, cmd=cmd))
    (updater.updates_dir()).mkdir(parents=True)
    q = updater.updates_dir() / "QnapNewsHub_0.1.1_x86_64.qpkg"
    q.write_bytes(b"x")
    updater._launch(q, "update", "0.1.0", "0.1.1")
    assert seen["kw"]["start_new_session"] is True and seen["cmd"][0] == "/bin/sh"
    with pytest.raises(updater.UpdateError):
        updater._launch(q, "update", "0.1.0", "0.1.1; reboot")
    with pytest.raises(updater.UpdateError):
        updater._launch(tmp_path / "x.qpkg", "update", "0.1.0", "0.1.1")
