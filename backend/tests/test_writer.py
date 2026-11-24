"""Write Coordinator・お休みモード・診断の試験（ARCHITECTURE 9・9.5）。

目的: 「待機中の書き込みは原則 0」「未保存のデータを保存済みとして扱わない」「厳格なお休み中はアプリが原因の書き込みが 0」を、
時刻・件数・容量・停止の各経路で壊さないこと。
根拠: 2026-11-24 利用者の構想（QNAP の SSD の書き込みを減らす）。ARCHITECTURE 9 章・15 章（待機中の書き込み 0・保存済みの消失 0）。
外への通信・LLM は全部模擬。時刻は rest.now を差し替えて日付をまたぐ。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from qnapnewshub import db as news_db
from qnapnewshub import diagnostics, rest, service, settings, sources, store, writer
from qnapnewshub.db import NewsItem
from tests.conftest import ADMIN_NAME, CSRF_HEADERS, login, user_id


def items(n: int, start: int = 0, size: int = 0) -> list[dict]:
    return [sources.make_item(kind="news", source="S", source_key="rss:s", title=f"記事 {i}", url=f"https://a.test/{i}", summary="x" * size)
            for i in range(start, start + n)]


def count_db() -> int:
    with news_db.session() as db:
        return db.scalar(select(func.count(NewsItem.id))) or 0


def wait_for(pred, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture()
def fast(monkeypatch):
    """まとめ書きの設定を、試験用に差し替える（間隔・件数・容量）。"""
    box = {"flush_seconds": 3600, "max_items": 1000, "max_bytes": 128 * 1024 * 1024}
    monkeypatch.setattr(writer.Coordinator, "config", lambda self: dict(box))
    return box


@pytest.fixture()
def clock(monkeypatch):
    """お休みモードの時計。box["t"] を動かして時刻をまたぐ。"""
    box = {"t": datetime(2026, 11, 24, 12, 0)}
    monkeypatch.setattr(rest, "now", lambda: box["t"])
    return box


def set_rest(enabled=True, mode="keep_memory", start="01:00", end="06:00"):
    cfg = settings.load()
    cfg["rest"] = {"enabled": enabled, "start": start, "end": end, "mode": mode}
    settings.save(cfg)


def tick():
    coord = writer.get()
    coord.run("test.tick", coord._tick, user=True)  # noqa: SLF001


# ---- まとめ書き ----

def test_items_are_buffered_until_interval_then_written_in_one_transaction(fast):
    """目的: 記事は積むだけでは DB に入らず、間隔が来たら 1 回でまとめて書かれる。根拠: 既定 30 分ごとのまとめ書き（待機中の書き込み 0）。"""
    fast["flush_seconds"] = 0.4
    result = store.stage_items(items(5))
    assert result["new"] == 5 and count_db() == 0
    assert writer.get().counts()["items"] == 5
    assert wait_for(lambda: count_db() == 5)
    assert wait_for(lambda: writer.get().flush_history)
    assert writer.get().flush_history[-1]["reason"] == "interval" and writer.get().flush_history[-1]["items"] == 5


def test_flush_when_item_count_exceeds_limit(fast):
    """目的: 件数の上限を超えたら、間隔を待たずに書く。根拠: 1,000 件 / 128MB を超えたとき（メモリを増やし続けない）。"""
    fast["max_items"] = 10
    store.stage_items(items(5))
    time.sleep(0.2)
    assert count_db() == 0
    store.stage_items(items(8, start=5))
    assert wait_for(lambda: count_db() == 13)
    assert wait_for(lambda: writer.get().flush_history)
    assert writer.get().flush_history[-1]["reason"] == "limit"


def test_flush_when_buffer_bytes_exceed_limit(fast):
    """目的: 容量の上限（バイト）でも書く。根拠: 件数が少なくても大きな記事が溜まるとメモリを食う。"""
    fast["max_bytes"] = 2000
    store.stage_items(items(3, size=6000))
    assert wait_for(lambda: count_db() == 3)
    assert wait_for(lambda: writer.get().flush_history)
    assert writer.get().flush_history[-1]["reason"] == "limit"


def test_user_operations_are_written_immediately_through_the_writer(client):
    """目的: 設定・好み・操作は即時に書くが、必ず書き手のスレッド経由。根拠: すべての永続化を 1 つの係に通す（ARCHITECTURE 9）。"""
    coord = writer.get()
    before = dict(coord.writes)
    r = client.put("/api/v1/news/prefs", json={"display": {}, "taste": {}}, headers=CSRF_HEADERS)
    assert r.status_code == 200
    cfg = settings.load()
    cfg["feed_refresh_minutes"] = 20
    r = client.put("/api/v1/news/settings", json=cfg, headers=CSRF_HEADERS)
    assert r.status_code == 200 and settings.load()["feed_refresh_minutes"] == 20      # すぐファイルに入っている
    assert coord.writes["prefs.save"] == before.get("prefs.save", 0) + 1
    assert coord.writes["settings.save"] > before.get("settings.save", 0)
    assert coord.writes["auth.login"] >= 1 or coord.writes["auth.create_admin"] >= 1      # 認証も係を通る


def test_wal_autocheckpoint_is_off_and_checkpoint_follows_flush(fast):
    """目的: WAL の自動 checkpoint を切り、まとめ書きの直後にだけ checkpoint する。根拠: 小さな書き込みのたびに WAL を戻すと書き込みが増える。"""
    with news_db.engine().connect() as conn:
        assert conn.exec_driver_sql("PRAGMA wal_autocheckpoint").scalar() == 0
    store.stage_items(items(20))
    info = writer.flush("test")
    assert info["ok"] and info["items"] == 20 and "passive" in info["checkpoint"]


def test_unsaved_flag_and_persisted_only_changes(client, fast):
    """目的: 未保存の記事は API で persisted:false、保存後は true。ControlDeck への差分は保存済みだけ。根拠: 保存が済んでいないデータを保存済みとして扱わない（ARCHITECTURE 9）。"""
    store.stage_items(items(3))
    got = client.get("/api/v1/news/items?view=latest").json()["items"]
    assert len(got) == 3 and all(i["persisted"] is False for i in got)
    with news_db.session() as db:
        assert store.changes_persisted(db) == []
    writer.flush("test")
    got = client.get("/api/v1/news/items?view=latest").json()["items"]
    assert len(got) == 3 and all(i["persisted"] is True for i in got)
    with news_db.session() as db:
        assert len(store.changes_persisted(db)) == 3


def test_patches_on_saved_items_are_marked_unsaved_until_flush(client, fast):
    """目的: 保存済みの記事への未保存の更新（下ごしらえの結果）も、画面には出しつつ persisted:false。根拠: 同上（訳が消える可能性を隠さない）。"""
    store.stage_items(items(1))
    writer.flush("test")
    with news_db.session() as db:
        item_id = db.scalar(select(NewsItem.id))
    writer.get().patch_item(item_id, set={"summary_ja": "日本語の概要", "prep_tries": 100})
    shown = client.get("/api/v1/news/items?view=latest").json()["items"][0]
    assert shown["summary"] == "日本語の概要" and shown["persisted"] is False
    with news_db.session() as db:
        assert db.get(NewsItem, item_id).summary_ja == ""
    writer.flush("test")
    assert client.get("/api/v1/news/items?view=latest").json()["items"][0]["persisted"] is True


def test_action_on_unsaved_item_flushes_first(client, fast):
    """目的: 未保存の記事への「いいね」は、記事を先に保存してから書く（外部キー）。根拠: 利用者の操作は即時に書く。"""
    store.stage_items(items(1))
    item_id = client.get("/api/v1/news/items?view=latest").json()["items"][0]["id"]
    r = client.post(f"/api/v1/news/items/{item_id}/action", json={"action": "like"}, headers=CSRF_HEADERS)
    assert r.status_code == 200 and r.json()["state"]["like"] is True and count_db() == 1


def test_sigterm_flushes_the_buffer_before_exit(tmp_path):
    """目的: 正常な停止（SIGTERM）でバッファを書いてから終わる。根拠: 15 章「保存済みのデータの再起動での消失 0」。実際の uvicorn の停止の経路で確かめる。"""
    data = tmp_path / "d"
    script = tmp_path / "run.py"
    port = 18900 + os.getpid() % 500
    script.write_text(textwrap.dedent(f"""
        import threading, time, uvicorn
        from qnapnewshub import sources, store
        from qnapnewshub.main import app
        def stage():
            time.sleep(1.5)
            store.stage_items([sources.make_item(kind="news", source="S", source_key="rss:s", title=f"記事 {{i}}", url=f"https://a.test/{{i}}") for i in range(7)])
            open({str(tmp_path / 'staged')!r}, "w").write("1")
        threading.Thread(target=stage, daemon=True).start()
        uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
    """))
    env = {**os.environ, "NEWSHUB_DATA": str(data), "NEWSHUB_AUTO": "0", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    # 試験の実行環境によっては SIGTERM が塞がれて子に引き継がれる。子では開けてから起こす
    unblock = lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})  # noqa: E731
    proc = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=unblock)
    try:
        assert wait_for(lambda: (tmp_path / "staged").exists(), 20)
        db_file = data / "news" / "news.sqlite3"
        import sqlite3
        assert sqlite3.connect(db_file).execute("SELECT count(*) FROM news_items").fetchone()[0] == 0      # まだ書いていない
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) in (0, -signal.SIGTERM)      # uvicorn は終了処理の後に受けた信号を再送する
    finally:
        if proc.poll() is None:
            proc.kill()
    con = sqlite3.connect(db_file)
    assert con.execute("SELECT count(*) FROM news_items").fetchone()[0] == 7
    assert not (data / "news" / "news.sqlite3-wal").exists() or (data / "news" / "news.sqlite3-wal").stat().st_size == 0      # 停止で checkpoint 済み


def test_failed_flush_keeps_the_buffer(fast, monkeypatch):
    """目的: 保存に失敗してもバッファを捨てず、次の機会に書く。根拠: 取り直せない分（利用者の操作に紐づく結果）を失わない。"""
    store.stage_items(items(4))
    real = store.apply_staged
    monkeypatch.setattr(store, "apply_staged", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    info = writer.flush("test")
    assert info["ok"] is False and writer.get().counts()["items"] == 4 and count_db() == 0
    monkeypatch.setattr(store, "apply_staged", real)
    assert writer.flush("test")["ok"] and count_db() == 4


# ---- お休みモード ----

def test_rest_window_crosses_midnight(clock):
    """目的: 開始 > 終了は日をまたぐ。根拠: 22:00〜06:00 のような設定が普通（時刻の判定の取りこぼしを防ぐ）。"""
    set_rest(start="22:00", end="06:00")
    for hhmm, expect in (("21:59", False), ("22:00", True), ("23:30", True), ("00:10", True), ("05:59", True), ("06:00", False)):
        clock["t"] = datetime(2026, 11, 24, int(hhmm[:2]), int(hhmm[3:]))
        assert rest.active() is expect, hhmm
    clock["t"] = datetime(2026, 11, 24, 23, 0)
    assert rest.state()["ends_at"] == "2026-11-25T06:00:00"


def test_rest_keep_memory_holds_buffer_and_flushes_at_end(fast, clock):
    """目的: 「集め続けてメモリに保持」: 休止中は上限を超えても DB に書かず、休止が明けたらまとめて書く。根拠: お休みモード（時刻をまたぐ）。"""
    set_rest(mode="keep_memory", start="01:00", end="06:00")
    fast["max_items"] = 5
    clock["t"] = datetime(2026, 11, 24, 2, 0)
    store.stage_items(items(8))
    tick()
    tick()
    assert count_db() == 0 and writer.get().counts()["items"] == 8
    clock["t"] = datetime(2026, 11, 24, 6, 1)
    tick()
    assert count_db() == 8 and writer.get().flush_history[-1]["reason"] == "rest_end"


def test_rest_keep_memory_stops_collecting_at_safety_cap(fast, clock):
    """目的: メモリ保持にも安全の上限がある（件数の 5 倍）。上限なら集めるのを止める。根拠: 停電以外でもメモリを使い切らない。"""
    set_rest(mode="keep_memory")
    fast["max_items"] = 2
    clock["t"] = datetime(2026, 11, 24, 2, 0)
    store.stage_items(items(10))
    import asyncio
    assert asyncio.run(service.refresh(force=True)) == {"started": False, "reason": "memory_full"}


def test_rest_stop_all_refuses_fetch_and_analysis(fast, clock, monkeypatch):
    """目的: 「すべて休む」: 取得を始めない・下ごしらえをしない・保存もしない。根拠: 厳格な休止（15 章: アプリが原因の書き込み 0）。"""
    import asyncio

    from qnapnewshub import prep
    set_rest(mode="stop_all")
    clock["t"] = datetime(2026, 11, 24, 3, 0)
    assert asyncio.run(service.refresh(force=True)) == {"started": False, "reason": "rest"}
    assert asyncio.run(prep.run(items(2) and [{"id": 1, "kind": "news"}])) == {"done": 0, "calls": 0, "error": ""}


def test_rest_flush_at_limit_writes_only_when_limit_reached(fast, clock):
    """目的: 「集め続けて上限のときだけ一括で保存」: 上限未満は書かず、上限に達したら書く。根拠: お休みモードの 3 つめの動き。"""
    set_rest(mode="flush_at_limit")
    fast["max_items"] = 6
    clock["t"] = datetime(2026, 11, 24, 2, 0)
    store.stage_items(items(3))
    tick()
    assert count_db() == 0
    store.stage_items(items(5, start=3))
    assert wait_for(lambda: count_db() == 8)
    assert wait_for(lambda: writer.get().flush_history)
    assert writer.get().flush_history[-1]["reason"] == "limit"


def test_db_files_do_not_change_during_rest(client, fast, clock):
    """目的: 休止中（保持・上限なし）は、DB と WAL の mtime と大きさが変わらない。読み（一覧・診断）も書き込みを起こさない。根拠: 15 章「厳格なお休み中の書き込み: アプリが原因のものは 0」。"""
    store.stage_items(items(2))
    writer.flush("test")
    db = news_db.path()
    wal = Path(str(db) + "-wal")
    auth_db = db.parent.parent / "auth.sqlite3"
    set_rest(mode="keep_memory")
    clock["t"] = datetime(2026, 11, 24, 2, 0)
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in (db, wal, auth_db) if p.exists()}
    time.sleep(0.05)
    store.stage_items(items(5, start=10))
    tick()
    client.get("/api/v1/news/items?view=latest")
    client.get("/api/v1/news/home")
    client.get("/api/v1/diagnostics")
    tick()
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in (db, wal, auth_db) if p.exists()}
    assert before == after
    assert [w for w in writer.get().rest_writes if not w["user"]] == []


def test_rest_records_names_of_writes_during_rest(client, clock):
    """目的: 休止中に書いた処理名を残す（利用者の操作か、アプリが原因か）。根拠: 診断「お休み中に書き込みがあったらその処理名」。"""
    set_rest(mode="keep_memory")
    clock["t"] = datetime(2026, 11, 24, 2, 0)
    writer.run("app.something", lambda: None, user=False)
    client.put("/api/v1/news/prefs", json={"display": {}, "taste": {}}, headers=CSRF_HEADERS)
    names = {(w["name"], w["user"]) for w in writer.get().rest_writes}
    assert ("app.something", False) in names and ("prefs.save", True) in names
    diag = client.get("/api/v1/diagnostics").json()
    assert diag["storage"]["rest_app_writes"] == 1 and any("お休み中" in p["text"] for p in diag["problems"])


def test_rest_settings_validation(admin_client):
    """目的: お休みモードの設定は形を検査する。根拠: 不正な動き・時刻が保存されると休止の判定が壊れる。"""
    cfg = admin_client.get("/api/v1/news/settings").json()["settings"]
    assert cfg["rest"]["enabled"] is False          # 既定は切
    cfg["rest"] = {"enabled": True, "start": "23:00", "end": "05:30", "mode": "stop_all"}
    assert admin_client.put("/api/v1/news/settings", json=cfg, headers=CSRF_HEADERS).status_code == 200
    cfg["rest"]["mode"] = "bad"
    assert admin_client.put("/api/v1/news/settings", json=cfg, headers=CSRF_HEADERS).status_code == 422
    cfg["rest"] = {"enabled": True, "start": "25:00", "end": "05:30", "mode": "stop_all"}
    assert admin_client.put("/api/v1/news/settings", json=cfg, headers=CSRF_HEADERS).status_code == 422


# ---- 診断 ----

SECRET_KEY = "sk-or-" + "-".join(["SECRETKEYVALUE", "0123456789"])
SECRET_QUERY = "-".join(["urlsecret", "98765"])


def test_diagnostics_has_no_secrets(admin_client):
    """目的: 診断（画面・束）に、OpenRouter の鍵・API トークン・URL の問い合わせの秘密を出さない。根拠: AGENTS.md 絶対ルール 4（秘密を画面・ログに出さない）・9.5「鍵や URL の秘密の部分は出さない」。"""
    c = admin_client
    assert c.put("/api/v1/news/openrouter-key", json={"key": SECRET_KEY}, headers=CSRF_HEADERS).status_code == 200
    token = c.post("/api/v1/auth/tokens", json={"name": "cd"}, headers=CSRF_HEADERS).json()["token"]
    c.put("/api/v1/news/sources", json={"rss": [{"name": "秘密つき", "url": f"https://feed.test/rss?token={SECRET_QUERY}"}]}, headers=CSRF_HEADERS)
    service._status["rss:https://feed.test/rss?token=" + SECRET_QUERY] = {"label": "秘密つき", "kind": "rss", "ok": False, "error": f"HTTP 500 https://feed.test/rss?token={SECRET_QUERY}", "at": "", "skipped": "", "count": 0}
    import logging
    logging.getLogger("qnapnewshub.test").warning("失敗 %s Bearer %s", SECRET_KEY, token)
    store.stage_items(items(1))
    for path in ("/api/v1/diagnostics", "/api/v1/diagnostics/bundle", "/api/v1/diagnostics/notice"):
        text = c.get(path).text
        assert SECRET_KEY not in text and token not in text and SECRET_QUERY not in text and "SECRETKEYVALUE" not in text, path
    text = c.post("/api/v1/diagnostics/selfcheck", headers=CSRF_HEADERS).text
    assert SECRET_KEY not in text
    data = c.get("/api/v1/diagnostics").json()
    assert data["llm"]["key_set"] is True and data["fetch"]["total"] >= 1


def test_diagnostics_is_admin_only(anon_client):
    """目的: 診断は管理者だけ（未ログイン・API トークンは不可）。根拠: AGENTS.md 絶対ルール 5（API ごとに認証と権限を確かめる）。"""
    from tests.conftest import ADMIN_PASSWORD
    assert anon_client.get("/api/v1/diagnostics").status_code == 401
    anon_client.post("/api/v1/auth/setup", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD})
    assert anon_client.get("/api/v1/diagnostics").status_code == 200
    token = anon_client.post("/api/v1/auth/tokens", json={"name": "cd"}, headers=CSRF_HEADERS).json()["token"]
    anon_client.cookies.clear()
    for path in ("/api/v1/diagnostics", "/api/v1/diagnostics/bundle"):
        assert anon_client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert anon_client.post("/api/v1/diagnostics/selfcheck", headers={**CSRF_HEADERS}).status_code == 401


def test_diagnostics_sections_and_selfcheck(admin_client, fast):
    """目的: 診断の 4 区分と自己点検が形どおりに返る（取得・LLM・保存と I/O・機械・点検）。根拠: ARCHITECTURE 9.5 の表。"""
    store.stage_items(items(3))
    data = admin_client.get("/api/v1/diagnostics").json()
    assert set(["fetch", "llm", "storage", "machine", "problems"]) <= set(data)
    st = data["storage"]
    assert st["unsaved"]["items"] == 3 and "write_bytes" in st["io"] and st["files"]["db"] > 0 and st["disk"]["free"] > 0
    assert data["machine"]["rss"] > 0 and data["machine"]["uptime_seconds"] >= 0 and data["llm"]["daily_limit"] > 0
    check = admin_client.post("/api/v1/diagnostics/selfcheck", headers=CSRF_HEADERS).json()
    names = [c["name"] for c in check["checks"]]
    assert any("quick_check" in n for n in names) and any("FTS" in n for n in names) and any("鍵" in n for n in names) and any("時計" in n for n in names)
    assert next(c for c in check["checks"] if "quick_check" in c["name"])["ok"]


def test_stalled_source_is_reported(admin_client):
    """目的: 失敗が続く取得先は「止まった可能性」と直し方を出す。根拠: 9.5「失敗が続く → 止まった可能性と直し方」。"""
    from datetime import timezone
    rows = [{"source_key": "rss:https://f.test/rss", "at": datetime.now(timezone.utc), "ok": False, "status": 500, "count": 0, "new": 0, "ms": 5, "error": "HTTP 500"} for _ in range(4)]
    writer.get().stage_fetchlog(rows)
    writer.flush("test")
    c = admin_client
    c.put("/api/v1/news/sources", json={"rss": [{"name": "F", "url": "https://f.test/rss"}]}, headers=CSRF_HEADERS)
    d = c.get("/api/v1/diagnostics").json()
    row = next(r for r in d["fetch"]["sources"] if r["name"] == "F")
    assert row["stalled"] and row["fail_streak"] == 4 and row["reason"] == "http" and row["fix"]
    assert c.get("/api/v1/diagnostics/notice").json()["message"]


# ---- 常駐 ----

def test_systemd_unit_content(tmp_path):
    """目的: install-service が書く systemd のユーザー単位の中身（再起動・停止時間・データの置き場・root でない）。根拠: AGENTS.md 絶対ルール 2・Restart=on-failure・TimeoutStopSec でバッファを書き切る。"""
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([str(root / "hub.sh"), "print-service"], capture_output=True, text=True, env={**os.environ, "HOME": str(tmp_path)})
    assert out.returncode == 0, out.stderr
    unit = out.stdout
    assert "[Service]" in unit and "Restart=on-failure" in unit and "TimeoutStopSec=" in unit and "WantedBy=default.target" in unit
    assert "ExecStart=" in unit and "cli run" in unit.replace("-m qnapnewshub.", "") or "qnapnewshub.cli run" in unit
    assert "User=" not in unit and "SECRET" not in unit.upper() and "KEY=" not in unit
    assert not (tmp_path / ".config" / "systemd").exists()      # 実際には入れない
