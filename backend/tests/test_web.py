"""目的: build した画面（frontend/dist）をバックエンドが `/` 以下で配ること、`/api` は画面に飲まれないこと、置き場の外のファイルを返さないことの確かめ。
根拠: 設計 2.1（画面は静的な build を 1 プロセスで配る）・AGENTS.md 絶対ルール 3（パスは resolve() して許した置き場の中か確かめる）・2 段（画面）の完了の条件。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>ニュースハブ</title><div id=root></div>", encoding="utf-8")
    (root / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("秘密", encoding="utf-8")
    monkeypatch.setenv("NEWSHUB_WEB", str(root))
    from qnapnewshub.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_index_assets_and_spa_fallback(web_client):
    r = web_client.get("/")
    assert r.status_code == 200 and "ニュースハブ" in r.text and r.headers["cache-control"] == "no-cache"
    assert r.headers["x-content-type-options"] == "nosniff"
    asset = web_client.get("/assets/app-abc123.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert web_client.get("/favicon.svg").status_code == 200
    # 画面の中の道（/sources・/settings）は index.html を返す（再読み込みで 404 にしない）
    assert "ニュースハブ" in web_client.get("/sources").text and "ニュースハブ" in web_client.get("/settings").text
    assert web_client.get("/assets/missing.js").status_code == 404


def test_api_is_not_swallowed_and_traversal_is_refused(web_client):
    assert web_client.get("/api/v1/health").json()["status"] == "ok"
    missing = web_client.get("/api/v1/nope")
    assert missing.status_code == 404 and "<title>" not in missing.text
    assert web_client.get("/api").status_code == 404
    for path in ("/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
        r = web_client.get(path)
        assert "秘密" not in r.text


def test_without_build_the_api_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSHUB_WEB", str(tmp_path / "nothing"))
    from qnapnewshub.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/v1/health").status_code == 200
        assert c.get("/").status_code == 404
