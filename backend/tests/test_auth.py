"""目的: 認証（最初の管理者・cookie の session・API トークン）と、API ごとの認証の確かめの試験。
根拠: AGENTS.md 絶対ルール 5（API ごとに認証と権限）・4（秘密は保存するとき暗号化／ハッシュ）。設計 2.3: 最初の起動で管理者を作る・
ControlDeck の同期は API トークン（取り消せる）・既定は 127.0.0.1 で待ち受け。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from qnapnewshub import auth, config, hubconf
from tests.conftest import ADMIN_NAME, ADMIN_PASSWORD, USER_PASSWORD

READ_APIS = ["/api/v1/news/items", "/api/v1/news/home", "/api/v1/news/sources", "/api/v1/news/sources/manage", "/api/v1/news/channels", "/api/v1/news/settings",
             "/api/v1/news/digests", "/api/v1/news/prefs", "/api/v1/news/archive", "/api/v1/news/trends", "/api/v1/news/profile", "/api/v1/news/presets",
             "/api/v1/news/export"]


def test_first_start_has_no_user_and_setup_creates_the_admin_once(anon_client):
    """最初の起動は管理者がいない。setup で作ると、そのままログインした状態になる。2 回目は 409（乗っ取りの防止）。"""
    assert anon_client.get("/api/v1/auth/status").json() == {"setup_needed": True, "setup_token_required": False, "authenticated": False, "username": None}
    assert anon_client.get("/api/v1/news/items").status_code == 401
    for bad in ({"username": "ab", "password": ADMIN_PASSWORD}, {"username": "admin", "password": "short"}, {"username": "bad name!", "password": ADMIN_PASSWORD}):
        assert anon_client.post("/api/v1/auth/setup", json=bad).status_code == 422
    r = anon_client.post("/api/v1/auth/setup", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD})
    assert r.status_code == 200 and anon_client.get("/api/v1/auth/status").json() == {"setup_needed": False, "setup_token_required": False, "authenticated": True, "username": ADMIN_NAME}
    assert anon_client.get("/api/v1/news/items").status_code == 200
    other = TestClient(anon_client.app)
    assert other.post("/api/v1/auth/setup", json={"username": "intruder", "password": USER_PASSWORD}).status_code == 409
    assert other.get("/api/v1/auth/status").json()["authenticated"] is False


def test_cookie_is_httponly_samesite_and_login_logout(anon_client):
    anon_client.post("/api/v1/auth/setup", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD})
    fresh = TestClient(anon_client.app)
    r = fresh.post("/api/v1/auth/login", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code == 200 and "httponly" in cookie and "samesite=lax" in cookie and auth.COOKIE in cookie
    assert fresh.get("/api/v1/news/items").status_code == 200
    assert fresh.post("/api/v1/auth/logout").status_code == 200
    assert fresh.get("/api/v1/news/items").status_code == 401           # ログアウトで session が無効（サーバー側でも消える）


def test_login_failures_do_not_say_which_part_is_wrong_and_are_throttled(anon_client):
    """目的: 利用者名・パスワードのどちらが違うかを言わない。同じ IP からの失敗が続いたら 429（総当たりの対策）。"""
    anon_client.post("/api/v1/auth/setup", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD})
    c = TestClient(anon_client.app)
    wrong_pw = c.post("/api/v1/auth/login", json={"username": ADMIN_NAME, "password": "x" * 14})
    wrong_user = c.post("/api/v1/auth/login", json={"username": "nobody", "password": ADMIN_PASSWORD})
    assert wrong_pw.status_code == wrong_user.status_code == 401 and wrong_pw.json() == wrong_user.json()
    for _ in range(auth.LOGIN_MAX_FAILURES):
        c.post("/api/v1/auth/login", json={"username": ADMIN_NAME, "password": "y" * 14})
    assert c.post("/api/v1/auth/login", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD}).status_code == 429       # 正しくても待たせる


def test_passwords_and_session_and_token_are_stored_hashed_only(client):
    """目的: パスワード・session の印・API トークンは、保存先に平文で無い（ハッシュだけ）。
    根拠: AGENTS.md 絶対ルール 4。DB のファイルが漏れても、そのままではログインできない。"""
    created = client.post("/api/v1/auth/tokens", json={"name": "ControlDeck"}).json()
    session = client.cookies.get(auth.COOKIE)
    dump = ""
    with sqlite3.connect(config.data_dir() / "auth.sqlite3") as conn:
        for table in ("users", "sessions", "api_tokens"):
            dump += str(conn.execute(f"SELECT * FROM {table}").fetchall())
    assert ADMIN_PASSWORD not in dump and created["token"] not in dump and session not in dump
    assert auth.verify_password(ADMIN_PASSWORD, auth._db().execute("SELECT pw_hash FROM users").fetchone()[0])      # noqa: SLF001
    assert (config.data_dir() / "auth.sqlite3").stat().st_mode & 0o077 == 0         # 他の利用者から読めない（0600）


def test_every_news_api_needs_authentication(anon_client):
    """目的: ニュースの API は、認証なしでは全部 401（管理者がまだいなくても）。health だけが公開。
    根拠: AGENTS.md 絶対ルール 5。"""
    assert anon_client.get("/api/v1/health").json()["status"] == "ok"
    for path in READ_APIS:
        assert anon_client.get(path).status_code == 401, path
    for method, path in (("post", "/api/v1/news/refresh"), ("put", "/api/v1/news/sources"), ("put", "/api/v1/news/channels"), ("put", "/api/v1/news/settings"),
                         ("put", "/api/v1/news/openrouter-key"), ("get", "/api/v1/news/openrouter-key"), ("delete", "/api/v1/news/openrouter-key"),
                         ("post", "/api/v1/news/digests/run"), ("post", "/api/v1/news/items/1/action"), ("put", "/api/v1/news/prefs"),
                         ("post", "/api/v1/news/sources/discover"), ("post", "/api/v1/news/sources/check"), ("post", "/api/v1/news/sources/manage/bulk"),
                         ("get", "/api/v1/auth/tokens"), ("post", "/api/v1/auth/tokens"), ("delete", "/api/v1/auth/tokens/1"), ("get", "/api/v1/auth/hub"),
                         ("put", "/api/v1/auth/hub")):
        assert getattr(anon_client, method)(path).status_code in (401, 422), (method, path)
        assert getattr(anon_client, method)(path).status_code != 200


def test_api_token_is_read_only_revocable_and_shown_once(client):
    """目的: ControlDeck 用の API トークン: 作った応答でだけ本体が見える・読み取りだけ（GET）・取り消すと使えない・一覧は本体を返さない。
    根拠: 設計 2.3・3.1（ControlDeck の同期はトークン。取り消せる）。漏れても書き込み・設定の変更・トークンの発行はできない。"""
    made = client.post("/api/v1/auth/tokens", json={"name": "ControlDeck"}).json()
    assert made["token"].startswith(auth.TOKEN_PREFIX) and made["hint"] == made["token"][:9] + "…"
    listed = client.get("/api/v1/auth/tokens").json()["tokens"]
    assert listed[0]["name"] == "ControlDeck" and "token" not in listed[0] and made["token"] not in str(listed)
    bearer = {"Authorization": f"Bearer {made['token']}"}
    anon = TestClient(client.app)
    for path in READ_APIS:
        assert anon.get(path, headers=bearer).status_code == 200, path
    assert anon.post("/api/v1/news/refresh", headers=bearer).status_code == 403
    assert anon.post("/api/v1/news/items/1/action", json={"action": "like"}, headers=bearer).status_code == 403
    assert anon.get("/api/v1/auth/tokens", headers=bearer).status_code == 403            # トークンの管理は session だけ
    assert anon.post("/api/v1/auth/tokens", json={"name": "x"}, headers=bearer).status_code == 403
    assert anon.put("/api/v1/news/openrouter-key", json={"key": "k" * 20}, headers=bearer).status_code == 403
    assert anon.get("/api/v1/news/openrouter-key", headers=bearer).status_code == 403
    assert anon.get("/api/v1/news/items", headers={"Authorization": "Bearer nhub_wrong"}).status_code == 401
    assert client.get("/api/v1/auth/tokens").json()["tokens"][0]["last_used_at"] != ""        # 使った時刻が残る
    assert client.delete(f"/api/v1/auth/tokens/{made['id']}").status_code == 200
    assert anon.get("/api/v1/news/items", headers=bearer).status_code == 401                # 取り消したら即使えない
    assert client.delete(f"/api/v1/auth/tokens/{made['id']}").status_code == 404
    assert client.get("/api/v1/auth/tokens").json()["tokens"][0]["revoked"] is True
    assert client.post("/api/v1/auth/tokens", json={"name": "  "}).status_code == 422


def test_cross_site_writes_with_the_cookie_are_refused(client):
    """目的: cookie を付けたブラウザの書き込みは、Origin が同じホストのときだけ（別のサイトの画面から勝手に押させない）。
    根拠: AGENTS.md 絶対ルール 5。SameSite=Lax に加えた二重の備え。"""
    bad = client.post("/api/v1/news/refresh", headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    ok = client.post("/api/v1/news/digests/run", json={"period": "monthly"}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 422             # 認証・Origin は通り、入力の検査で断られる


def test_listens_on_localhost_by_default_and_can_be_widened_in_settings(client, monkeypatch):
    """目的: 既定の待ち受けは 127.0.0.1。設定（管理者だけ）で 0.0.0.0 にできる。それ以外の値は断る。環境変数が設定より優先。
    根拠: 設計 2.3（既定は LAN の中だけ。0.0.0.0 にするのは利用者の判断）。"""
    monkeypatch.delenv("NEWSHUB_HOST", raising=False)
    monkeypatch.delenv("NEWSHUB_PORT", raising=False)
    assert config.listen() == ("127.0.0.1", 8787) and hubconf.load()["host"] == "127.0.0.1"
    assert client.put("/api/v1/auth/hub", json={"host": "192.0.2.1", "port": 8787}).status_code == 422
    assert client.put("/api/v1/auth/hub", json={"host": "0.0.0.0", "port": 80}).status_code == 422          # 1024 未満は不可
    ok = client.put("/api/v1/auth/hub", json={"host": "0.0.0.0", "port": 8800})
    assert ok.status_code == 200 and ok.json()["restart_needed"] is True and config.listen() == ("0.0.0.0", 8800)
    monkeypatch.setenv("NEWSHUB_HOST", "127.0.0.1")
    assert config.listen()[0] == "127.0.0.1"


def test_data_dir_follows_the_environment_and_rejects_outside_paths(tmp_path, monkeypatch):
    """目的: データの置き場は NEWSHUB_DATA。置き場の外のパスは inside() が断る（resolve() で .. や symlink を正規化してから）。
    根拠: AGENTS.md 絶対ルール 3。"""
    monkeypatch.setenv("NEWSHUB_DATA", str(tmp_path / "d"))
    root = config.data_dir()
    assert root == (tmp_path / "d").resolve()
    assert config.inside(root / "news" / "x.json") == (root / "news" / "x.json")
    with pytest.raises(ValueError):
        config.inside(root / ".." / "elsewhere")
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(ValueError):
        config.inside(link / "x")


def test_every_registered_route_is_authenticated_except_the_public_ones(anon_client):
    """目的: 登録された API の全部（新しく足したものも）が、認証なしで 401 になること（公開は health・auth の status/setup/login だけ）。足し忘れを機械で検出する。
    根拠: AGENTS.md 絶対ルール 5（API ごとに認証を確かめる）。"""
    public = {("GET", "/api/v1/health"), ("GET", "/api/v1/auth/status"), ("POST", "/api/v1/auth/setup"), ("POST", "/api/v1/auth/login")}
    checked = 0
    from qnapnewshub import auth_router, router

    routes = [(r, "/api/v1" + r.path) for mod in (auth_router, router) for r in mod.router.routes] + [(None, "/api/v1/health")]
    for route, path in routes:
        if route is None:
            continue
        concrete = path.replace("{item_id}", "1").replace("{digest_id}", "1").replace("{token_id}", "1")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, path) in public:
                continue
            r = anon_client.request(method, concrete)
            assert r.status_code == 401, (method, path, r.status_code)
            checked += 1
    assert checked >= 35


def test_concurrent_requests_do_not_share_one_sqlite_connection(client):
    """目的: 同時に来た認証つきの GET が、1 本の sqlite 接続を取り合って 500（InterfaceError）にならない。
    根拠: 2026-11-24 の E2E（実際のバックエンド）で、設定の画面が開く瞬間の同時の GET が 500・401 になった（接続を全スレッドで共有していた）。"""
    from concurrent.futures import ThreadPoolExecutor

    def hit(_):
        return client.get("/api/v1/news/openrouter-key").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(hit, range(80)))
    assert set(codes) == {200}


def test_setup_needs_the_one_time_token_when_listening_on_the_lan(isolated_data, monkeypatch):
    """目的: QPKG（NEWSHUB_SETUP_TOKEN=1）では、置き場の setup-token.txt の番号を知っている人だけが最初の管理者を作れる。
    根拠: QPKG は LAN に待ち受けるので、番号が無いと LAN の誰でも先に管理者を作って乗っ取れる（2026-11-24 の設計）。
    番号はログに出さず、ファイルは 0600、管理者を作ったら消す。違う番号は 403 で、総当たりは制限される。"""
    import os
    from qnapnewshub.main import app

    monkeypatch.setenv("NEWSHUB_SETUP_TOKEN", "1")
    with TestClient(app) as c:
        path = auth.setup_token_path()
        assert path.is_file() and (os.stat(path).st_mode & 0o777) == 0o600
        token = path.read_text(encoding="utf-8").strip()
        assert len(token) == 14 and token.count("-") == 2
        assert c.get("/api/v1/auth/status").json()["setup_token_required"] is True
        body = {"username": ADMIN_NAME, "password": ADMIN_PASSWORD}
        assert c.post("/api/v1/auth/setup", json=body).status_code == 403
        assert c.post("/api/v1/auth/setup", json={**body, "setup_token": "AAAA-BBBB-CCCC"}).status_code == 403
        assert not auth.has_users()
        r = c.post("/api/v1/auth/setup", json={**body, "setup_token": token.lower()})      # 大文字小文字は問わない
        assert r.status_code == 200, r.text
        assert not path.exists()
        assert c.get("/api/v1/auth/status").json()["setup_token_required"] is False
        from qnapnewshub import logbuf
        assert token not in json_dump(logbuf)


def test_wrong_setup_tokens_are_throttled(isolated_data, monkeypatch):
    """目的: 番号の総当たりを止める。根拠: 番号は 32 文字 × 12 桁だが、LAN から無制限に試せると守りにならない。"""
    from qnapnewshub.main import app

    monkeypatch.setenv("NEWSHUB_SETUP_TOKEN", "1")
    with TestClient(app) as c:
        body = {"username": ADMIN_NAME, "password": ADMIN_PASSWORD, "setup_token": "AAAA-BBBB-CCCC"}
        codes = [c.post("/api/v1/auth/setup", json=body).status_code for _ in range(auth.LOGIN_MAX_FAILURES + 2)]
        assert codes[0] == 403 and codes[-1] == 429


def json_dump(logbuf_module) -> str:
    import json
    try:
        return json.dumps(logbuf_module.recent(500), ensure_ascii=False)
    except AttributeError:
        raise
