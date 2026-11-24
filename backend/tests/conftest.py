"""試験の共通の準備。外へ通信しない・本物のデータの置き場を使わない。

- 試験ごとに NEWSHUB_DATA を一時のフォルダへ（記事の DB・設定・認証・秘密がすべてそこ）。ユーザーの置き場（~/.local/share/qnapnewshub）には触れない。
- 取得の見張り（定期取得）は動かさない（NEWSHUB_AUTO=0。外へ通信するため）。
"""
from __future__ import annotations

import os
import tempfile

_boot = tempfile.mkdtemp(prefix="newshub-test-boot-")
os.environ["NEWSHUB_DATA"] = _boot
os.environ["NEWSHUB_AUTO"] = "0"
for _name in ("NEWSHUB_HOST", "NEWSHUB_PORT"):
    os.environ.pop(_name, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CSRF_HEADERS = {"X-Requested-With": "NewsHub"}
ADMIN_NAME = "admin"
ADMIN_PASSWORD = "-".join(["test", "password", "1234"])      # 見本の値は実行時に組み立てる（AGENTS.md 1）
USER_PASSWORD = "-".join(["news", "test", "password"])


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSHUB_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NEWSHUB_AUTO", "0")
    from qnapnewshub import auth, llm, service

    auth._failures.clear()  # noqa: SLF001
    llm.reset_minute_window()
    service._status.clear()
    service._last.update(started=0.0, finished=None, new=0)
    from qnapnewshub import metrics, rest, writer

    rest.invalidate()
    metrics.reset()
    yield
    writer.forget_all()      # 書き手のスレッドを止める（バッファの未保存分は試験ごとに捨てる）


@pytest.fixture()
def anon_client(isolated_data):
    from qnapnewshub.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client(anon_client):
    """管理者を作ってログイン済みの client。"""
    r = anon_client.post("/api/v1/auth/setup", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return anon_client


@pytest.fixture()
def admin_client(client):
    return client


def user_id(name: str = "admin") -> int:
    """利用者の id（なければ作る）。記事の操作・好みの試験が、利用者ごとの分けを確かめるのに使う。"""
    from qnapnewshub import auth

    row = auth._db().execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()  # noqa: SLF001
    return row[0] if row else auth.create_user(name, USER_PASSWORD)


def login(client, name: str = "admin", role: str = ""):
    """その利用者でログインし直した client を返す（cookie を入れ替える）。"""
    user_id(name)
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"username": name, "password": ADMIN_PASSWORD if name == ADMIN_NAME else USER_PASSWORD})
    assert r.status_code == 200, r.text
    return client
