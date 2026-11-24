"""目的: OpenRouter の鍵の暗号化した保存（secretbox）と、設定の API で鍵を入れる・消す・確かめる流れの試験。
根拠: AGENTS.md 絶対ルール 4（秘密は保存するとき暗号化・ログ・画面に出さない）。設計 2.4（鍵は暗号化して保存。鍵の本文は画面・ログに出さない）。
"""
from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from qnapnewshub import config, llm, secretbox
from tests.test_news_llm_prep import fake_key


def test_secret_is_encrypted_on_disk_with_aes_gcm_and_files_are_private():
    """鍵は secrets.json に暗号文として入り、平文がファイルのどこにも無い。暗号化の鍵（secret.key）は 0600。"""
    secretbox.set_secret("openrouter_key", fake_key())
    root = config.data_dir()
    blob = (root / "secrets.json").read_text(encoding="utf-8")
    assert fake_key() not in blob and "kkkkkkkk" not in blob and json.loads(blob)["openrouter_key"]["v"] == 1
    assert fake_key().encode() not in (root / "secret.key").read_bytes()
    for name in ("secrets.json", "secret.key"):
        assert stat.S_IMODE(os.stat(root / name).st_mode) == 0o600, name
    assert (root / "secret.key").stat().st_size == 32
    assert secretbox.get_secret("openrouter_key") == fake_key() and secretbox.has_secret("openrouter_key")


def test_same_secret_encrypts_differently_each_time_and_wrong_slot_or_key_cannot_open():
    """同じ鍵でも暗号文は毎回違う（nonce）。別の名前の枠へ貼り替えても、暗号化の鍵が変わっても開けない（改ざんの検知）。"""
    secretbox.set_secret("a", "same value")
    first = json.loads((config.data_dir() / "secrets.json").read_text())["a"]["c"]
    secretbox.set_secret("a", "same value")
    assert json.loads((config.data_dir() / "secrets.json").read_text())["a"]["c"] != first
    table = json.loads((config.data_dir() / "secrets.json").read_text())
    table["b"] = table["a"]
    (config.data_dir() / "secrets.json").write_text(json.dumps(table))
    with pytest.raises(secretbox.SecretError):
        secretbox.get_secret("b")                                    # 名前を認証付きデータに入れているので貼り替えは失敗
    (config.data_dir() / "secret.key").write_bytes(os.urandom(32))   # 鍵ファイルが入れ替わった
    with pytest.raises(secretbox.SecretError):
        secretbox.get_secret("a")
    assert secretbox.get_secret("missing") is None


def test_delete_secret():
    secretbox.set_secret("x", "v")
    assert secretbox.delete_secret("x") is True and secretbox.get_secret("x") is None and secretbox.delete_secret("x") is False


def test_key_api_stores_encrypted_and_never_returns_the_key(client, caplog):
    """目的: 設定の API で鍵を入れる・確かめる・消す。応答・ログ・設定の画面の値に鍵の本文は出ない。形が違う鍵は 422。
    根拠: AGENTS.md 絶対ルール 4。ControlDeck からの受け取り（ペアリング）は 4 段なので、いまは手で入れる道だけ。"""
    assert client.get("/api/v1/news/openrouter-key").json() == {"set": False}
    for bad in ("short", "has space in it 12345", "日本語の鍵ではない" * 3):
        assert client.put("/api/v1/news/openrouter-key", json={"key": bad}).status_code == 422
    with caplog.at_level(logging.DEBUG):
        put = client.put("/api/v1/news/openrouter-key", json={"key": fake_key()})
        got = client.get("/api/v1/news/openrouter-key")
        settings = client.get("/api/v1/news/settings")
    assert put.json() == {"set": True} and got.json() == {"set": True} and settings.json()["llm_stats"]["key_set"] is True
    assert fake_key() not in put.text + got.text + settings.text + caplog.text
    assert fake_key() not in (config.data_dir() / "secrets.json").read_text()
    assert llm.key_is_set()
    assert client.delete("/api/v1/news/openrouter-key").json() == {"set": False}
    assert not llm.key_is_set() and client.get("/api/v1/news/openrouter-key").json() == {"set": False}


def test_validation_error_does_not_echo_the_key(client):
    """形が違う鍵の 422 の応答に、入れた文字列を含めない（入力の誤りで鍵が応答・ログへ写らない）。"""
    r = client.put("/api/v1/news/openrouter-key", json={"key": "x y " * 5})
    assert r.status_code == 422 and "x y x y" not in r.text
    r2 = client.put("/api/v1/news/openrouter-key", json={"key": ""})
    assert r2.status_code == 422
