"""目的: 取得のエンジン（v0.1）の試験 — 条件付きの取得（ETag・Last-Modified・304）・Adaptive Polling とバックオフ・重複の除去（URL の正規化・GUID・SimHash）・
CSV と OPML の取り込み／書き出し・出どころの種類。外へ通信しない（取得は net.http_get_conditional を模擬）。

根拠: 2026-11-24 利用者「ETag・Last-Modified の条件付きの取得…304 は変わりなしとして成功に数え本文を読まない」「間隔は基準の 1/2〜8 倍・失敗・429・503 は指数で延ばす（Retry-After を尊重・上限 24 時間）」
「URL の正規化・GUID・SimHash（64bit・ハミング距離 3 以下）を 48 時間の窓で。近い重複は捨てずに別の出どころとして代表にまとめる」
「CSV は列の名前が柔軟・取り込み前に確認の表・重複は飛ばして数える・書き出しは OPML と CSV」（ARCHITECTURE 10 章）。
守ること: 304 では本文を解析しない／間隔の上下限／Retry-After／近い重複を 1 件として出す／書き出しを取り込むと元に戻る。
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from qnapnewshub import db as news_db
from qnapnewshub import dedup, fetchlog, net, polling, portability, service, sourcemgr, sources, store, writer
from tests.conftest import CSRF_HEADERS

FEED = "https://a.test/feed.xml"
KEY = f"rss:{FEED}"


@pytest.fixture(autouse=True)
def isolated(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_path", lambda: tmp_path / "news" / "sources.json")
    polling.reset_memory()
    service._last_fetch.clear()

    async def no_images(ids):
        return {"tried": 0, "found": 0, "logos": 0}

    async def no_after(saved):
        return {"gist": {"done": 0, "error": ""}, "prep": {"done": 0, "calls": 0, "error": ""}}
    monkeypatch.setattr(service.images, "fill", no_images)
    monkeypatch.setattr(service, "_after_save", no_after)
    yield
    polling.reset_memory()


def rss(items: list[tuple[str, str, str]]) -> bytes:
    """(題, URL, 抜粋) の RSS 2.0。"""
    body = "".join(f"<item><title>{t}</title><link>{u}</link><guid>{u}</guid><description>{d}</description></item>" for t, u, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode()


def use_feed(monkeypatch, responder):
    """net.http_get_conditional を差し替える。responder(url, etag, last_modified) -> (status, headers, body) か例外。呼ばれた引数を calls に残す。"""
    calls: list[dict] = []

    def fake(url, *, etag="", last_modified="", pace=1.0, timeout=30):
        calls.append({"url": url, "etag": etag, "last_modified": last_modified})
        return responder(url, etag, last_modified)
    monkeypatch.setattr(net, "http_get_conditional", fake)
    sources.save_config({"rss": [{"name": "A", "url": FEED}]})
    return calls


def refresh_once():
    return asyncio.run(service.refresh(force=True, keys=[KEY]))


# ---- URL の正規化 ----

@pytest.mark.parametrize("a,b", [
    ("http://www.example.com/post/1/?utm_source=x&utm_medium=y", "https://example.com/post/1"),
    ("https://example.com/post/1#section", "https://example.com/post/1"),
    ("https://example.com/post/1/amp", "https://example.com/post/1"),
    ("https://example.com/post/1.amp.html", "https://example.com/post/1.html".replace(".html", "")),
    ("https://amp.example.com/post/1?amp=1", "https://example.com/post/1"),
    ("https://example.com/post/1?output=amp&fbclid=abc", "https://example.com/post/1"),
    ("https://EXAMPLE.com:443/post/1", "https://example.com/post/1"),
    ("https://example.com/p?b=2&a=1&gclid=z", "https://example.com/p?a=1&b=2"),
])
def test_normalize_url_collapses_tracking_amp_scheme_and_trailing_slash(a, b):
    assert dedup.normalize_url(a) == dedup.normalize_url(b)


def test_normalize_url_keeps_real_differences():
    assert dedup.normalize_url("https://example.com/post/1") != dedup.normalize_url("https://example.com/post/2")
    assert dedup.normalize_url("https://example.com/p?id=1") != dedup.normalize_url("https://example.com/p?id=2")
    assert dedup.normalize_url("ftp://x") == "ftp://x"      # 読めない URL はそのまま


# ---- SimHash ----

def test_simhash_near_duplicates_within_3_bits_and_unrelated_far():
    base = "AMD が新しい GPU を発表した。ROCm 7 に対応し、ローカル LLM の推論が最大 2 倍速くなるという。価格は 1,299 ドルから。発売は来月の予定。"
    h = dedup.simhash(base)
    assert h is not None and -(1 << 63) <= h < (1 << 63)
    assert dedup.hamming(h, dedup.simhash(base + "。")) <= dedup.HAMMING_MAX            # 句点 1 つの違い
    assert dedup.hamming(h, dedup.simhash(base.replace("AMD", "ＡＭＤ"))) <= dedup.HAMMING_MAX   # 全角・半角の違い
    other = dedup.simhash("Linux カーネル 7.0 の最初の候補版が公開された。新しいスケジューラと Rust のドライバが追加された。")
    assert dedup.hamming(h, other) > 10
    assert dedup.simhash("短い題") is None                 # 短すぎる文は作らない（偶然の一致でまとめない）


def item(title, url, summary="", source_key="rss:https://a.test/f", **kw):
    base = {"kind": "news", "source": source_key, "source_key": source_key, "title": title, "url": url, "summary": summary,
            "image_url": None, "published_at": datetime.now(timezone.utc), "extra_topics": ()}
    return {**base, **kw}


LONG = "AMD が新しい GPU を発表した。ROCm 7 に対応し、ローカル LLM の推論が最大 2 倍速くなるという。価格は 1,299 ドルから。発売は来月の予定。"


def test_annotate_merges_cross_source_drops_same_source_and_guid():
    known = dedup.Known()
    batch = [
        item("GPU 発表", "https://x.test/a/1", LONG, "rss:https://x.test/f"),
        item("GPU 発表（転載）", "http://www.x.test/a/1/?utm_source=tw", LONG, "rss:https://x.test/f"),      # 同じ取得先の URL の揺れ: 捨てる
        item("別の見出し", "https://y.test/news/9", LONG + "。", "rss:https://y.test/f"),                    # 別の取得先・近い本文: まとめる
        item("無関係の記事のタイトルです", "https://z.test/1", "Linux カーネル 7.0 の最初の候補版が公開された。新しいスケジューラが入った。", "rss:https://z.test/f"),
        item("名前の変わった記事のタイトルです", "https://z.test/2-new", "別の本文で、まったく違う内容の抜粋です。ここは長めに書く。", "rss:https://z.test/f", guid="g-1"),
        item("名前の変わった記事のタイトルです", "https://z.test/2-renamed", "別の本文で、まったく違う内容の抜粋です。ここは長めに書く。", "rss:https://z.test/f", guid="g-1"),   # 同じ GUID: 捨てる
    ]
    kept, stats = dedup.annotate(batch, known)
    urls = [i["url"] for i in kept]
    assert "http://www.x.test/a/1/?utm_source=tw" not in urls and "https://z.test/2-renamed" not in urls
    merged = next(i for i in kept if i["url"] == "https://y.test/news/9")
    assert merged["dup_of_url"] == "https://x.test/a/1" and not merged.get("dup_of")
    assert stats["dropped_same_source"] == 1 and stats["dropped_guid"] == 1 and stats["merged_simhash"] == 1


def test_near_duplicates_are_saved_but_listed_as_one_with_also_count(admin_client):
    """近い重複は捨てず保存し、一覧には代表の 1 件だけを出す（also = 別の出どころの数）。"""
    rep = item("GPU 発表", "https://x.test/a/1", LONG, "rss:https://x.test/f")
    dup1 = item("別の見出し", "https://y.test/news/9", LONG + "。", "rss:https://y.test/f")
    dup2 = item("さらに別の見出し", "https://w.test/p", LONG, "rss:https://w.test/f")
    kept, _ = dedup.prepare([rep, dup1, dup2])
    saved = store.stage_items(kept)
    assert saved["new"] == 3 and len(saved["new_ids"]) == 1       # 重複は下ごしらえ・画像の仕事に回さない
    writer.flush("manual")
    page = admin_client.get("/api/v1/news/items?view=latest").json()
    assert len(page["items"]) == 1
    only = page["items"][0]
    assert only["url"] == "https://x.test/a/1" and only["also"] == 2 and set(only["also_sources"]) == {"rss:https://y.test/f", "rss:https://w.test/f"}
    with news_db.session() as db:
        from sqlalchemy import func, select
        from qnapnewshub.db import NewsItem
        assert db.scalar(select(func.count(NewsItem.id))) == 3
        assert db.scalar(select(func.count(NewsItem.id)).where(NewsItem.dup_of.is_not(None))) == 2
    # 次の取得で、すでにまとまった記事（代表の別名）の近い重複が来ても、代表へ付く
    again, _ = dedup.prepare([item("またの見出し", "https://v.test/q", LONG, "rss:https://v.test/f")])
    assert again[0]["dup_of"] and not again[0].get("dup_of_url")


def test_window_is_48_hours():
    known = dedup.Known()
    known.add({"id": 1, "url": "https://old.test/1", "source_key": "k1", "canon_url": "https://old.test/1", "guid": "", "simhash": dedup.simhash(LONG), "dup_of": None})
    kept, _ = dedup.annotate([item("t", "https://n.test/1", LONG, "k2")], known)
    assert kept[0]["dup_of"] == 1
    # 窓の外（48 時間より前に入った記事）は DB から索引に読まれない
    with news_db.session() as db:
        from qnapnewshub.db import NewsItem
        old = datetime.now(timezone.utc) - timedelta(hours=60)
        db.add(NewsItem(kind="news", source="s", source_key="k1", title="古い", url="https://o.test/1", published_at=old, fetched_at=old, simhash=dedup.simhash(LONG), canon_url="https://o.test/1"))
        db.commit()
    kept, _ = dedup.prepare([item("t", "https://n2.test/1", LONG, "k2")])
    assert not kept[0].get("dup_of") and not kept[0].get("dup_of_url")


# ---- 条件付きの取得（304） ----

def test_second_fetch_sends_validators_and_304_does_not_parse_body(monkeypatch):
    """304 は変わりなしの成功。本文を解析せず、履歴に 304 を残し、当たり率に数える。"""
    def responder(url, etag, lm):
        if etag:
            assert etag == '"v1"' and lm == "Tue, 24 Nov 2026 01:00:00 GMT"
            return 304, {}, b""
        return 200, {"etag": '"v1"', "last-modified": "Tue, 24 Nov 2026 01:00:00 GMT"}, rss([("最初の記事のタイトルです", "https://a.test/1", "抜粋")])
    calls = use_feed(monkeypatch, responder)
    assert refresh_once()["new"] == 1
    assert calls[0]["etag"] == "" and polling.get(KEY)["etag"] == '"v1"'

    def boom(*a, **k):
        raise AssertionError("304 の本文を解析してはいけない")
    monkeypatch.setattr(sources, "parse_feed", boom)
    result = refresh_once()
    assert result["new"] == 0 and result["failed"] == 0
    assert calls[1]["etag"] == '"v1"' and calls[1]["last_modified"] == "Tue, 24 Nov 2026 01:00:00 GMT"
    with news_db.session() as db:
        last = fetchlog.history(db, KEY)[0]
    assert last["ok"] and last["status"] == 304 and last["count"] == 0
    snap = polling.snapshot(KEY, 30, False)
    assert snap["conditional"] == 1 and snap["hits"] == 1 and snap["hit_rate"] == 100


def test_validators_are_not_saved_when_parse_fails_and_state_is_persisted(monkeypatch):
    use_feed(monkeypatch, lambda u, e, l: (200, {"etag": '"bad"'}, b"<rss><broken"))
    assert refresh_once()["failed"] == 1
    assert not polling.get(KEY).get("etag")          # 読めなかった応答の ETag を覚えない（次も全部取る）
    use_feed(monkeypatch, lambda u, e, l: (200, {"etag": '"ok"'}, rss([("記事のタイトルです長め", "https://a.test/1", "")])))
    refresh_once()
    writer.flush("manual")
    saved = json.loads((news_db.path().parent / "fetch-state.json").read_text())
    assert saved[KEY]["etag"] == '"ok"'


def test_aget_conditional_sends_headers_and_429_carries_retry_after(monkeypatch):
    seen = {}

    async def fake_get(url, **kw):
        seen["headers"] = kw.get("headers")
        return seen["reply"]
    monkeypatch.setattr(net, "get", fake_get)
    seen["reply"] = (304, {"etag": '"a"'}, b"")
    status, _, body = asyncio.run(net.aget_conditional("https://a.test/f", etag='"a"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT"))
    assert status == 304 and body == b"" and seen["headers"] == {"If-None-Match": '"a"', "If-Modified-Since": "Mon, 01 Jan 2026 00:00:00 GMT"}
    seen["reply"] = (429, {"retry-after": "120"}, b"slow down")
    with pytest.raises(net.HttpError) as info:
        asyncio.run(net.aget_conditional("https://a.test/f"))
    assert info.value.status == 429 and info.value.retry_after == 120 and seen["headers"] is None
    assert net.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT", now=0) > 1_000_000 and net.parse_retry_after("x") is None


# ---- Adaptive Polling とバックオフ ----

def test_interval_shrinks_with_new_items_and_grows_without_within_half_to_8x():
    base, now = 60.0, 1_000_000.0
    st: dict = {}
    for i in range(12):                        # 新着が毎回: 基準の 1/2 まで縮む（それ以上は縮まない）
        polling.adapt(st, base, 3, now + i * 1800)
    assert st["interval"] == pytest.approx(base * 0.5)
    st = {}
    for i in range(30):                        # 新着が無いまま: 基準の 8 倍まで伸びる（それ以上は伸びない）
        polling.adapt(st, base, 0, now + i * 3600)
    assert st["interval"] == pytest.approx(base * 8)


def test_interval_follows_arrival_gap_ema():
    """6 時間おきに新着が出る取得先は、基準 60 分でも 3 時間の間隔（出る間隔の半分）に落ち着く。"""
    base, now, st = 60.0, 2_000_000.0, {}
    for i in range(6):
        polling.adapt(st, base, 2, now + i * 6 * 3600)
    assert st["interval"] == pytest.approx(180.0, rel=0.05)


def test_backoff_doubles_honors_retry_after_and_caps_at_24h():
    assert polling.backoff_minutes(30, 1) == 60 and polling.backoff_minutes(30, 2) == 120
    assert polling.backoff_minutes(30, 1, retry_after=3 * 3600) == 180            # Retry-After が長ければそちら
    assert polling.backoff_minutes(30, 1, retry_after=30) == 60                    # 短ければ指数のまま
    assert polling.backoff_minutes(30, 12) == 24 * 60 and polling.backoff_minutes(30, 1, retry_after=10 * 86400) == 24 * 60


def test_failure_429_with_retry_after_extends_next_fetch_and_success_restores(monkeypatch):
    def limited(url, etag, lm):
        raise net.HttpError(429, 4 * 3600)
    use_feed(monkeypatch, limited)
    sources.save_config({"rss": [{"name": "A", "url": FEED}], "intervals": {KEY: 30}})
    assert refresh_once()["failed"] == 1
    st = polling.get(KEY)
    assert st["fails"] == 1 and st["backoff"] == 240          # Retry-After 4 時間 > 指数の 60 分
    assert polling.snapshot(KEY, 30, False)["interval"] == 240 and polling.snapshot(KEY, 30, True)["backoff"]      # 固定でも失敗では延ばす
    with news_db.session() as db:
        assert fetchlog.history(db, KEY)[0]["status"] == 429
    use_feed(monkeypatch, lambda u, e, l: (200, {}, rss([("記事のタイトルです長め", "https://a.test/1", "")])))
    sources.save_config({"rss": [{"name": "A", "url": FEED}], "intervals": {KEY: 30}})
    refresh_once()
    assert polling.get(KEY)["fails"] == 0 and polling.snapshot(KEY, 30, False)["interval"] <= 30 * 1.5 + 1


def test_fixed_source_keeps_base_interval():
    st = {}
    for _ in range(10):
        polling.record(KEY, base=60, fixed=True, ok=True, new=0, now=1000.0)
    snap = polling.snapshot(KEY, 60, True)
    assert snap["interval"] == 60 and snap["auto"] is False
    assert polling.snapshot(KEY, 60, False)["auto"] is True


def test_due_uses_effective_interval_and_restart_continues_from_saved_time(monkeypatch):
    import time as _time

    src = sources.Source(KEY, "A", "news", lambda: None)
    polling.record(KEY, base=30, fixed=False, ok=False, status=503, now=_time.time() - 600)       # 10 分前に失敗 → 待ちは 60 分
    cfg = sources.load_config()
    eff = service._effective_interval(src, cfg, 30)
    assert eff == 60
    service._last_fetch.clear()
    service._seed_last_fetch([src])
    mono = _time.monotonic()
    assert 590 <= mono - service._last_fetch[KEY] <= 700            # 起動し直しても 10 分前に取ったことを覚えている
    from dataclasses import replace
    assert service.due_keys([replace(src, interval=eff)], service._last_fetch, mono, 30) == []
    assert service.due_keys([replace(src, interval=eff)], service._last_fetch, mono + 3600, 30) == [KEY]


# ---- CSV・OPML ----

def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_csv_header_aliases_encodings_and_confirmation_table():
    csv_text = "サイト名;フィードURL;カテゴリ;種類;間隔;有効\nGIGAZINE;https://gigazine.net/news/rss_2.0/;ニュース;報道;60;はい\n公式ブログ;http://blog.example.com/feed/;企業;公式;;×\nだめ;ftp://x/y;;;;\nGIGAZINE 2;https://gigazine.net/news/rss_2.0;;;;\n"
    for enc in ("utf-8-sig", "cp932"):
        pre = portability.preview(sources.normalize({"rss": []}), csv_text.encode(enc))
        assert pre["format"] == "csv" and pre["counts"] == {"new": 2, "duplicate": 1, "invalid": 1}, enc
        first, second, bad, dup = pre["rows"]
        assert first["name"] == "GIGAZINE" and first["origin"] == "press" and first["interval"] == 60 and first["category"] == "ニュース"
        assert second["url"] == "https://blog.example.com/feed/" and "https" in second["reason"] and second["enabled"] is False
        assert bad["status"] == "invalid" and dup["status"] == "duplicate" and "重複" in dup["reason"]


@pytest.mark.parametrize("header", ["Name,URL,Category", "TITLE , Feed , Group", "名前,アドレス,分類", "name\txmlUrl\tfolder"])
def test_csv_column_name_variants(header):
    sep = "\t" if "\t" in header else ","
    text = f"{header}\nA{sep}https://a.test/feed{sep}x\n"
    pre = portability.preview(sources.normalize({}), text.encode())
    assert pre["counts"]["new"] == 1 and pre["rows"][0]["url"] == "https://a.test/feed" and pre["rows"][0]["category"] == "x"


def test_csv_without_url_column_is_rejected_with_message():
    with pytest.raises(portability.ImportError_, match="URL の列"):
        portability.preview(sources.normalize({}), "名前,メモ\nA,b\n".encode())


OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0"><head><title>x</title></head><body>
<outline text="AI" title="AI"><outline type="rss" text="Feed One" xmlUrl="https://one.test/rss" htmlUrl="https://one.test/"/>
<outline type="rss" text="Feed Two" xmlUrl="https://two.test/atom"/></outline>
<outline type="rss" text="Top" xmlUrl="https://top.test/rss"/><outline type="rss" text="Again" xmlUrl="http://ONE.test/rss/"/>
</body></opml>"""


def test_opml_import_folders_become_categories_and_duplicates_are_counted():
    pre = portability.preview(sources.normalize({}), OPML.encode())
    assert pre["format"] == "opml" and pre["counts"] == {"new": 3, "duplicate": 1, "invalid": 0}
    assert [r["category"] for r in pre["rows"]] == ["AI", "AI", "", ""]
    assert pre["rows"][3]["status"] == "duplicate"


def test_opml_with_dtd_is_rejected():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><opml><body><outline xmlUrl="https://a.test/f"/></body></opml>'
    with pytest.raises(portability.ImportError_, match="DTD"):
        portability.parse_opml(evil)


def test_export_then_import_roundtrip_csv_and_opml():
    cfg = sources.normalize({"rss": [{"name": "A, \"quoted\"", "url": "https://a.test/f"}, {"name": "B", "url": "https://b.test/f"}], "pages": [{"name": "P", "url": "https://p.test/list"}],
                             "github": ["o/r"], "discourse": [], "reddit": [], "hackernews": False, "huggingface": False, "intervals": {"rss:https://a.test/f": 120}, "disabled": ["rss:https://b.test/f"],
                             "origins": {"rss:https://a.test/f": "blog"}, "categories": {"rss:https://a.test/f": "メモ"}})
    for fmt, dump in (("csv", portability.export_csv), ("opml", portability.export_opml)):
        text = dump(cfg)
        pre = portability.preview(sources.normalize({"rss": [], "github": [], "discourse": [], "reddit": [], "hackernews": False, "huggingface": False}), text.encode())
        assert pre["format"] == fmt and pre["counts"]["new"] == 4, fmt
        by = {r["url"]: r for r in pre["rows"]}
        a = by["https://a.test/f"]
        assert a["name"] == 'A, "quoted"' and a["category"] == "メモ" and a["origin"] == "blog" and a["interval"] == 120 and a["enabled"]
        assert by["https://b.test/f"]["enabled"] is False
        assert by["https://p.test/list"]["kind"] == "page" and "https://github.com/o/r/releases.atom" in by


def test_import_api_preview_commit_direct_disabled_and_check_mode(admin_client, monkeypatch):
    c = admin_client
    csv_text = "name,url,category\nOne,https://one.test/rss,AI\nTwo,https://two.test/rss,AI\nBroken,https://broken.test/rss,\n"
    pre = c.post("/api/v1/news/sources/import/preview", json={"data": b64(csv_text.encode())}, headers=CSRF_HEADERS)
    assert pre.status_code == 200 and pre.json()["counts"]["new"] == 3
    assert c.post("/api/v1/news/sources/import/preview", json={"data": b64(b"")}, headers=CSRF_HEADERS).status_code == 422
    rows = pre.json()["rows"]
    direct = c.post("/api/v1/news/sources/import/commit", json={"mode": "direct", "rows": rows[:1]}, headers=CSRF_HEADERS).json()
    assert direct["added"] == 1
    by = {i["key"]: i for i in direct["items"]}
    assert by["rss:https://one.test/rss"]["enabled"] is False and by["rss:https://one.test/rss"]["category"] == "AI"      # そのまま追加は無効で
    again = c.post("/api/v1/news/sources/import/commit", json={"mode": "direct", "rows": rows[:2]}, headers=CSRF_HEADERS).json()
    assert again["added"] == 1 and again["duplicate"] == 1                                                              # 同じ URL は飛ばして数える

    async def fake_check(kind, value, name=""):
        return {"ok": "broken" not in value, "error": {"message": "HTTP 404"} if "broken" in value else None}
    monkeypatch.setattr("qnapnewshub.router.probe.check", fake_check)
    more = [{**rows[2], "url": "https://ok.test/rss", "name": "OK"}, rows[2]]
    checked = c.post("/api/v1/news/sources/import/commit", json={"mode": "check", "rows": more}, headers=CSRF_HEADERS).json()
    assert checked["added"] == 1 and [f["reason"] for f in checked["failed"]] == ["HTTP 404"]
    by = {i["key"]: i for i in checked["items"]}
    assert by["rss:https://ok.test/rss"]["enabled"] is True and "rss:https://broken.test/rss" not in by           # 取れなかったものは足さない


def test_export_endpoint_downloads(admin_client):
    c = admin_client
    c.post("/api/v1/news/sources/manage/add", json={"items": [{"kind": "rss", "value": "https://a.test/f", "name": "A"}]}, headers=CSRF_HEADERS)
    o = c.get("/api/v1/news/sources/export?format=opml")
    assert o.status_code == 200 and "attachment" in o.headers["content-disposition"] and 'xmlUrl="https://a.test/f"' in o.text
    s = c.get("/api/v1/news/sources/export?format=csv")
    assert s.text.lstrip("﻿").startswith("name,url,category,origin,type,interval,enabled") and "https://a.test/f" in s.text
    assert c.get("/api/v1/news/sources/export?format=xml").status_code == 422


# ---- 出どころの種類・固定・管理の画面 ----

@pytest.mark.parametrize("kind,value,expected", [
    ("github", "o/r", "official"), ("hackernews", "", "community"), ("rss", "https://rss.arxiv.org/rss/cs.AI", "paper"),
    ("rss", "https://gigazine.net/news/rss_2.0/", "press"), ("rss", "https://zenn.dev/feed", "blog"), ("rss", "https://blog.example.org/feed", "official"),
    ("rss", "https://www.reddit.com/r/x/.rss", "community"), ("rss", "https://unknown.example.net/rss", "other"),
])
def test_origin_inference_by_domain(kind, value, expected):
    assert sourcemgr.infer_origin(kind, value, "") == expected


def test_manage_list_shows_next_fetch_and_interval_and_bulk_fixed_origin(admin_client, monkeypatch):
    c = admin_client
    c.post("/api/v1/news/sources/manage/add", json={"items": [{"kind": "rss", "value": FEED, "name": "A", "interval": 30}]}, headers=CSRF_HEADERS)
    row = {i["key"]: i for i in c.get("/api/v1/news/sources/manage").json()["items"]}[KEY]
    assert row["poll"]["next_at"] == "" and row["poll"]["auto"] is True and row["origin"] == "other" and row["fixed"] is False
    polling.record(KEY, base=30, fixed=False, ok=True, new=0, now=datetime.now(timezone.utc).timestamp())
    row = {i["key"]: i for i in c.get("/api/v1/news/sources/manage").json()["items"]}[KEY]
    assert row["poll"]["next_at"] and row["poll"]["interval"] > 30                      # 新着が無く、基準より長い間隔へ
    for body in ({"action": "fixed", "keys": [KEY]}, {"action": "origin", "keys": [KEY], "origin": "blog"}):
        assert c.post("/api/v1/news/sources/manage/bulk", json=body, headers=CSRF_HEADERS).status_code == 200
    row = {i["key"]: i for i in c.get("/api/v1/news/sources/manage").json()["items"]}[KEY]
    assert row["fixed"] and row["origin"] == "blog" and row["origin_set"] and row["poll"]["auto"] is False and row["poll"]["interval"] == 30
    c.post("/api/v1/news/sources/manage/bulk", json={"action": "origin", "keys": [KEY], "origin": "auto"}, headers=CSRF_HEADERS)
    assert not {i["key"]: i for i in c.get("/api/v1/news/sources/manage").json()["items"]}[KEY]["origin_set"]
    assert c.post("/api/v1/news/sources/manage/bulk", json={"action": "origin", "keys": [KEY], "origin": "bogus"}, headers=CSRF_HEADERS).status_code == 422


def test_diagnostics_reports_hit_rate(admin_client, monkeypatch):
    use_feed(monkeypatch, lambda u, e, l: (304, {}, b"") if e else (200, {"etag": '"v"'}, rss([("記事のタイトルです長め", "https://a.test/1", "")])))
    refresh_once()
    refresh_once()
    fetch = admin_client.get("/api/v1/diagnostics").json()["fetch"]
    assert fetch["hit_rate"] == 100 and fetch["hits_304"] == 1 and fetch["conditional"] == 1
    assert fetch["sources"][0]["hit_rate"] == 100 and "interval_auto" in fetch["sources"][0]


def test_old_database_gets_dedup_columns(tmp_path, monkeypatch):
    import sqlite3

    from qnapnewshub import db as module

    file = tmp_path / "old" / "news" / "news.sqlite3"
    file.parent.mkdir(parents=True)
    with sqlite3.connect(file) as c:
        c.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY, kind TEXT, fetched_at TEXT)")
    monkeypatch.setattr(module, "path", lambda: file)
    try:
        module.engine()
        with sqlite3.connect(file) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(news_items)")}
            idx = {r[1] for r in c.execute("PRAGMA index_list(news_items)")}
        assert {"canon_url", "guid", "simhash", "dup_of"} <= cols and "ix_news_items_dup_of" in idx
    finally:
        module._engines.pop(str(file), None)
