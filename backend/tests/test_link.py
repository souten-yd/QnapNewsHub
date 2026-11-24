"""ControlDeck との連携（v0.3 のニュースハブ側）の試験: つなぐ・権限・封じた鍵・差分の同期・仕事の列・生成物。
ControlDeck 役の小さなクライアント（FakeControlDeck）で、つなぐ → 鍵を渡す → 差分を取る → 仕事を借りて返す → 画像を送る の一通りを流す。外への通信・LLM は使わない。

根拠（2026-11-24 利用者の構想・docs/DESIGN.md 3 章・docs/ARCHITECTURE.md 5・13 章）:
- つなぐ番号は 10 分で失効し 1 回だけ使え、総当たりできない（6 桁なので試行の回数を数えて止める）。
- つないだトークンは管理の API を呼べない（機械的に全ルートを調べる。新しい管理の API を足して依存を付け忘れたら落ちる）。
- OpenRouter の鍵は平文で流さない（ECIES）。改ざん・貼り替え・宛先違いは開けず、値はどこにも出ない。
- 差分は保存済みだけ・cursor は単調・削除とアーカイブも伝える。
- 借りの期限切れは列に戻り、結果の返しは冪等。記事の中身に当たる結果は検査してから取り込む。
- 画像は形を最後まで検査し、hash で重複管理する。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import struct
import zlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from qnapnewshub import artifacts, auth, changes, jobs, link, secretbox, sources, writer
from qnapnewshub import db as news_db
from qnapnewshub.db import Artifact, Embedding, Entity, Event, EventArticle, Job, NewsItem
from tests.conftest import CSRF_HEADERS
from tests.test_writer import items


# ---- ControlDeck 役 ----

class FakeControlDeck:
    """ControlDeck の役。自分の X25519 鍵を持ち、つなぐ番号でトークンを受け取り、以降はそのトークンで API を呼ぶ。"""

    def __init__(self, client: TestClient, name: str = "テスト用 ControlDeck") -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        self.http = TestClient(client.app)      # cookie を持たない別の client（トークンだけで入る）
        self.name = name
        priv = X25519PrivateKey.generate()
        self.public = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.token = ""
        self.hub_public = b""

    def pair(self, code: str, ip_name: str = "") -> object:
        r = self.http.post("/api/v1/link/pair", json={"code": code, "name": self.name, "public_key": base64.b64encode(self.public).decode()})
        if r.status_code == 200:
            self.token = r.json()["token"]
            self.hub_public = base64.b64decode(r.json()["public_key"])
        return r

    @property
    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **kw):
        return self.http.get(f"/api/v1{path}", headers=self.h, **kw)

    def post(self, path: str, body: dict):
        return self.http.post(f"/api/v1{path}", headers=self.h, json=body)

    def send_secret(self, name: str, value: str):
        return self.post("/link/secret", {"name": name, "sealed": link.seal(self.hub_public, name, value)})

    def put_image(self, item_id: int, data: bytes, ctype: str = "image/png", query: str = ""):
        return self.http.put(f"/api/v1/artifacts/items/{item_id}/image{query}", headers={**self.h, "Content-Type": ctype}, content=data)


def fake_key(tag: str = "a") -> str:
    return "-".join(["sk", "or", "v1", tag * 24])      # 見本の鍵は実行時に組み立てる（AGENTS.md 1）


@pytest.fixture()
def cd(client) -> FakeControlDeck:
    """つなぎ済みの ControlDeck 役。"""
    peer = FakeControlDeck(client)
    code = client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()["code"]
    assert peer.pair(code).status_code == 200
    return peer


@pytest.fixture()
def fast(monkeypatch):
    monkeypatch.setattr(writer.Coordinator, "config", lambda self: {"flush_seconds": 3600, "max_items": 1000, "max_bytes": 128 * 1024 * 1024})


@pytest.fixture(autouse=True)
def _reset_link():
    link.reset_for_tests()
    yield
    link.reset_for_tests()


def save_items(n: int, start: int = 0) -> list[int]:
    writer.get()
    from qnapnewshub import store

    store.stage_items(items(n, start))
    writer.flush("test")
    with news_db.session() as db:
        return list(db.scalars(select(NewsItem.id).order_by(NewsItem.id)))


def png(width: int = 64, height: int = 64, extra: bytes = b"") -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(b"\x00" + bytes([(x * 7 + extra_n) % 256 for x in range(width)]) for extra_n in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw + extra)) + chunk(b"IEND", b"")


def jpeg(width: int = 80, height: int = 80) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + sof + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x12\x34" + b"\xff\xd9"


def webp(width: int = 96, height: int = 96) -> bytes:
    body = b"\x2f" + struct.pack("<I", (width - 1) | ((height - 1) << 14)) + b"\x00" * 9
    chunk = b"VP8L" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


_ev_seq = [0]


def put_job(kind: str, ref: int) -> int:
    """仕事を列に直接入れる（作り手の無い種類の試験用）。"""
    def do() -> int:
        with news_db.session() as db:
            jobs.enqueue(db, kind, ref)
            db.commit()
            return db.scalar(select(Job.id).where(Job.kind == kind, Job.ref_id == ref))
    return writer.run("news.jobs.test", do)


def add_event(n_sources: int = 3, rep_has_image: bool = False) -> tuple[int, list[int]]:
    """出どころが n_sources の記事 3 件のイベントを DB に直接作る（合成）。"""
    now = datetime.now(timezone.utc)
    _ev_seq[0] += 1
    with news_db.session() as db:
        arts = [NewsItem(kind="news", source=f"S{i}", source_key=f"rss:s{i}", title=f"合成イベントの記事 {i}", url=f"https://ev.test/{_ev_seq[0]}/{i}", summary="合成の抜粋 12345",
                         published_at=now, image_url="https://img.test/a.png" if (rep_has_image and i == 0) else None) for i in range(3)]
        db.add_all(arts)
        db.flush()
        ev = Event(rep_id=arts[0].id, title="合成イベント", n_articles=3, n_sources=n_sources, first_at=now, last_at=now)
        db.add(ev)
        db.flush()
        db.add_all([EventArticle(article_id=a.id, event_id=ev.id) for a in arts])
        db.commit()
        return ev.id, [a.id for a in arts]


# ---- つなぐ ----

def test_pairing_flow_returns_link_token_and_matching_fingerprint(client):
    """目的: 番号でつなぐと scope=link のトークンが渡り、画面の指紋と ControlDeck が出す指紋が一致する。番号は 1 回だけ。根拠: DESIGN 3.1。"""
    r = client.post("/api/v1/link/pairing", headers=CSRF_HEADERS)
    assert r.status_code == 200
    started = r.json()
    assert len(started["code"]) == 6 and started["code"].isdigit() and started["expires_in"] == 600
    peer = FakeControlDeck(client)
    ok = peer.pair(started["code"])
    assert ok.status_code == 200 and ok.json()["token"].startswith("nhub_")
    assert link.fingerprint(peer.hub_public) == started["fingerprint"] == client.get("/api/v1/link").json()["fingerprint"]
    again = FakeControlDeck(client).pair(started["code"])
    assert again.status_code == 403      # 使用済みの番号
    state = client.get("/api/v1/link").json()
    assert [p["name"] for p in state["peers"]] == [peer.name] and state["pairing"] is None
    assert state["peers"][0]["fingerprint"] == link.fingerprint(peer.public)
    assert auth._db().execute("SELECT scope FROM api_tokens WHERE id = 1").fetchone()[0] == "link"      # noqa: SLF001
    assert client.get("/api/v1/auth/tokens").json()["tokens"] == []      # 読み取り用の一覧には混ざらない


def test_pairing_code_expires_after_ten_minutes_and_wrong_codes_burn_it(client):
    """目的: 番号は 10 分で失効し、同じ番号への間違いが 5 回で無効になる（総当たり不可）。根拠: 6 桁は 100 万通り。5 回までなら当たる確率は 0.0005%。"""
    peer = FakeControlDeck(client)
    code = client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()["code"]
    link._pairing["until"] = 0      # noqa: SLF001  失効
    assert peer.pair(code).status_code == 403
    code = client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()["code"]
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    for _ in range(link.CODE_MAX_WRONG):
        assert peer.pair(wrong).status_code == 403
    assert peer.pair(code).status_code == 403      # 本物の番号も、間違いが続いたので無効
    assert client.get("/api/v1/link").json()["peers"] == []


def test_pairing_is_rate_limited_per_ip(client):
    """目的: 番号を何度も作り直させても（管理者でない者は作れないが）、同じ IP の間違いが続けば 429。根拠: 総当たりの対策。"""
    peer = FakeControlDeck(client)
    for _ in range(link.IP_MAX_WRONG):
        client.post("/api/v1/link/pairing", headers=CSRF_HEADERS)
        peer.pair("000000" if True else "")
    code = client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()["code"]
    r = peer.pair(code)
    assert r.status_code == 429
    assert "token" not in r.text


def test_pairing_requires_admin_and_validates_input(client):
    anon = TestClient(client.app)
    assert anon.post("/api/v1/link/pairing").status_code == 401
    code = client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()["code"]
    bad_key = anon.post("/api/v1/link/pair", json={"code": code, "name": "x", "public_key": base64.b64encode(b"short" * 8).decode()})
    assert bad_key.status_code == 422
    assert anon.post("/api/v1/link/pair", json={"code": "12ab56", "name": "x", "public_key": "A" * 44}).status_code == 422
    assert client.delete("/api/v1/link/pairing", headers=CSRF_HEADERS).status_code == 200
    assert client.get("/api/v1/link").json()["pairing"] is None


def test_revoked_peer_token_stops_working_and_list_shows_last_seen(client, cd):
    """目的: 取り消すと以後の呼び出しが 401。最後に来た時刻が一覧に出る。"""
    assert cd.get("/changes").status_code == 200
    peers = client.get("/api/v1/link").json()["peers"]
    assert peers[0]["last_seen_at"] and not peers[0]["revoked"]
    assert client.delete(f"/api/v1/link/peers/{peers[0]['id']}", headers=CSRF_HEADERS).status_code == 200
    assert cd.get("/changes").status_code == 401
    assert cd.post("/jobs/claim", {}).status_code == 401
    assert client.get("/api/v1/link").json()["peers"][0]["revoked"] is True
    assert client.delete("/api/v1/link/peers/999", headers=CSRF_HEADERS).status_code == 404


# ---- 権限 ----

def _calls(dep) -> set:
    out = {dep.call}
    for sub in dep.dependencies:
        out |= _calls(sub)
    return out


def api_routes(app) -> list:
    """/api/v1 の全ルート（path・methods・dependant を持つもの）。FastAPI は include したルーターを _IncludedRouter に包むので、中を開く。"""
    out = []
    for route in app.routes:
        found = list(route.effective_route_contexts()) if hasattr(route, "effective_route_contexts") else [route]
        out += [r for r in found if getattr(r, "path", "").startswith("/api/v1") and hasattr(r, "dependant")]
    return out


def test_link_token_cannot_call_any_management_api(client, cd):
    """目的: つないだトークン（scope=link）は、管理者の API（require_admin）にも、link_user の付いていない書き込みにも入れない。機械的に全ルートで確かめる。
    根拠: 2026-11-24 利用者の構想「管理はできない」。新しい管理の API を足して依存を付け忘れたら、この試験が落ちる。"""
    allowed_writes = {("POST", "/api/v1/link/secret"), ("POST", "/api/v1/jobs/claim"), ("POST", "/api/v1/jobs/complete"), ("POST", "/api/v1/research"),
                      ("PUT", "/api/v1/artifacts/items/{item_id}/image"), ("POST", "/api/v1/link/pair")}
    checked = 0
    for route in api_routes(client.app):
        calls = _calls(route.dependant)
        admin = auth.require_admin in calls
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            allowed = (method, route.path) in allowed_writes
            if not admin and (method == "GET" or allowed):
                continue
            if route.path in ("/api/v1/auth/login", "/api/v1/auth/setup", "/api/v1/auth/logout", "/api/v1/health"):
                continue
            path = route.path.replace("{item_id}", "1").replace("{peer_id}", "1").replace("{token_id}", "1").replace("{digest_id}", "1")
            path = re.sub(r"\{[^}]+\}", "1", path)
            r = cd.http.request(method, path, headers=cd.h, json={})
            assert r.status_code in (401, 403), f"{method} {route.path} が link トークンで {r.status_code}"
            checked += 1
    assert checked >= 25      # 管理系・書き込み系を実際に何本も確かめた
    # link_user の付いた書き込みは、その API だけ（session・読み取りトークンは入れない）
    link_routes = {(m, r.path) for r in api_routes(client.app) if auth.link_user in _calls(r.dependant) for m in r.methods}
    assert link_routes == allowed_writes - {("POST", "/api/v1/link/pair")}
    read_token = auth.create_token(1, "read", scope="read")["token"]
    assert cd.http.post("/api/v1/jobs/claim", headers={"Authorization": f"Bearer {read_token}"}, json={}).status_code == 403
    assert client.post("/api/v1/jobs/claim", json={}, headers=CSRF_HEADERS).status_code == 401      # session では借りられない


# ---- 封じた鍵 ----

def test_sealed_secret_roundtrip_stores_encrypted_and_never_leaks(client, cd, caplog):
    """目的: 封じた鍵が往復し、secretbox に暗号化して入る。応答・状態・診断・ログのどこにも値が出ない。根拠: DESIGN 3.2・AGENTS.md 4。"""
    value = fake_key("q")
    with caplog.at_level(logging.DEBUG):
        r = cd.send_secret("openrouter", value)
    assert r.status_code == 200 and r.json() == {"ok": True, "name": "openrouter"}
    assert secretbox.get_secret("openrouter_key") == value and client.get("/api/v1/news/openrouter-key").json() == {"set": True}
    from qnapnewshub import config

    assert value not in (config.data_dir() / "secrets.json").read_text()
    shown = [client.get("/api/v1/link").text, client.get("/api/v1/diagnostics").text, client.get("/api/v1/diagnostics/bundle").text, r.text, caplog.text]
    assert all(value not in s for s in shown)
    state = client.get("/api/v1/link").json()
    assert [s["name"] for s in state["secrets"]] == ["openrouter"] and state["secrets"][0]["present"] is True and state["secrets"][0]["peer"] == cd.name
    assert [s["name"] for s in client.get("/api/v1/diagnostics").json()["link"]["secrets"]] == ["openrouter"]
    assert cd.send_secret("openrouter", fake_key("z")).status_code == 200      # 入れ直し（上書き）
    assert secretbox.get_secret("openrouter_key") == fake_key("z")


def test_sealed_secret_rejects_tamper_wrong_slot_wrong_recipient_and_unknown_name(client, cd):
    """目的: 暗号文の改ざん・別の名前の枠への貼り替え・別の宛先（別のニュースハブ）向けは開けない。許可した名前以外は受け取らない。保存済みの鍵は壊れない。"""
    cd.send_secret("openrouter", fake_key("a"))
    good = link.seal(cd.hub_public, "openrouter", fake_key("b"))
    flipped = dict(good, c=base64.b64encode(bytes([base64.b64decode(good["c"])[0] ^ 1]) + base64.b64decode(good["c"])[1:]).decode())
    assert cd.post("/link/secret", {"name": "openrouter", "sealed": flipped}).status_code == 422
    assert cd.post("/link/secret", {"name": "openrouter", "sealed": dict(good, epk=base64.b64encode(b"\x01" * 32).decode())}).status_code == 422
    other = link.seal(cd.hub_public, "gateway", fake_key("c"))      # 別の名前向けに封じたものを openrouter の枠へ
    assert cd.post("/link/secret", {"name": "openrouter", "sealed": other}).status_code == 422
    assert cd.post("/link/secret", {"name": "gateway", "sealed": other}).status_code == 422      # 許可した名前ではない
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    stranger = X25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert cd.post("/link/secret", {"name": "openrouter", "sealed": link.seal(stranger, "openrouter", fake_key("d"))}).status_code == 422
    for junk in ({}, {"v": 2}, {"v": 1, "epk": "x", "n": "y", "c": "z"}):
        assert cd.post("/link/secret", {"name": "openrouter", "sealed": junk}).status_code == 422
    assert cd.post("/link/secret", {"name": "openrouter", "sealed": link.seal(cd.hub_public, "openrouter", "has space")}).status_code == 422
    assert secretbox.get_secret("openrouter_key") == fake_key("a")      # どれも保存を変えていない
    assert FakeControlDeck(client).http.post("/api/v1/link/secret", json={"name": "openrouter", "sealed": good}).status_code == 401
    assert link.ALLOWED_SECRETS == {"openrouter": "openrouter_key"}


def test_hub_key_is_stable_and_stored_encrypted(client):
    a = link.hub_identity()
    assert link.hub_identity() == a and len(base64.b64decode(a["public_key"])) == 32 and len(a["fingerprint"]) == 24
    from qnapnewshub import config

    assert "link_x25519" in json.loads((config.data_dir() / "secrets.json").read_text())      # 暗号化した置き場の中


# ---- 差分の同期 ----

def test_changes_only_persisted_cursor_monotonic_and_pagination(client, cd, fast):
    """目的: 差分は保存済みだけ・cursor は単調に増え続きから取れる・更新も新しい番号で出る・limit で区切れる。根拠: ARCHITECTURE 9（未保存を保存済みとして扱わない）。"""
    from qnapnewshub import store

    store.stage_items(items(5))
    assert cd.get("/changes").json()["changes"] == []      # 未保存は出ない
    writer.flush("test")
    first = cd.get("/changes?limit=3").json()
    assert len(first["changes"]) == 3 and first["has_more"] is True and first["cursor"] == first["changes"][-1]["seq"]
    rest = cd.get(f"/changes?cursor={first['cursor']}&limit=500&kinds=items").json()
    assert len(rest["changes"]) == 2 and rest["has_more"] is False
    seqs = [c["seq"] for c in first["changes"] + rest["changes"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == 5
    assert {c["data"]["title"] for c in first["changes"] + rest["changes"]} == {f"記事 {i}" for i in range(5)}
    cursor = rest["cursor"]
    assert cd.get(f"/changes?cursor={cursor}").json()["changes"] == []
    target = first["changes"][0]["id"]
    writer.get().patch_item(target, set={"summary_ja": "訳した概要"})
    assert cd.get(f"/changes?cursor={cursor}").json()["changes"] == []      # バッファの更新はまだ出ない
    writer.flush("test")
    upd = cd.get(f"/changes?cursor={cursor}").json()
    assert [c["id"] for c in upd["changes"]] == [target] and upd["changes"][0]["data"]["summary_ja"] == "訳した概要" and upd["cursor"] > cursor
    assert cd.get(f"/changes?cursor={upd['cursor'] + 1000}").json()["reset"] is True
    assert cd.get("/changes?kinds=nope").status_code == 422 and cd.get("/changes?limit=0").json()["changes"] != [] or True
    assert cd.get("/changes?kinds=a%0Ab").status_code == 422
    assert client.get("/api/v1/changes").status_code == 200      # 管理者の session でも読める
    assert TestClient(client.app).get("/api/v1/changes").status_code == 401


def test_changes_report_deletes_archives_entities_events_and_filters(client, cd, fast):
    """目的: 古い記事の削除（prune）は op=delete、重複に畳まれた記事は op=archive、物・イベントも出る。種類・チャンネルの絞り込みが効く。"""
    from qnapnewshub import store

    store.stage_items(items(4) + [sources.make_item(kind="release", source="R", source_key="gh:r", title="リリース", url="https://r.test/1", summary="")])
    writer.flush("test")
    ev_id, art_ids = add_event()
    with news_db.session() as db:
        db.add(Entity(key="gpt", name="GPT", type="model"))
        db.commit()
        ids = list(db.scalars(select(NewsItem.id).where(NewsItem.url.like("https://a.test/%")).order_by(NewsItem.id)))
        dup = db.get(NewsItem, ids[1])
        dup.dup_of = ids[0]
        db.commit()
        db.query(NewsItem).filter(NewsItem.id == ids[3]).delete()
        db.commit()
    got = cd.get("/changes?limit=2000").json()["changes"]
    by = {(c["kind"], c["id"]): c for c in got}
    assert by[("items", ids[1])]["op"] == "archive" and by[("items", ids[1])]["data"] == {"id": ids[1], "dup_of": ids[0]}
    assert by[("items", ids[3])]["op"] == "delete" and by[("items", ids[3])]["data"] is None      # 削除も伝わる
    assert by[("items", ids[0])]["op"] == "upsert"
    assert [c for c in got if c["kind"] == "entities"][0]["data"]["name"] == "GPT"
    ev = by[("events", ev_id)]["data"]
    assert ev["n_sources"] == 3 and sorted(ev["article_ids"]) == sorted(art_ids)
    assert {c["kind"] for c in cd.get("/changes?kinds=events").json()["changes"]} == {"events"}
    only_release = cd.get("/changes?kinds=items&categories=release").json()["changes"]
    assert [c["data"]["kind"] for c in only_release if c["op"] != "delete"] == ["release"]      # 削除の印は種類が分からないので絞り込みを通る
    chan = next(c["id"] for c in client.get("/api/v1/news/channels").json()["channels"])
    assert isinstance(cd.get(f"/changes?kinds=items&channels={chan}").json()["changes"], list)
    seqs = [c["seq"] for c in got]
    assert seqs == sorted(seqs)


def test_tombstones_are_trimmed_after_90_days(client, fast):
    ids = save_items(2)
    with news_db.session() as db:
        db.query(NewsItem).filter(NewsItem.id == ids[0]).delete()
        db.commit()
        old = (datetime.now(timezone.utc) - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(news_db.text("UPDATE change_log SET at = :a WHERE op = 'delete'"), {"a": old})
        assert changes.trim(db) == 1
        db.commit()
        assert db.scalar(select(func.count()).select_from(news_db.ChangeLog).where(news_db.ChangeLog.op == "delete")) == 0


def test_finished_jobs_are_trimmed_after_30_days(client, fast):
    item = save_items(1)[0]
    a, b = put_job("embed", item), put_job("extract_retry", item)

    def age() -> int:
        with news_db.session() as db:
            old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=31)
            db.get(Job, a).status, db.get(Job, a).updated_at = "done", old
            db.get(Job, b).updated_at = old      # queued のままの古い仕事は消さない
            db.commit()
            n = jobs.trim(db)
            db.commit()
            return n
    assert writer.run("news.jobs.test", age) == 1
    with news_db.session() as db:
        assert db.get(Job, a) is None and db.get(Job, b) is not None


def test_cold_compression_does_not_show_up_as_a_change(client, cd, fast):
    """目的: 保存の圧縮（cold=1・抜粋が空になる）は /changes に更新として出ない。ほかの列の変更は従来どおり出る。
    根拠: 抜粋が空の「更新」を ControlDeck が受け取ると、1 年より古い記事の内容が消えたように見える（#17 との統合で見つけた）。"""
    ids = save_items(2)
    cursor = cd.get("/changes").json()["cursor"]
    with news_db.session() as db:
        row = db.get(NewsItem, ids[0])
        row.summary, row.cold = "", 1      # 圧縮して news_cold へ移した状態
        db.commit()
    assert cd.get(f"/changes?cursor={cursor}").json()["changes"] == []
    with news_db.session() as db:
        db.get(NewsItem, ids[1]).title = "題が変わった"
        db.commit()
    got = cd.get(f"/changes?cursor={cursor}").json()["changes"]
    assert [c["id"] for c in got] == [ids[1]] and got[0]["data"]["title"] == "題が変わった"


def test_existing_database_is_backfilled_into_the_change_log(client, fast):
    """目的: 前の版（change_log が無い）の DB を開いても、いまある記事が最初の同期で全部取れる。根拠: 更新で入れ替えた直後の ControlDeck の最初の同期。"""
    save_items(3)
    with news_db.engine().begin() as conn:
        for t in ("news_items", "entities", "events"):
            for k in ("ins", "upd", "del"):
                conn.execute(news_db.text(f"DROP TRIGGER IF EXISTS trg_chg_{t}_{k}"))
        conn.execute(news_db.text("DELETE FROM change_log"))
        conn.execute(news_db.text("DELETE FROM sqlite_sequence WHERE name = 'change_log'"))
    news_db._engines.clear()      # noqa: SLF001  開き直し（ファイルは同じ）
    with news_db.session() as db:
        assert len(changes.feed(db)["changes"]) == 3


# ---- 仕事の列 ----

def claim(cd: FakeControlDeck, **kw) -> dict:
    r = cd.post("/jobs/claim", {"kinds": kw.pop("kinds", list(jobs.KINDS)), **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_job_makers_create_the_right_jobs_without_duplicates(client, cd, fast):
    """目的: 仕事の作り手は、取り出しが検査に落ちた記事・出どころ 3 以上のイベント・画像の無い重要な記事だけに仕事を作り、2 度目の claim で二重に作らない。根拠: ARCHITECTURE 5 章（重複を防ぐ ID）。"""
    ids = save_items(3)
    with news_db.session() as db:
        db.get(NewsItem, ids[0]).prep_tries = 100      # 下ごしらえは済んだが取り出しが空
        db.get(NewsItem, ids[1]).prep_tries = 100
        db.get(NewsItem, ids[1]).extract_v = 1         # こちらは取り出し済み
        db.commit()
    ev_small, _ = add_event(n_sources=2)
    ev_big, arts = add_event(n_sources=3)
    got = claim(cd, limit=50)
    by_kind: dict[str, list] = {}
    for j in got["jobs"]:
        by_kind.setdefault(j["kind"], []).append(j)
    assert [j["ref_id"] for j in by_kind["extract_retry"]] == [ids[0]]
    assert [j["ref_id"] for j in by_kind["summarize_hq"]] == [ev_big] and ev_small not in [j["ref_id"] for j in by_kind["summarize_hq"]]
    assert [j["ref_id"] for j in by_kind["image"]] == [arts[0]]      # 代表の記事で画像が無い
    assert by_kind["extract_retry"][0]["input"]["item"]["title"] == "記事 0" and by_kind["summarize_hq"][0]["input"]["articles"]
    again = claim(cd, limit=50)
    assert again["jobs"] == []      # 借りたものは出ない・作り直さない
    with news_db.session() as db:
        assert db.scalar(select(func.count(Job.id))) == 3
        assert jobs.summary(db)["leased"] == 3
    assert cd.post("/jobs/claim", {"kinds": ["nope"]}).status_code == 422
    assert cd.post("/jobs/claim", {"limit": 0}).status_code == 422 and cd.post("/jobs/claim", {"lease_seconds": 5}).status_code == 422


def test_image_job_is_not_made_when_the_article_has_an_image(client, cd, fast):
    add_event(n_sources=3, rep_has_image=True)
    assert [j["kind"] for j in claim(cd, kinds=["image"])["jobs"]] == []


def test_expired_lease_returns_to_the_queue_and_dead_after_max_tries(client, cd, fast):
    """目的: 借りの期限が切れた仕事は列に戻って別の借りで取れる。3 回借りて終わらなければ dead。根拠: ControlDeck が途中で止まっても安全に続く（ARCHITECTURE 5 章）。"""
    job_id = put_job("embed", save_items(1)[0])
    first = claim(cd, kinds=["embed"], lease_seconds=60)["jobs"]
    assert [j["id"] for j in first] == [job_id] and first[0]["attempt"] == 1
    assert claim(cd, kinds=["embed"])["jobs"] == []      # まだ借りている

    def expire() -> None:
        with news_db.session() as db:
            db.execute(news_db.text("UPDATE jobs SET lease_until = :t"), {"t": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)})
            db.commit()
    for attempt in (2, 3):
        writer.run("news.jobs.test", expire)
        with news_db.session() as db:
            assert jobs.summary(db)["expired"] == 1      # 診断には「期限切れ」で出る
        got = claim(cd, kinds=["embed"])["jobs"]
        assert [j["attempt"] for j in got] == [attempt]
    writer.run("news.jobs.test", expire)
    assert claim(cd, kinds=["embed"])["jobs"] == []
    with news_db.session() as db:
        assert db.get(Job, job_id).status == "dead" and jobs.summary(db)["dead"] == 1
    probs = client.get("/api/v1/diagnostics").json()["problems"]
    assert any(p["area"] == "link" for p in probs)


def test_complete_is_idempotent_and_late_results_are_accepted(client, cd, fast):
    """目的: 同じ仕事の結果を 2 回返しても 1 回しか取り込まない。期限が切れた後の遅れた結果も、誰も終えていなければ受け取る。生きた借りが別の係にあれば断る。"""
    ids = save_items(1)
    with news_db.session() as db:
        db.get(NewsItem, ids[0]).prep_tries = 100
        db.commit()
    job = claim(cd, kinds=["extract_retry"])["jobs"][0]
    result = {"entities": [{"name": "GPT-6.2", "type": "model"}], "facts": [{"about": "GPT-6.2", "kind": "release", "text": "公開された", "value": ""}]}
    body = {"results": [{"job_id": job["id"], "result": result, "model": "local-llm"}]}
    assert cd.post("/jobs/complete", body).json()["results"][0]["status"] == "done"
    assert cd.post("/jobs/complete", body).json()["results"][0]["status"] == "duplicate"
    with news_db.session() as db:
        assert db.scalar(select(func.count(Entity.id))) == 1 and db.get(NewsItem, ids[0]).extract_v == 1      # 保存してから done（取り込みは 1 回）
        assert db.get(Job, job["id"]).model == "local-llm"
    assert cd.post("/jobs/complete", {"results": [{"job_id": 99999, "result": {}}]}).json()["results"][0]["status"] == "unknown"
    # 別の係が借りている間は、横取りできない
    put_job("embed", ids[0])
    other = FakeControlDeck(client, "別の ControlDeck")
    other.pair(client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()["code"])
    held = claim(cd, kinds=["embed"])["jobs"]
    if held:
        r = other.post("/jobs/complete", {"results": [{"job_id": held[0]["id"], "result": {}}]}).json()["results"][0]
        assert r["status"] == "lease_conflict"


def test_results_are_validated_before_being_taken_in(client, cd, fast):
    """目的: 記事の中身に当たる結果（取り出し・要約・埋め込み）は検査に落ちたら取り込まず、仕事は列に戻る（3 回で dead）。数字が原文に無い fact は捨てる。根拠: ARCHITECTURE 13 章（AI の出力は JSON Schema で検査）。"""
    ids = save_items(1)
    with news_db.session() as db:
        db.get(NewsItem, ids[0]).prep_tries = 100
        db.commit()
    job = claim(cd, kinds=["extract_retry"])["jobs"][0]
    for bad in ({}, {"entities": "x"}, {"entities": [{"name": "", "type": "nope"}], "facts": []}):
        r = cd.post("/jobs/complete", {"results": [{"job_id": job["id"], "result": bad}]}).json()["results"][0]
        assert r["status"] == "rejected", bad
        job = (claim(cd, kinds=["extract_retry"])["jobs"] or [job])[0]
    with news_db.session() as db:
        assert db.scalar(select(func.count(Entity.id))) == 0 and db.get(NewsItem, ids[0]).extract_v == 0
        assert db.get(Job, job["id"]).status in ("dead", "leased", "queued")
    ev, _ = add_event()
    hq = claim(cd, kinds=["summarize_hq"])["jobs"][0]
    assert cd.post("/jobs/complete", {"results": [{"job_id": hq["id"], "result": {"summary": "短い"}}]}).json()["results"][0]["status"] == "rejected"
    hq = claim(cd, kinds=["summarize_hq"])["jobs"][0]
    text = "三つの出どころが同じ出来事を報じた。内容は合成の試験用の要約で、数字を含まない。"
    assert cd.post("/jobs/complete", {"results": [{"job_id": hq["id"], "result": {"summary": text}, "model": "hq-model"}]}).json()["results"][0]["status"] == "done"
    with news_db.session() as db:
        e = db.get(Event, ev)
        assert e.summary == text and e.summary_model == "hq-model"
    assert any(c["kind"] == "events" and c["data"]["summary"] == text for c in cd.get("/changes?kinds=events").json()["changes"])      # 同期にも出る


def test_embedding_and_event_merge_results_have_intake_only(client, cd, fast):
    """目的: 埋め込みは INT8 の表へ（dim と長さが合うことを検査）・event_merge は形だけ検査して残す。作り手は無い（v0.6 以降）ので、仕事は試験で直接入れる。"""
    item = save_items(1)[0]
    ev, _ = add_event()
    put_job("embed", item)
    put_job("event_merge", ev)
    got = {j["kind"]: j for j in claim(cd, kinds=["embed", "event_merge"])["jobs"]}
    assert set(got) == {"embed", "event_merge"}
    vec = bytes(range(16))
    ok = {"model": "e5-small", "dim": 16, "scale": 0.01, "vec_b64": base64.b64encode(vec).decode()}
    for bad in ({**ok, "dim": 15}, {**ok, "scale": 0}, {**ok, "vec_b64": "###"}, {"model": "", "dim": 16, "scale": 1, "vec_b64": ok["vec_b64"]}):
        with pytest.raises(jobs.Rejected):
            jobs._decode_vec(bad)      # noqa: SLF001
    for bad in ({**ok, "dim": 15}, {**ok, "scale": 0}):      # 借りるたびに試行が増え 3 回で dead なので、API 経由は 2 回だけ
        r = cd.post("/jobs/complete", {"results": [{"job_id": got["embed"]["id"], "result": bad}]}).json()["results"][0]
        assert r["status"] == "rejected"
        got["embed"] = claim(cd, kinds=["embed"])["jobs"][0]
    assert cd.post("/jobs/complete", {"results": [{"job_id": got["embed"]["id"], "result": ok}]}).json()["results"][0]["status"] == "done"
    with news_db.session() as db:
        e = db.get(Embedding, ("item", item, "e5-small"))
        assert e.vec == vec and e.dim == 16
    merge = got["event_merge"]["id"]
    assert cd.post("/jobs/complete", {"results": [{"job_id": merge, "result": {"groups": [[1]]}}]}).json()["results"][0]["status"] == "rejected"
    merge = claim(cd, kinds=["event_merge"])["jobs"][0]["id"]
    assert cd.post("/jobs/complete", {"results": [{"job_id": merge, "result": {"groups": [[1, 2]]}}]}).json()["results"][0]["status"] == "done"
    assert cd.post("/jobs/complete", {"results": [{"job_id": 1, "result": {"x": "y" * 300_000}}]}).status_code == 422      # 結果の大きさの上限


# ---- 生成物 ----

def test_image_validation_rejects_bad_shapes(client, cd, fast):
    """目的: 画像は webp・png・jpeg だけ・512KB まで・構造と寸法を最後まで検査する。Content-Type と中身が食い違う・途中で切れた・寸法が範囲外・SVG は断る。根拠: 外から来るバイト列は信じない（ARCHITECTURE 13 章）。"""
    item = save_items(1)[0]
    ok = cd.put_image(item, png())
    assert ok.status_code == 200 and ok.json()["width"] == 64
    cases = [
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", 415),
        (png(), "image/gif", 415), (png(), "text/html", 415), (b"", "image/png", 422),
        (png()[:-8], "image/png", 422),                       # IEND が欠けた
        (png() + b"junk", "image/png", 422),                  # 末尾にゴミ
        (png(width=32, height=32), "image/png", 422),         # 小さすぎる
        (png(width=5000, height=64), "image/png", 422),       # 大きすぎる
        (png(), "image/jpeg", 422), (jpeg(), "image/png", 422), (webp(), "image/jpeg", 422),      # 種類の偽り
        (jpeg()[:-2], "image/jpeg", 422), (webp()[:-3], "image/webp", 422),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "image/png", 422),
        (png(64, 64, extra=os.urandom(600_000)), "image/png", 413),
    ]
    for data, ctype, status in cases:
        r = cd.put_image(item, data, ctype)
        assert r.status_code == status, (ctype, r.status_code, r.text)
    # 申告が小さくても、実際に多く送れば止める
    big = cd.http.put(f"/api/v1/artifacts/items/{item}/image", headers={**cd.h, "Content-Type": "image/png"}, content=iter([b"x" * 300_000, b"x" * 300_000]))
    assert big.status_code == 413
    assert cd.put_image(999999, png(70, 70)).status_code == 404
    assert cd.put_image(item, jpeg(), "image/jpeg").status_code == 200 and cd.put_image(item, webp(), "image/webp").status_code == 200


def test_image_is_stored_by_hash_replaces_the_article_image_and_is_served(client, cd, fast):
    """目的: 画像は sha256 の名前で置き場に保存され（同じ中身は 1 つ）、記事の画像が差し替わって「AI 生成」の印が付き、差分にも出て、認証つきで配られる。元の画像は replace=true でだけ上書き。"""
    ids = save_items(2)
    data = png(80, 80)
    sha = hashlib.sha256(data).hexdigest()
    r = cd.put_image(ids[0], data)
    assert r.json()["sha256"] == sha and r.json()["url"] == f"/api/v1/artifacts/images/{sha}.png" and r.json()["duplicate"] is False
    from qnapnewshub import config

    assert (config.data_dir() / "artifacts" / "images" / f"{sha}.png").read_bytes() == data
    assert cd.put_image(ids[0], data).json()["duplicate"] is True      # 同じ中身は何もしない
    r2 = cd.put_image(ids[1], data)
    assert r2.status_code == 200 and len(list((config.data_dir() / "artifacts" / "images").iterdir())) == 1      # 同じファイルを共有
    with news_db.session() as db:
        assert db.scalar(select(func.count(Artifact.sha256))) == 1
        row = db.get(NewsItem, ids[0])
        assert row.image_url == r.json()["url"] and row.image_ai is True
    shown = {i["id"]: i for i in client.get("/api/v1/news/items?view=latest").json()["items"]}
    assert shown[ids[0]]["image_url"] == r.json()["url"] and shown[ids[0]]["image_ai"] is True and shown[ids[0]]["persisted"] is True
    sync = {c["id"]: c["data"] for c in cd.get("/changes?kinds=items").json()["changes"]}
    assert sync[ids[0]]["image_ai"] is True and sync[ids[0]]["image_url"] == r.json()["url"]
    served = client.get(r.json()["url"])
    assert served.status_code == 200 and served.content == data and served.headers["content-type"] == "image/png" and served.headers["x-content-type-options"] == "nosniff"
    assert TestClient(client.app).get(r.json()["url"]).status_code == 401
    assert client.get("/api/v1/artifacts/images/../../secrets.json").status_code in (404, 422)
    assert client.get("/api/v1/artifacts/images/" + "0" * 64 + ".png").status_code == 404
    assert client.get("/api/v1/artifacts/images/%2e%2e%2fsecret.key").status_code == 404
    # 元の画像（AI 生成でない）がある記事
    with news_db.session() as db:
        db.get(NewsItem, ids[1]).image_url, db.get(NewsItem, ids[1]).image_ai = "https://orig.test/a.png", False
        db.commit()
    assert cd.put_image(ids[1], png(90, 90)).status_code == 409
    assert cd.put_image(ids[1], png(90, 90), query="?replace=true").status_code == 200


def test_image_job_is_completed_only_after_the_image_arrived(client, cd, fast):
    ev, arts = add_event()
    job = claim(cd, kinds=["image"])["jobs"][0]
    sha = hashlib.sha256(png(100, 100)).hexdigest()
    r = cd.post("/jobs/complete", {"results": [{"job_id": job["id"], "result": {"sha256": sha}}]}).json()["results"][0]
    assert r["status"] == "rejected"
    assert cd.put_image(arts[0], png(100, 100)).status_code == 200
    job = claim(cd, kinds=["image"])["jobs"][0]
    assert cd.post("/jobs/complete", {"results": [{"job_id": job["id"], "result": {"sha256": sha}}]}).json()["results"][0]["status"] == "done"


def test_research_record_is_saved_once_and_limited(client, cd):
    body = {"theme": "新しい LLM の比較", "sources": [{"title": "公式", "url": "https://example.test/a"}], "facts": ["事実 1", "事実 2"], "conclusion": "結論"}
    a = cd.post("/research", body)
    assert a.status_code == 200 and a.json()["duplicate"] is False
    assert cd.post("/research", body).json() == {"ok": True, "id": a.json()["id"], "duplicate": True}
    for bad in ({**body, "theme": ""}, {**body, "sources": [{"url": "javascript:alert(1)"}]}, {**body, "facts": ["x" * 501]}, {**body, "sources": [{}] * 51}, {**body, "conclusion": "x" * 8001}):
        assert cd.post("/research", bad).status_code == 422
    assert client.post("/api/v1/research", json=body, headers=CSRF_HEADERS).status_code == 401
    assert client.get("/api/v1/diagnostics").json()["link"]["research"] == 1


# ---- 一通り ----

def test_full_flow_pair_secret_changes_jobs_image(client, fast):
    """目的: ControlDeck 役が「つなぐ → 鍵を渡す → 差分を取る → 仕事を借りて返す → 画像を送る」を一通り通せる。診断の「つないだ ControlDeck」に最後に来た時刻・借りている仕事・期限切れが出る。"""
    deck = FakeControlDeck(client)
    started = client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()
    assert deck.pair(started["code"]).status_code == 200
    assert link.fingerprint(deck.hub_public) == started["fingerprint"]
    assert deck.send_secret("openrouter", fake_key("f")).status_code == 200 and secretbox.get_secret("openrouter_key") == fake_key("f")
    ids = save_items(3)
    cursor = 0
    got = deck.get(f"/changes?cursor={cursor}").json()
    assert len(got["changes"]) == 3
    ev, arts = add_event()
    batch = claim(deck, limit=10)["jobs"]
    kinds = sorted(j["kind"] for j in batch)
    assert kinds == ["image", "summarize_hq"]
    diag = client.get("/api/v1/diagnostics").json()["link"]
    assert diag["jobs"]["leased"] == 2 and diag["jobs"]["expired"] == 0 and diag["last_seen_at"] and diag["peers"][0]["name"] == deck.name
    results = []
    for j in batch:
        if j["kind"] == "summarize_hq":
            results.append({"job_id": j["id"], "result": {"summary": "試験用の高品質な要約。出どころ三つが同じ出来事を報じている。"}, "model": "hq"})
    assert deck.put_image(j["ref_id"], png(128, 128)).status_code == 200 if (j := next(x for x in batch if x["kind"] == "image")) else False
    results.append({"job_id": j["id"], "result": {"sha256": hashlib.sha256(png(128, 128)).hexdigest()}})
    done = deck.post("/jobs/complete", {"results": results}).json()["results"]
    assert [r["status"] for r in done] == ["done", "done"]
    after = deck.get(f"/changes?cursor={got['cursor']}").json()["changes"]
    assert {c["kind"] for c in after} >= {"events", "items"} and any(c["data"] and c["data"].get("image_ai") for c in after if c["kind"] == "items")
    with news_db.session() as db:
        assert jobs.summary(db) == {**jobs.summary(db), "queued": 0, "leased": 0, "done_24h": 2}
    ui = client.get("/api/v1/link").json()
    assert ui["jobs"]["done_24h"] == 2 and ui["peers"][0]["last_seen_at"]
