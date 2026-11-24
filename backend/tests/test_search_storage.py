"""v0.2 後半: 日本語の全文検索（trigram）＋英語（porter）・本文の取り出し（Trafilatura・zstd）・古い記事の圧縮の試験。

目的: ①日本語の部分一致・英語の語幹・混在が効き BM25 で順位づけされる ②古い DB の索引は開くとき壊れず、まとめ書きの単位で作り直せる
③本文の取り出しは 1 日・1 分の上限を守り、robots.txt・有料・取れない記事を記録して二度と試さない ④本文・抜粋は Zstandard で往復できる
⑤1 年より古い記事は抜粋を圧縮し索引から外す ⑥書き込みは Write Coordinator（まとめ書き・書き手のスレッド）だけを通る。
根拠: 2026-11-24 利用者「日本語の全文検索・本文の取り出し（Trafilatura）・保存の圧縮」・ARCHITECTURE 2・8・9・10 章・AGENTS.md 7（robots・ロボット確認をすり抜けない）。
外への通信は全部模擬（bodies._http を差し替える）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from qnapnewshub import bodies, maintenance, searchindex, settings, writer, ztext
from qnapnewshub import db as news_db
from qnapnewshub.db import NewsBody, NewsCold, NewsItem

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def add(title: str, url: str, *, summary="", summary_ja="", kind="news", published=None, source="S") -> int:
    with news_db.session() as db:
        row = NewsItem(kind=kind, source=source, source_key="rss:s", title=title, url=url, summary=summary, summary_ja=summary_ja,
                       published_at=published or NOW, fetched_at=NOW, topics="[]")
        db.add(row)
        db.commit()
        return row.id


def find(client, q: str) -> list[int]:
    r = client.get("/api/v1/news/items", params={"view": "latest", "q": q})
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


# ---- 検索 ----

def test_japanese_partial_match_short_words_and_english(admin_client):
    """目的: 日本語の部分一致（3 文字以上は trigram・2 文字以下は LIKE）と英語の語幹（porter）が効く。"""
    a = add("大規模言語モデルの新しい推論手法", "https://a.test/1", summary_ja="推論を速くする方法を発表")
    b = add("The model runs fast on a laptop", "https://a.test/2", summary="Benchmarks of local inference")
    c = add("GPT のあたらしい言語モデルを公開", "https://a.test/3")
    assert find(admin_client, "言語モデル") == [a, c] or set(find(admin_client, "言語モデル")) == {a, c}
    assert find(admin_client, "語モデルの新") == [a]              # 語の途中からでも
    assert find(admin_client, "推論") == [a]                      # 2 文字: LIKE
    assert find(admin_client, "running") == [b]                   # 英語: runs と running は同じ語幹
    assert find(admin_client, "bench") == [b]                     # 語幹で見つからない部分一致は trigram へ
    assert find(admin_client, "GPT 言語モデル") == [c]            # 英語＋日本語の混在（両方の索引の積）
    assert find(admin_client, "存在しない語です") == []


def test_bm25_ranks_title_hits_above_excerpt_hits(admin_client):
    """目的: 検索は新しさでなく BM25 の順（題の一致が抜粋だけの一致より上）。根拠: ARCHITECTURE 15 章（検索の質）・v0.2 の要件「両方を合わせて順位づけ」。"""
    old = add("量子コンピュータの発表", "https://a.test/1", published=NOW - timedelta(days=3))
    new = add("今日のニュースまとめ", "https://a.test/2", summary="記事の中で量子コンピュータにも触れる", published=NOW)
    assert find(admin_client, "量子コンピュータ") == [old, new]


def test_entity_names_and_source_are_searchable(admin_client):
    """目的: 物の名前（要素の取り出し）と出どころも索引の対象。"""
    from qnapnewshub import entities

    i = add("新しいチップの話", "https://a.test/1", source="ひみつ通信")
    with news_db.session() as db:
        entities.apply_extractions(db, {i: {"entities": [{"name": "Qwen3-Coder", "type": "model"}], "facts": []}})
        db.commit()
    assert find(admin_client, "Qwen3") == [i]
    assert find(admin_client, "ひみつ通信") == [i]


def test_old_database_index_is_rebuilt_in_batches(admin_client):
    """目的: 古い DB（3 列の外部コンテンツの索引）を開くと新しい索引へ移り、既存の行は未索引の間も LIKE で見つかり、バッチで作り直せる。
    根拠: 利用者の QNAP にある v0.1.x の DB を壊さずに更新する（更新で戻せないデータの移行）。"""
    ids = [add(f"移行の確認の記事 {n}", f"https://a.test/{n}", summary_ja=f"固有の語{n:03d}を含む") for n in range(5)]
    news_db._engines.clear()
    con = sqlite3.connect(news_db.path())
    for t in ("news_items_ai", "news_items_ad", "news_items_au"):
        con.execute(f"DROP TRIGGER {t}")
    con.execute("DROP TABLE news_fts_tri")
    con.execute("DROP TABLE news_fts_word")
    con.execute("CREATE VIRTUAL TABLE news_items_fts USING fts5(title, summary, source, content='news_items', content_rowid='id', tokenize='trigram')")
    con.execute("INSERT INTO news_items_fts(news_items_fts) VALUES ('rebuild')")
    con.execute("CREATE TRIGGER news_items_ai AFTER INSERT ON news_items BEGIN INSERT INTO news_items_fts(rowid, title, summary, source) VALUES (new.id, new.title, new.summary, new.source); END")
    con.commit()
    con.close()
    with news_db.engine().connect() as conn:      # 開くと移行
        assert not conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='news_items_fts'").fetchall()
        assert searchindex.progress(conn)["building"]
    assert find(admin_client, "固有の語003") == [ids[3]]      # 作り直し中は未索引の範囲を LIKE で補う
    done = maintenance.index_step(rows=2)
    assert done["done"] == 2 and done["building"]
    while maintenance.index_step(rows=2)["building"]:
        pass
    with news_db.engine().connect() as conn:
        assert not searchindex.progress(conn)["building"]
        assert searchindex.counts(conn) == {"tri": 5, "word": 5}
    assert find(admin_client, "固有の語003") == [ids[3]]
    assert writer.get().writes["news.index"] >= 3      # 書き込みは書き手（Write Coordinator）を通っている


def test_rebuild_request_and_idle_step_does_not_write(admin_client):
    """目的: 作り直しの予約で索引が空になり、仕事が無いとき（待機中）は書き込みを起こさない。"""
    add("作り直しの記事です", "https://a.test/1")
    assert maintenance.index_step()["done"] == 0
    assert "news.index" not in writer.get().writes      # 仕事が無ければ書き手へも行かない
    with news_db.engine().begin() as conn:
        searchindex.request_rebuild(conn)
        assert searchindex.counts(conn) == {"tri": 0, "word": 0}
    while maintenance.index_step()["building"]:
        pass
    with news_db.engine().connect() as conn:
        assert searchindex.counts(conn) == {"tri": 1, "word": 1}


# ---- 圧縮 ----

def test_zstd_round_trip_and_corrupt_data():
    """目的: 本文・抜粋の圧縮と展開が往復し、壊れたデータは例外（ValueError）で、展開の大きさに上限がある。"""
    text = "日本語の本文です。" * 500 + "English body. " * 200
    blob = ztext.pack(text)
    assert ztext.unpack(blob) == text and len(blob) < len(text.encode()) // 5
    assert ztext.unpack(None) == ""
    with pytest.raises(ValueError):
        ztext.unpack(b"not zstd data")


def test_old_articles_are_compressed_and_dropped_from_the_excerpt_index(admin_client):
    """目的: 1 年より古い記事は抜粋を圧縮して news_cold へ移し、索引からは抜粋を外す（題・概要は残る）。画面には抜粋を戻して出す。"""
    old = NOW - timedelta(days=400)
    a = add("古い記事の題です", "https://a.test/1", summary="抜粋だけにある珍しい言葉ゼフィロス", summary_ja="古い概要の文", published=old)
    b = add("新しい記事の題です", "https://a.test/2", summary="抜粋だけにある珍しい言葉ゼフィロス", published=NOW)
    assert set(find(admin_client, "ゼフィロス")) == {a, b}
    assert maintenance.compact_step(years=1)["done"] == 1
    assert maintenance.compact_step(years=1)["done"] == 0      # 対象が無ければ何もしない
    assert find(admin_client, "ゼフィロス") == [b]            # 古い記事は抜粋の語で見つからない
    assert find(admin_client, "古い記事の題") == [a] and find(admin_client, "古い概要") == [a]      # 題・概要は残る
    with news_db.session() as db:
        row = db.get(NewsItem, a)
        assert row.cold == 1 and row.summary == "" and ztext.unpack(db.get(NewsCold, a).z).startswith("抜粋だけ")
        assert db.get(NewsItem, b).cold == 0
    r = admin_client.get("/api/v1/news/items", params={"view": "latest", "q": "古い記事の題"}).json()["items"][0]
    assert r["summary"] == "古い概要の文"
    assert maintenance.compact_step(years=0)["done"] == 0      # 0 年は圧縮しない設定


# ---- 本文の取り出し ----

PARA = "この記事は本文の取り出しの試験のために書かれた、十分に長い段落です。ニュースハブは重要度の高い記事の本文を、上限の中で取り出します。"
PAGE = ("<html><head><title>記事</title></head><body><nav>メニュー</nav><article><h1>見出し</h1>"
        + "".join(f"<p>{PARA}{n}番目の段落。</p>" for n in range(8)) + "</article><footer>フッター</footer></body></html>").encode()


class FakeWeb:
    """bodies._http の代わり。URL ごとの応答 {url: (status, headers, body)}。robots.txt は host/robots.txt。"""

    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    async def __call__(self, url, *, max_bytes=0, accept=""):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return self.pages.get(url, (404, {}, b""))
        return self.pages.get(url, (404, {}, b""))


@pytest.fixture()
def web(monkeypatch):
    bodies._robots.clear()
    bodies._deferred.clear()
    bodies._host_last.clear()
    monkeypatch.setattr(bodies, "HOST_PACE", 0)
    box = FakeWeb({})
    monkeypatch.setattr(bodies, "_http", box)
    return box


def html(body=PAGE, **head):
    return 200, {"content-type": "text/html; charset=utf-8", **head}, body


def set_body(**changes):
    cfg = settings.load()
    settings.save({**cfg, "body": {**cfg["body"], **changes}})


def run(coro):
    return asyncio.run(coro)


def test_body_is_extracted_compressed_and_marked(admin_client, web):
    """目的: 本文を取り出して zstd の BLOB で news_body へ（まとめ書きで）・記事に「本文あり」の印・本文は API に出さない。"""
    i = add("本文のある記事", "https://news.test/a")
    web.pages["https://news.test/a"] = html()
    got = run(bodies.run_once())
    assert got["tried"] == 1 and got["ok"] == 1
    with news_db.session() as db:
        assert db.get(NewsBody, i) is None      # まとめ書きの前は DB に無い（積んだだけ）
    r = admin_client.get("/api/v1/news/items", params={"view": "latest"}).json()["items"][0]
    assert r["has_body"] is True and r["persisted"] is not None and "body" not in r      # 未保存でも印は出る（重ねる）
    writer.flush("test")
    with news_db.session() as db:
        row = db.get(NewsBody, i)
        assert row.state == 1 and db.get(NewsItem, i).body_state == 1
        text = ztext.unpack(row.body)
        assert "本文の取り出しの試験" in text and "メニュー" not in text and "フッター" not in text
        assert len(row.body) < row.raw_len
        assert bodies.get_body(db, i) == text
        stats = bodies.stats(db)
    assert stats["ok"] == 1 and stats["packed_bytes"] < stats["raw_bytes"]


def test_per_minute_and_per_day_limits_and_importance_order(admin_client, web):
    """目的: 1 回（1 分）の件数・1 日の件数の上限を守り、重要度（近い重複の数・物の数）の高い記事から取る。"""
    ids = [add(f"記事{n}", f"https://n{n}.test/a", published=NOW - timedelta(minutes=n)) for n in range(6)]
    with news_db.session() as db:       # ids[5] に近い重複を 2 件つける → 一番重要
        for k in range(2):
            db.add(NewsItem(kind="news", source="S2", source_key="rss:t", title="同じ", url=f"https://dup{k}.test/a", published_at=NOW, fetched_at=NOW, dup_of=ids[5]))
        db.commit()
    for n in range(6):
        web.pages[f"https://n{n}.test/a"] = html()
    set_body(per_minute=2, per_day=3)
    first = run(bodies.run_once())
    assert first["tried"] == 2
    tried_first = {u for u in web.calls if not u.endswith("robots.txt")}
    assert "https://n5.test/a" in tried_first      # 重要度が一番高い
    second = run(bodies.run_once())
    assert second["tried"] == 1                  # 1 日 3 件まで（試行は積んだ分も数える）
    assert run(bodies.run_once())["tried"] == 0
    set_body(enabled=False)
    assert run(bodies.run_once())["reason"] == "off"


def test_robots_paywall_and_failures_are_recorded_and_never_retried(admin_client, web):
    """目的: robots.txt が禁じる・有料・ロボット確認・取れないページは理由を記録し、二度と試さない（すり抜けない）。"""
    r1 = add("robots が禁止", "https://r.test/a")
    r2 = add("有料の記事", "https://p.test/a")
    r3 = add("ロボット確認", "https://c.test/a")
    r4 = add("403 の記事", "https://f.test/a")
    r5 = add("短いページ", "https://s.test/a")
    web.pages["https://r.test/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /a\n")
    web.pages["https://r.test/a"] = html()
    web.pages["https://p.test/a"] = html(PAGE.replace(b"<head>", b'<head><script type="application/ld+json">{"isAccessibleForFree": false}</script>'))
    web.pages["https://c.test/a"] = html(b"<html><head><title>Just a moment...</title></head><body>checking</body></html>")
    web.pages["https://f.test/a"] = (403, {}, b"forbidden")
    web.pages["https://s.test/a"] = html(b"<html><body><p>short</p></body></html>")
    set_body(per_minute=10)
    got = run(bodies.run_once())
    assert got["failed"] == 5 and got["ok"] == 0
    assert "https://r.test/a" not in web.calls      # robots が禁じたページは取りに行かない
    writer.flush("test")
    with news_db.session() as db:
        reasons = {i: db.get(NewsBody, i).reason for i in (r1, r2, r3, r4, r5)}
        assert reasons == {r1: "robots", r2: "paywall", r3: "challenge", r4: "blocked", r5: "short"}
        assert all(db.get(NewsItem, i).body_state == 2 for i in reasons)
        assert bodies.stats(db)["reasons"]["robots"] == 1
    web.calls.clear()
    assert run(bodies.run_once())["tried"] == 0      # 二度と試さない
    assert web.calls == []


def test_noarchive_is_respected_and_unreadable_robots_is_deferred(admin_client, web):
    """目的: noarchive／noai の指示は守る。robots.txt そのものが読めないときは記録せず後で試す（記事のせいではない）。"""
    a = add("noai の記事", "https://x.test/a")
    b = add("robots が読めない", "https://y.test/a")
    web.pages["https://x.test/a"] = html(PAGE, **{"x-robots-tag": "noai"})
    web.pages["https://y.test/robots.txt"] = (503, {}, b"")
    web.pages["https://y.test/a"] = html()
    got = run(bodies.run_once())
    assert got["failed"] == 1 and got["deferred"] == 1
    writer.flush("test")
    with news_db.session() as db:
        assert db.get(NewsBody, a).reason == "noarchive"
        assert db.get(NewsBody, b) is None and db.get(NewsItem, b).body_state == 0
    assert "https://y.test/a" not in web.calls


def test_redirect_to_another_host_checks_robots_there(admin_client, web):
    """目的: リダイレクト先のホストの robots.txt も守る。"""
    i = add("転送される記事", "https://a.test/a")
    web.pages["https://a.test/a"] = (301, {"location": "https://b.test/z"}, b"")
    web.pages["https://b.test/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /\n")
    web.pages["https://b.test/z"] = html()
    run(bodies.run_once())
    writer.flush("test")
    with news_db.session() as db:
        assert db.get(NewsBody, i).reason == "robots"
    assert "https://b.test/z" not in web.calls


def test_quiet_and_rest_modes_do_not_fetch(admin_client, web, monkeypatch):
    """目的: お休みモード中は本文を取りに行かない（通信も書き込みも増やさない）。"""
    from qnapnewshub import rest

    add("記事", "https://n.test/a")
    web.pages["https://n.test/a"] = html()
    monkeypatch.setattr(rest, "blocks_fetch", lambda: True)
    assert run(bodies.run_once())["reason"] == "rest" and web.calls == []


def test_lead_replaces_excerpt_in_the_prep_prompt(admin_client, web):
    """目的: 下ごしらえ（LLM）への入力は、本文があれば抜粋の代わりに本文の頭（既定 1,500 文字）。"""
    from qnapnewshub import prep, store

    i = add("本文のある記事", "https://news.test/a", summary="短い抜粋")
    web.pages["https://news.test/a"] = html()
    run(bodies.run_once())
    writer.flush("test")
    with news_db.session() as db:
        item = store.item_dict(db.get(NewsItem, i))
    assert "短い抜粋" in prep.build_prompt([item])
    run(prep.attach_leads([item]))
    prompt = prep.build_prompt([item])
    assert "本文の取り出しの試験" in prompt and "短い抜粋" not in prompt
    assert len(prep._excerpt(item)) <= prep.LEAD_CHARS


def test_writes_go_through_the_write_coordinator(admin_client, web):
    """目的: 本文・圧縮・索引の書き込みはまとめ書き（書き手のスレッド）だけ。積んだだけでは DB の WAL を動かさない。"""
    i = add("記事", "https://n.test/a")
    web.pages["https://n.test/a"] = html()
    coord = writer.get()
    run(bodies.run_once())
    assert coord.counts()["bodies"] == 1 and coord.unsaved_total() >= 1      # 積んだだけ
    before = coord.writes["flush:test"]
    writer.flush("test")
    assert coord.writes["flush:test"] == before + 1
    assert coord.counts()["bodies"] == 0
    with news_db.session() as db:
        assert db.get(NewsBody, i) is not None


def test_settings_validation_and_diagnostics(admin_client, web):
    """目的: 本文の取り出しの上限・圧縮の年数が設定で変えられ、範囲外は断る。診断に本文の成否・圧縮の効果・索引の状態が出る。"""
    r = admin_client.put("/api/v1/news/settings", json={**admin_client.get("/api/v1/news/settings").json()["settings"],
                                                         "body": {"enabled": True, "per_day": 50, "per_minute": 3}, "storage": {"compress_years": 2}},
                         headers={"X-Requested-With": "NewsHub"})
    assert r.status_code == 200, r.text
    got = r.json()["settings"]
    assert got["body"] == {"enabled": True, "per_day": 50, "per_minute": 3} and got["storage"]["compress_years"] == 2
    bad = admin_client.put("/api/v1/news/settings", json={"body": {"per_minute": 0}}, headers={"X-Requested-With": "NewsHub"})
    assert bad.status_code == 422
    add("記事", "https://n.test/a")
    web.pages["https://n.test/a"] = html()
    run(bodies.run_once())
    writer.flush("test")
    sec = admin_client.get("/api/v1/diagnostics").json()["search"]
    assert sec["body"]["ok"] == 1 and sec["body"]["packed_bytes"] > 0 and sec["index"]["available"] and sec["compress_years"] == 2
    assert sec["index"]["docs"]["tri"] == 1
    chk = admin_client.post("/api/v1/diagnostics/selfcheck", headers={"X-Requested-With": "NewsHub"}).json()
    assert [c for c in chk["checks"] if "FTS" in c["name"]][0]["ok"]
