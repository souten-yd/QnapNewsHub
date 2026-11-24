"""分析（集計の表・ホットトピック・API）の試験。

目的: ①増えた分だけ足した集計が、既存の記事からの作り直しと一致する ②近い重複を独立した記事に数えない
③ホットトピックで常に多い語が上位に来ず、急に増えた語が上に来る・新しく出た語は別の枠 ④期間・絞り込み・読み取りのトークン
⑤集計の書き込みはまとめ書きのときだけ（待機中・読むだけで DB が変わらない）。
根拠: 2026-11-24 利用者「1 日 1000 件は全部見られないので分析して…グラフで見たい」。ARCHITECTURE 7・7.5・9 章
（「AI」「ゲーム」が並ぶだけにしない・待機中の書き込み 0）。外への通信・LLM は使わない。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from qnapnewshub import analytics, sources, store, writer
from qnapnewshub import db as news_db
from qnapnewshub.db import AnaCount, AnaFetch, AnaTopic, AnaTopicSrc, NewsItem

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def mk(i: int, *, hours: float = 1, kind="news", src="a", title=None, image=None, topics=None) -> dict:
    it = sources.make_item(kind=kind, source=src, source_key=src, title=title or f"Quantization update number {i}", url=f"https://x.test/{src}/{i}", summary="s",
                           image=image, published=NOW() - timedelta(hours=hours))
    if topics:
        it["extra_topics"] = tuple(topics)
    return it


def dump() -> dict:
    out = {}
    with news_db.session() as db:
        for model in (AnaCount, AnaTopic, AnaTopicSrc, AnaFetch):
            rows = db.execute(select(model)).scalars().all()
            out[model.__tablename__] = sorted(tuple(getattr(r, c.name) for c in model.__table__.columns if c.name != "label") for r in rows)
    return out


def test_incremental_equals_rebuild(client):
    fetch = [{"source_key": "a", "at": NOW(), "ok": True, "status": 200, "count": 3, "new": 3, "ms": 5, "error": "", "skipped": ""},
             {"source_key": "a", "at": NOW(), "ok": True, "status": 304, "count": 0, "new": 0, "ms": 5, "error": "", "skipped": ""}]
    store.stage_items([mk(i, hours=i, image="https://i.test/1.png" if i % 2 else None, topics=["Qwen"]) for i in range(6)])
    writer.flush("t")         # 集計の表がまだ無いので、ここで作り直しから始まる
    writer.get().stage_fetchlog(fetch[:1])
    store.stage_items([mk(i, hours=i % 5, src="b", title=f"日本語のニュース ロケット打ち上げ {i}") for i in range(10, 15)])
    writer.flush("t")
    writer.get().stage_fetchlog(fetch[1:])
    store.stage_items([mk(20, hours=2, src="a")])
    writer.flush("t")
    inc = dump()
    assert inc["ana_count"] and inc["ana_topic"] and inc["ana_fetch"]
    analytics.rebuild("test")
    assert dump() == inc


def test_image_gained_later_is_counted(client):
    store.stage_items([mk(1)])
    writer.flush("t")
    with news_db.session() as db:
        item = db.scalar(select(NewsItem))
        item_id = item.id
    writer.get().patch_item(item_id, set={"image_url": "https://i.test/z.png"})
    writer.flush("t")
    inc = dump()
    analytics.rebuild("test")
    assert dump() == inc
    with news_db.session() as db:
        assert analytics.sources_analysis(db)["sources"][0]["image_pct"] == 100.0


def test_near_duplicates_are_not_independent(client):
    # 記事は「今」公開にする（既定の 1 時間前だと、0 時〜1 時に流すと「昨日」に数えられて落ちた。2026-11-25 0:03）
    first = mk(1, hours=0, src="a", title="Same story about a new GPU driver release today")
    store.stage_items([first])
    writer.flush("t")
    with news_db.session() as db:
        rep = db.scalar(select(NewsItem.id))
    dup = mk(2, hours=0, src="b", title="Same story about a new GPU driver release today")
    dup["dup_of"] = rep
    store.stage_items([dup])
    writer.flush("t")
    with news_db.session() as db:
        ov = analytics.overview(db)
        assert ov["today"] == 2 and ov["today_indep"] == 1
        s = analytics.series(db, range_="24h", metric="indep")
        assert s["total"] == 1
        src = {r["key"]: r for r in analytics.sources_analysis(db)["sources"]}
        assert src["b"]["dup_pct"] == 100.0 and src["a"]["dup_pct"] == 0.0


def seed_topics(db, rows: list[tuple[str, float, str, list[str]]]) -> None:
    """(題, 何時間前, 取得先, 話題)。"""
    for n, (title, hours, src, topics) in enumerate(rows):
        db.add(NewsItem(kind="news", source=src, source_key=src, title=title, url=f"https://t.test/{n}", summary="", published_at=NOW() - timedelta(hours=hours),
                        topics=json.dumps(topics, ensure_ascii=False)))
    db.commit()


def test_hot_topics_generic_word_stays_low_and_surge_and_new_word(client):
    rows = []
    for d in range(2, 20):                      # 過去 18 日: AI は毎日 20 件、rocm は毎日 1 件
        rows += [(f"daily ai item {d} {k}", 24 * d + 3 + k * 0.1, f"s{k % 4}", ["AI"]) for k in range(20)]
        rows += [(f"rocm note {d}", 24 * d + 5, "s0", ["ROCm"])]
    rows += [(f"daily ai today {k}", 1 + k * 0.5, f"s{k % 4}", ["AI"]) for k in range(23)]          # 今日の AI は 23 件（少しだけ多い）
    rows += [(f"rocm big news {k}", 1 + k * 0.7, f"s{k % 3}", ["ROCm"]) for k in range(12)]        # rocm は 12 件に急増
    rows += [(f"Zorblax launch {k}", 1 + k * 0.7, f"s{k}", ["Zorblax"]) for k in range(4)]        # 初めて出た語（3 つの出どころ）
    with news_db.session() as db:
        seed_topics(db, rows)
    analytics.rebuild("test")
    with news_db.session() as db:
        hot = analytics.hot_topics(db)
    assert hot["enough_history"]
    labels = [r["label"] for r in hot["top"]]
    assert labels[0] == "ROCm"
    assert "AI" not in labels[:1] and (labels.index("AI") > labels.index("ROCm") if "AI" in labels else True)
    new = [r["label"] for r in hot["new"]]
    assert "Zorblax" in new and "Zorblax" not in labels and "AI" not in new and "ROCm" not in new
    assert hot["new"][0]["sources"] >= 2 and len(hot["new"][0]["spark"]) == 8


def test_filters_and_ranges(client):
    store.stage_items([mk(1, kind="news", hours=1, title="English headline words here"), mk(2, kind="release", hours=30, title="日本語の見出しです"),
                       mk(3, kind="news", hours=24 * 20, title="old English article text")])
    writer.flush("t")
    with news_db.session() as db:
        assert analytics.series(db, range_="24h")["total"] == 1
        assert analytics.series(db, range_="7d")["total"] == 2
        assert analytics.series(db, range_="30d")["total"] == 3
        assert analytics.series(db, range_="30d", lang="ja")["total"] == 1
        assert analytics.series(db, range_="30d", kind="release")["total"] == 1
        by_kind = analytics.series(db, range_="30d", by="kind")
        assert {s["key"] for s in by_kind["series"]} == {"news", "release"}
        assert analytics.compare(db)["total_this"] == 2


def test_api_reads_with_read_token_and_requires_login(client, anon_client):
    store.stage_items([mk(1)])
    writer.flush("t")
    token = client.post("/api/v1/auth/tokens", json={"name": "t"}).json()["token"]
    anon = anon_client.__class__(anon_client.app)
    for path in ("overview", "series?range=7d&by=source", "hot-topics", "sources", "compare", "filters"):
        r = anon.get(f"/api/v1/analytics/{path}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, (path, r.text)
        assert anon.get(f"/api/v1/analytics/{path}").status_code == 401
    assert anon.post("/api/v1/analytics/rebuild", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert client.post("/api/v1/analytics/rebuild", headers={"X-Requested-With": "NewsHub"}).status_code == 200


def test_reads_do_not_write_and_idle_does_not_change_db(client):
    import os
    import time

    store.stage_items([mk(i, hours=i) for i in range(5)])
    assert dump()["ana_count"] == []                      # 積んだだけ（まとめ書きの前）では、集計の表は空
    writer.flush("t")
    base = news_db.path()

    def state():
        wal = base.with_name(base.name + "-wal")
        return (base.stat().st_mtime_ns, base.stat().st_size, wal.stat().st_mtime_ns if wal.exists() else 0, wal.stat().st_size if wal.exists() else 0)
    client.get("/api/v1/analytics/overview")
    before = state()
    time.sleep(1.1)
    for path in ("overview", "series", "hot-topics", "sources", "compare", "filters"):
        assert client.get(f"/api/v1/analytics/{path}").status_code == 200
    assert state() == before and os.path.exists(base)


def test_cache_is_invalidated_only_when_data_changes(client):
    store.stage_items([mk(1)])
    writer.flush("t")
    with news_db.session() as db:
        a = analytics.series(db, range_="7d")
        assert analytics.series(db, range_="7d") is a      # 同じ答え（キャッシュ）
        store.stage_items([mk(2)])
        writer.flush("t")
        b = analytics.series(db, range_="7d")
        assert b is not a and b["total"] == a["total"] + 1


def test_terms_extraction():
    t = analytics.title_terms("Nvidia releases new Blackwell GPU drivers; ゲーム用のカラオケアプリ")
    assert "blackwell" in t and "gpu drivers" in t and "the" not in t and "カラオケアプリ" in t
    assert analytics.lang_of("日本語") == "ja" and analytics.lang_of("English only") == "en"


def test_a_word_covered_by_a_longer_phrase_is_not_listed_twice():
    """目的: 「jensen」「huang」「jensen huang」のように同じ出来事の語が上位を埋めない。
    根拠: 2026-11-24 の実データの撮影で、新しく出てきた語の上位 6 つのうち 4 つが同じ人の名前だった。
    長い語の記事の数が短い語の 8 割以上なら、短い語を外す（ほかの文脈でも多く出る語は残す）。"""
    from qnapnewshub.analytics import _drop_subsumed

    rows = [{"term": "jensen", "count": 9}, {"term": "huang", "count": 9}, {"term": "jensen huang", "count": 9},
            {"term": "nvidia", "count": 18}, {"term": "nvidia ceo", "count": 5}]
    assert [r["term"] for r in _drop_subsumed(rows)] == ["jensen huang", "nvidia", "nvidia ceo"]
