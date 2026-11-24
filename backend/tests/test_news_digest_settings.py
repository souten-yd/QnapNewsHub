"""目的: 日刊・週刊のまとめ（外部の LLM・失敗したら見出しの一覧）・一言要約・更新のタイミング（まとめの時刻・曜日・取得しない時間帯）の試験。外へ通信しない（LLM は差し替え）。

根拠: 2026-11-23 利用者「ニュースページ及びフィードの更新タイミング、その際に利用する LLM は個別に設定できること。このデータは基本公開情報だと思うので OpenRouter の無料 LLM の利用を想定している」
「まとめがまとまってない。一つの大きなトピックに対しての傾向と、メインの技術・注目すべき内容」。ControlDeck の test_news_rag_llm.py から移した
（RAG・ローカル LLM・ゲートウェイ・通知の分は、ニュースハブに無いので外した）。
守ること: LLM が落ちたら印つきの見出しの一覧・鍵が無ければその回は作らない（理由を残す）・出来事と期間外の記事は渡さない・まとめの時刻・曜日・取得しない時間帯・
返事の検査（存在しない出典・日本語でない・傾向の行が無い）・作成中は見出しの一覧を先に見せる。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from qnapnewshub import writer
from qnapnewshub import digest, gist, llm, service, settings, store
from qnapnewshub import db as news_db
from qnapnewshub.db import NewsDigest, NewsItem
from tests.conftest import user_id
from tests.test_news import add

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


# ---- まとめ ----

def seed_articles(now=None):
    now = now or datetime.now(timezone.utc)
    add("https://d.test/1", title="Qwen の新モデルが出た", summary="量子化に対応", published=now - timedelta(hours=3), topics=["Qwen"])
    add("https://d.test/2", title="llama.cpp が高速化", summary="ROCm 対応", published=now - timedelta(hours=5), topics=["llama.cpp"])
    add("https://d.test/3", title="先月の記事 Qwen", published=now - timedelta(days=20), topics=["Qwen"])
    me = user_id("digest-user")
    add(f"/settings?news-event={me}-1", title="秘密の警報", kind="event", source="Example", uid=me, published=now - timedelta(hours=1))


GOOD_DIGEST = ("## ローカル LLM\n- 傾向: モデルの量子化と高速化が進んでいる。[1][2]\n- 主な技術・製品: Qwen の新モデルと llama.cpp。[1][2][9]\n- 注目点: ROCm 対応。[2]\n"
               "## 今日の 3 行\n- Qwen が出た。[1]\n- llama.cpp が速くなった。[2]\n- ROCm 対応が進む。[2]\n")


class Spy:
    def __init__(self, answer=GOOD_DIGEST):
        self.calls, self.answer = [], answer

    async def __call__(self, model, messages, **kw):
        self.calls.append((model, messages))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer



def test_digest_with_llm_cites_only_real_items_and_excludes_events_and_old_items(monkeypatch):
    seed_articles()
    spy = Spy()
    monkeypatch.setattr(llm, "complete", spy)
    row = run(digest.generate("daily", "2026-11-23"))
    assert row["fallback"] is False and row["error"] == "" and row["model"] == llm.DEFAULT_MODEL
    assert "[1]" in row["body"] and "[9]" not in row["body"]                          # 存在しない番号の出典は消す
    sent = "\n".join(m["content"] for m in spy.calls[0][1])
    assert "Qwen の新モデルが出た" in sent and "llama.cpp が高速化" in sent
    assert "秘密の警報" not in sent and "先月の記事" not in sent                        # 出来事・期間外は渡さない
    assert len(row["item_ids"]) == 2



def test_digest_falls_back_to_marked_headlines_when_llm_fails(monkeypatch):
    seed_articles()
    monkeypatch.setattr(llm, "complete", Spy(llm.LlmUnavailable("接続できません")))
    row = run(digest.generate("daily", "2026-11-23"))
    assert row["fallback"] is True and "接続できません" in row["error"]
    assert "- Qwen の新モデルが出た" in row["body"] and "llama.cpp が高速化" in row["body"] and "[1]" in row["body"]



def test_digest_api_lists_and_returns_sources(admin_client, monkeypatch):
    seed_articles()
    monkeypatch.setattr(llm, "complete", Spy())
    r = admin_client.post("/api/v1/news/digests/run", json={"period": "daily"})
    assert r.status_code == 200 and r.json()["period"] == "daily"
    listing = admin_client.get("/api/v1/news/digests").json()
    assert listing["latest"]["daily"]["label"] == "今朝のまとめ" and listing["latest"]["weekly"] is None
    detail = admin_client.get(f"/api/v1/news/digests/{listing['digests'][0]['id']}").json()
    assert {s["url"] for s in detail["sources"]} == {"https://d.test/1", "https://d.test/2"} and detail["sources"][0]["n"] == 1
    assert all(isinstance(s["topics"], list) for s in detail["sources"])     # 話題の絞り込みへ移る印に使う（2026-11-24 利用者）
    assert admin_client.get("/api/v1/news/digests/99999").status_code == 404
    assert admin_client.post("/api/v1/news/digests/run", json={"period": "monthly"}).status_code == 422



# ---- 一言要約と話題の語 ----

def test_gist_is_off_by_default_and_adds_gist_and_topics_when_on(monkeypatch):
    first = add("https://g.test/1", title="Qwen の新モデル", published=datetime.now(timezone.utc))
    rows = [store.item_dict(r) for r in news_db.session().scalars(select(NewsItem))]
    spy = Spy("1|新しいモデルが出た|Qwen, 量子化\n9|範囲外|X")
    monkeypatch.setattr(llm, "complete", spy)
    assert run(gist.enrich(rows)) == {"done": 0, "error": ""} and spy.calls == []        # 既定は切
    cfg = settings.load()
    cfg["llm"]["gist_enabled"] = True
    settings.save(cfg)
    assert run(gist.enrich(rows))["done"] == 1
    writer.flush("test")      # まとめ書きの前の分は、保存してから DB で確かめる
    with news_db.session() as db:
        row = db.get(NewsItem, first)
        assert row.gist == "新しいモデルが出た" and "量子化" in row.topics
    assert "Qwen の新モデル" in spy.calls[0][1][1]["content"]



# ---- 更新のタイミング ----

def at(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute)          # 2026-11-23 は月曜



def test_daily_digest_is_due_after_the_time_only():
    cfg = settings.normalize({"digest_weekly": {"enabled": False}})
    assert settings.due_digests(cfg, at(23, 6, 29)) == []
    assert settings.due_digests(cfg, at(23, 6, 30)) == [("daily", "2026-11-23")]
    assert settings.due_digests(cfg, at(23, 22)) == [("daily", "2026-11-23")]         # 寝ていた後でも作る
    off = settings.normalize({"digest_daily": {"enabled": False}, "digest_weekly": {"enabled": False}})
    assert settings.due_digests(off, at(23, 12)) == []


def test_weekly_digest_follows_weekday_and_time():
    cfg = settings.normalize({"digest_daily": {"enabled": False}})                       # 月曜 07:00
    assert settings.due_digests(cfg, at(23, 6, 59)) == [("weekly", "2026-11-16")]       # 月曜の朝、まだ 7 時前: 先週の予定
    assert settings.due_digests(cfg, at(23, 7, 0)) == [("weekly", "2026-11-23")]
    assert settings.due_digests(cfg, at(25, 9)) == [("weekly", "2026-11-23")]           # 水曜でも今週の月曜の分
    fri = settings.normalize({"digest_daily": {"enabled": False}, "digest_weekly": {"weekday": 4, "time": "18:00"}})
    assert settings.due_digests(fri, at(23, 12)) == [("weekly", "2026-11-20")]
    assert settings.due_digests(fri, at(27, 18, 0)) == [("weekly", "2026-11-27")]


def test_quiet_hours_including_past_midnight():
    cfg = settings.normalize({"quiet": {"enabled": True, "start": "22:00", "end": "06:00"}})
    assert settings.in_quiet(cfg, at(23, 23)) and settings.in_quiet(cfg, at(23, 5, 59)) and not settings.in_quiet(cfg, at(23, 6, 0)) and not settings.in_quiet(cfg, at(23, 12))
    assert not settings.in_quiet(settings.normalize({}), at(23, 3))                        # 既定は切
    day = settings.normalize({"quiet": {"enabled": True, "start": "01:00", "end": "05:00"}})
    assert settings.in_quiet(day, at(23, 1, 0)) and not settings.in_quiet(day, at(23, 5, 0))


def test_run_due_makes_each_missing_digest_once(monkeypatch):
    seed_articles()
    spy = Spy()
    monkeypatch.setattr(llm, "complete", spy)
    now = datetime(2026, 11, 23, 8, 0)
    made = run(digest.run_due(now))
    assert sorted(r["period"] for r in made) == ["daily", "weekly"]
    assert run(digest.run_due(now)) == [] and len(spy.calls) == 2                          # 2 回目は作らない


def test_news_loop_skips_fetch_in_quiet_hours(monkeypatch):
    calls = []

    async def fake_refresh(force=False):
        calls.append("refresh")
        return {"started": True, "new": 0, "failed": 0}

    async def fake_due(now=None):
        calls.append("due")
        return []

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        if seconds:
            raise asyncio.CancelledError      # 1 周したら止める（最初の待ち FIRST_DELAY は 0）
        await real_sleep(0)

    monkeypatch.setenv("NEWSHUB_AUTO", "1")
    monkeypatch.setattr(service, "refresh", fake_refresh)
    monkeypatch.setattr(digest, "run_due", fake_due)
    monkeypatch.setattr(service, "FIRST_DELAY_SECONDS", 0)
    cfg = settings.normalize({"quiet": {"enabled": True, "start": "00:00", "end": "23:59"}})
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(service.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(service.news_loop())
    assert calls == ["due"]                                                                # 取得はせず、まとめの確認だけ




# ---- まとめの形・重点・返事の検査・英語の訳（2026-11-23 利用者の要望） ----

def _focus_cfg(focus: str, **extra):
    cfg = settings.load()
    cfg["llm"]["digest_focus"] = focus
    cfg["llm"].update(extra)
    cfg["digest_weekly"]["enabled"] = False
    settings.save(cfg)


def test_focus_goes_into_the_prompt_and_prompt_asks_for_trend_tech_attention_and_three_lines(monkeypatch):
    """目的: 利用者が書いた重点の文がプロンプトに入り、話題ごとの傾向・主な技術・注目点と最後の 3 行を求めること。
    根拠: 2026-11-23 利用者「AI モデルの最新動向…日本語で入力して、それをベースにまとめてもらう」「傾向と、メインの技術、注目すべき内容」。"""
    seed_articles()
    _focus_cfg("AI モデルの最新動向\nAI 技術の最新動向")
    spy = Spy()
    monkeypatch.setattr(llm, "complete", spy)
    run(digest.generate("daily", "2026-11-23"))
    text = "\n".join(m["content"] for m in spy.calls[0][1])
    assert "AI モデルの最新動向" in text and "AI 技術の最新動向" in text and digest.FOCUS_NONE in text
    assert "傾向" in text and "主な技術・製品" in text and "注目点" in text and "今日の 3 行" in text
    _focus_cfg("")
    spy2 = Spy()
    monkeypatch.setattr(llm, "complete", spy2)
    run(digest.generate("daily", "2026-11-23"))
    assert "重点" not in "\n".join(m["content"] for m in spy2.calls[0][1])        # 重点が空なら枠も付けない


def test_weekly_prompt_uses_weekly_wording(monkeypatch):
    seed_articles()
    spy = Spy()
    monkeypatch.setattr(llm, "complete", spy)
    run(digest.generate("weekly", "2026-11-23"))
    text = spy.calls[0][1][1]["content"]
    assert "週の傾向" in text and "今週の 3 行" in text


def test_prompt_carries_focus_but_no_other_user_info(monkeypatch):
    """目的: 外部へ送る中身は、公開の記事の項目と重点の文だけ（出来事・利用者名・鍵は入らない）。
    根拠: 設計 2.4・AGENTS.md 4（秘密をログ・外へ出さない）。まとめの重点は利用者が設定に書いた文なので送ってよいが、他は送らない。"""
    seed_articles()
    _focus_cfg("特許の動向")
    spy = Spy()
    monkeypatch.setattr(llm, "complete", spy)
    run(digest.generate("daily", "2026-11-23"))
    text = "\n".join(m["content"] for m in spy.calls[0][1])
    assert "特許の動向" in text and "秘密の警報" not in text and "digest-user" not in text


def test_digest_without_key_makes_nothing_and_does_not_call_http(monkeypatch):
    """目的: OpenRouter の鍵が未設定なら、その回のまとめは作らず（本文は空・理由を残す）、通信もしないこと。見張りは「作った」と数えて毎分やり直さない。
    根拠: 設計 2.4（鍵は設定で入れる）。鍵が無いのに毎分 HTTP を試すと、ログが溢れ、鍵を入れた直後の混乱になる。"""
    seed_articles()
    _focus_cfg("")      # 週刊は切（まとめを 1 つに絞って確かめる）
    posts = []

    async def no_post(body, key):
        posts.append(1)
        return 200, "{}"

    monkeypatch.setattr(llm, "_post", no_post)
    row = run(digest.generate("daily", "2026-11-23"))
    assert row["body"] == "" and "鍵が未設定" in row["error"] and row["fallback"] is False and posts == []
    assert run(digest.run_due(datetime(2026, 11, 23, 23, 0))) == []


def test_focus_matching_articles_are_ranked_first(monkeypatch):
    now = datetime.now(timezone.utc)
    for i in range(8):
        add(f"https://f.test/{i}", title=f"一般の話題 {i}", published=now - timedelta(hours=i + 1))
    add("https://f.test/hit", title="特許の審査基準が変わる", published=now - timedelta(hours=20))
    with news_db.session() as db:
        groups = digest.gather(db, "daily", now, "特許")
    titles = [i["title"] for g in groups for i in g["items"]]
    assert titles[0] == "特許の審査基準が変わる"


def test_broken_answers_fall_back_to_headlines(monkeypatch):
    """目的: 存在しない [n] だらけ・見出し無し・引用無し・傾向無し・空の返事は、見出しの一覧（fallback）に落ちること。"""
    for answer in ("## A\n- 傾向: x [8][9]\n", "- 傾向: x [1]", "## A\n- 傾向: 引用が無い\n", "## A\n- 注目点: x [1]\n", "   "):
        with news_db.session() as db:
            db.execute(delete(NewsItem)); db.execute(delete(NewsDigest)); db.commit()
        seed_articles()
        monkeypatch.setattr(llm, "complete", Spy(answer))
        row = run(digest.generate("daily", "2026-11-23"))
        assert row["fallback"] is True and "壊れていました" in row["error"]
        assert "- Qwen の新モデルが出た" in row["body"]


def test_focus_setting_is_validated_and_limited(admin_client):
    assert settings.normalize({})["llm"]["digest_focus"] == "" and settings.normalize({})["llm"]["prep_enabled"] is True and settings.normalize({})["llm"]["prep_max_per_run"] == 100
    ok = admin_client.put("/api/v1/news/settings", json={"llm": {"digest_focus": "AI 技術の最新動向\n\x00AI モデルの最新動向"}})
    assert ok.status_code == 200 and ok.json()["settings"]["llm"]["digest_focus"] == "AI 技術の最新動向\nAI モデルの最新動向"
    too_long = admin_client.put("/api/v1/news/settings", json={"llm": {"digest_focus": "あ" * 1001}})
    assert too_long.status_code == 422 or too_long.status_code == 400



def test_digest_rejects_english_and_retries_once():
    """目的: まとめが日本語で書かれていなければ、1 回だけ理由を伝えて書き直させ、それでも英語なら見出しの一覧に落とすこと。

    根拠: 2026-11-23 本番で無料のモデル（Nemotron 3.5 Lightning）が、日本語でと指示しても英語でまとめを返した。
    """
    en = "## Local LLM\n- 傾向: x [1]\n- A new model was released with better efficiency and lower memory use [1]"
    en = en.replace("傾向: x [1]", "傾向: Models are getting smaller and faster on laptops today [1]")
    with pytest.raises(digest.BadDigest, match="日本語"):
        digest.validate_body(en, 3)
    ja = "## ローカル LLM\n- 傾向: ノート PC で動く小さなモデルが増えている [1]\n- 主な技術・製品: Inkling 2 Mini と Qwen 3.8 [2]"
    assert digest.validate_body(ja, 3).startswith("## ローカル LLM")
    assert digest.japanese_ratio(ja) > 0.4 > digest.japanese_ratio(en)



def test_digest_shows_headlines_while_the_llm_is_working(monkeypatch):
    """目的: まとめを作っている間も、先に見出しの一覧（作成中の印つき）が見られ、LLM ができたら置き換わること。

    根拠: 2026-11-23 利用者「参照元の記事の名前や概要をそのまま記載し、LLM で変換が終わったものから順次変えていくような形にすれば、
    見れないみたいな不具合は発生しない」。作成中のまま止まった行は、次の予定で作り直す（missing が数えない）。
    """
    seed_articles()
    seen = {}

    async def slow(model, messages, **kw):
        with news_db.session() as db:
            row = db.scalar(select(NewsDigest).where(NewsDigest.period == "daily", NewsDigest.day == "2026-11-23"))
            seen["pending"] = (row.error, row.fallback, row.body)
        return GOOD_DIGEST

    monkeypatch.setattr(llm, "complete", slow)
    row = run(digest.generate("daily", "2026-11-23"))
    assert seen["pending"][0] == digest.PENDING and seen["pending"][1] is True and "Qwen の新モデルが出た" in seen["pending"][2]
    assert row["fallback"] is False and row["error"] == ""


def test_settings_defaults_and_validation(admin_client):
    """目的: 設定の既定（まとめの時刻・OpenRouter の無料モデル・1 日 800 回・1 分 15 回）と、形の検査・上限の範囲。
    根拠: docs/research-free-llm-batch.md（既定の上限は 1 日 800 回・1 分 15 回）。不正な値は黙って受けず 422 にする（AGENTS.md 8）。"""
    body = admin_client.get("/api/v1/news/settings").json()
    cfg = body["settings"]
    assert cfg["digest_daily"] == {"enabled": True, "time": "06:30"} and cfg["digest_weekly"] == {"enabled": True, "weekday": 0, "time": "07:00"}
    assert cfg["llm"]["gist_enabled"] is False and cfg["llm"]["prep_model"] == llm.DEFAULT_MODEL
    assert cfg["llm"]["daily_limit"] == 800 and cfg["llm"]["minute_limit"] == 15 and body["prep_batch"] == 20
    assert "rag" not in cfg and "imagegen" not in cfg
    assert body["weekdays"][0] == "月" and body["interval_minutes"] == 30 and body["llm_stats"]["key_set"] is False
    for bad in ({"digest_daily": {"time": "6:30"}}, {"digest_weekly": {"weekday": 7}}, {"feed_refresh_minutes": 0},
                {"llm": {"digest_model": "bad model!"}}, {"quiet": {"start": "25:00"}}, {"llm": {"daily_limit": 0}}, {"llm": {"minute_limit": 101}}):
        assert admin_client.put("/api/v1/news/settings", json=bad).status_code == 422, bad
    ok = admin_client.put("/api/v1/news/settings", json={"feed_refresh_minutes": 5, "digest_daily": {"enabled": False, "time": "05:00"},
                                                          "llm": {"daily_limit": 120, "minute_limit": 5}})
    assert ok.status_code == 200 and ok.json()["settings"]["feed_refresh_minutes"] == 5 and ok.json()["settings"]["llm"]["daily_limit"] == 120
