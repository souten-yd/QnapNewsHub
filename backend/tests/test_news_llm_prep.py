"""目的: 外部の LLM（OpenRouter）による記事の下ごしらえ（30 件ずつ・記事ごとの検査・送り直し・3 回で諦め）と、回数の上限（1 日・1 分）・混み合いのやり直しの試験。
外へ通信しない（HTTP の 1 回 = llm._post を差し替える。待ちは llm._sleep を差し替えて実際には待たない）。

根拠: docs/research-free-llm-batch.md（2026-11-24 実測）: 30 件は 30 秒以内で形が崩れず、概要が日本語で返らない記事は回ごとにばらつく → 記事ごとに検査し、
合わない記事だけ同じ回で 1 回送り直す。既定の上限は（v0.2 から）1 日 800 回・1 分 15 回。混み合い（429）は 20・60・120 秒で最大 3 回（ControlDeck と同じ。2026-11-23 利用者
「混み合いの制限などで一時的に断られた場合は、少し時間を置いてから再度実行する」）。鍵はログ・応答・例外の文に出さない（AGENTS.md 4）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from qnapnewshub import writer
from qnapnewshub import llm, prep, settings, store
from qnapnewshub import db as news_db
from qnapnewshub.db import NewsItem
from tests.test_news import add


def fake_key() -> str:
    return "sk-or-" + "k" * 24          # 見本の鍵は実行時に組み立てる（AGENTS.md 1）


def run(coro):
    return asyncio.run(coro)


def chat(content: str) -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": content}}]})


def answer(messages, *, skip=(), english=()):
    """プロンプトの記事の番号ごとに JSON を作る（skip は欠け、english は概要が英語のまま）。"""
    count = len([ln for ln in messages[1]["content"].splitlines() if ln[:1].isdigit() and ". " in ln[:5]])
    body = {}
    for n in range(1, count + 1):
        if n in skip:
            continue
        ja = "english only text" if n in english else f"新しい GPU ドライバーとモデルの記事です {n}"
        body[str(n)] = {"ja": ja, "img": f"a graphics card on a desk number {n}", "topics": ["GPU", "ドライバー"]}
    return json.dumps(body, ensure_ascii=False)


def rows_of(count: int, tag: str = "e"):
    ids = [add(f"https://{tag}.test/{i}", title=f"Title number {i} of the day", summary=f"This is an English excerpt number {i} about new GPU drivers and models.",
               published=datetime.now(timezone.utc)) for i in range(count)]
    with news_db.session() as db:
        return ids, [store.item_dict(r) for r in db.scalars(select(NewsItem).where(NewsItem.id.in_(ids)).order_by(NewsItem.id))]


@pytest.fixture
def keyed(monkeypatch):
    """鍵が入っていて、待ちは実際には待たず記録するだけの準備。"""
    llm.set_key(fake_key())
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(llm, "_sleep", sleep)
    return slept


class Server:
    """llm._post の代わり。呼ばれた要求の本文を記録し、answer_fn(messages, n) の結果を返す。"""

    def __init__(self, answer_fn=None):
        self.calls: list[dict] = []
        self.answer_fn = answer_fn or (lambda messages, n: chat(answer(messages)))

    async def __call__(self, body, key):
        assert key == fake_key()
        self.calls.append(body)
        return self.answer_fn(body["messages"], len(self.calls))


def test_prep_batches_15_per_call_saves_and_presents(monkeypatch, keyed):
    """目的: 下ごしらえは 15 件で 1 回の呼び出し（2026-11-24 に要素の取り出しを足して 30 → 15）。日本語の概要・画像の描写・話題の語を保存し、一覧は訳を出して元の抜粋も返す。呼び出し回数を数える。切のときは呼ばない。
    根拠: 2026-11-24 利用者「ある程度のまとまりを送って構造的に個別に返してもらう」。件数は docs/research-free-llm-batch.md（20 → 30 に変更）。"""
    ids, rows = rows_of(31)
    server = Server()
    monkeypatch.setattr(llm, "_post", server)
    out = run(prep.run(rows))
    assert prep.BATCH == 15 and out["done"] == 31 and out["calls"] == 3 and len(server.calls) == 3
    first = server.calls[0]["messages"][1]["content"]
    assert "15. Title number 14" in first and "16. " not in first             # 1 回目は 15 件ちょうど
    assert server.calls[0]["model"] == llm.DEFAULT_MODEL and server.calls[0]["reasoning"] == {"enabled": False}
    assert prep.calls_today() == 3 and llm.stats()["total"] == 3
    writer.flush("test")      # まとめ書きの前の分は、保存してから DB で確かめる
    with news_db.session() as db:
        row = db.get(NewsItem, ids[0])
        assert row.summary_ja.startswith("新しい GPU") and row.title == "Title number 0 of the day" and row.img_prompt.startswith("a graphics card")
        assert row.prep_tries == prep.PREP_DONE and "GPU" in row.topics
        shown = store.present(row, {})
    assert shown["translated"] is True and shown["summary"] == row.summary_ja and "English excerpt number 0" in shown["summary_original"]
    cfg = settings.load(); cfg["llm"]["prep_enabled"] = False; settings.save(cfg)      # 切のときは呼ばない
    _, rows2 = rows_of(1, "e2")
    assert run(prep.run(rows2))["done"] == 0 and len(server.calls) == 3


def test_prep_parse_rejects_broken_and_misnumbered_replies():
    """目的: JSON が壊れていれば ValueError、番号が範囲外・整数でない項目は捨て、コードの囲み・配列の形も読み、英語だけの ja は使わない。
    根拠: ControlDeck の prep.parse の試験を移した（返事の形の検査は記事ごとの検査の前提）。"""
    with pytest.raises(ValueError):
        prep.parse("すみません、できません", 2)
    with pytest.raises(ValueError):
        prep.parse("{broken", 2)
    ok = prep.parse('```json\n{"1": {"ja": "日本語の概要", "img": "a red rocket", "topics": ["A","B","C","D"]}, "7": {"ja": "範囲外"}, "x": {"ja": "番号が違う"}}\n```', 2)
    assert list(ok) == [1] and ok[1]["topics"] == ["A", "B", "C"]
    assert list(prep.parse('[{"n": 2, "ja": "二番目の概要", "img": "wind turbines"}]', 2)) == [2]
    assert prep.parse('{"1": {"ja": "english only", "img": ""}}', 1) == {}


def test_prep_checks_each_article_and_resends_only_the_failures_once_stricter(monkeypatch, keyed):
    """目的: 返事の記事ごとの検査（JSON・番号・概要が日本語か）。合わない記事（欠け・英語のまま）だけを同じ回の中で 1 回、「必ず日本語で」を強めた指示で送り直す。
    それでもだめな記事は試行を +1 して次の取得へ回し、3 回で諦める（元の題と抜粋のまま）。
    根拠: docs/research-free-llm-batch.md「概要を日本語で書かずに英語のまま返す記事が混ざる。その割合は回ごとにばらつく」。"""
    ids, rows = rows_of(4)

    def fn(messages, n):
        if n == 1:
            return chat(answer(messages, skip=(2,), english=(3,)))      # 2 は欠け、3 は英語のまま
        return chat(answer(messages, skip=(1,)))                        # 送り直しの 1 番目（= 欠けていた記事）はまた返さない

    server = Server(fn)
    monkeypatch.setattr(llm, "_post", server)
    out = run(prep.run(rows))
    sent = [c["messages"][1]["content"] for c in server.calls]
    assert len(sent) == 2 and out["calls"] == 2 and out["done"] == 3
    assert prep.STRICT.strip() not in sent[0] and prep.STRICT.strip() in sent[1]                # 送り直しだけ強めた指示
    assert "Title number 1 of the day" in sent[1] and "Title number 2 of the day" in sent[1] and "Title number 0 of the day" not in sent[1]
    writer.flush("test")      # まとめ書きの前の分は、保存してから DB で確かめる
    with news_db.session() as db:
        got = {r.id: r for r in db.scalars(select(NewsItem))}
    assert got[ids[1]].prep_tries == 1 and got[ids[1]].summary_ja == "" and got[ids[2]].summary_ja.startswith("新しい GPU")
    assert got[ids[0]].prep_tries == prep.PREP_DONE
    # 次の取得: 新着が無くても、前に失敗した記事を拾って送る。2 回目の失敗 → 3 回目の失敗で諦める
    server.answer_fn = lambda messages, n: chat("not json")
    run(prep.run([]))
    run(prep.run([]))
    writer.flush("test")
    with news_db.session() as db:
        assert db.get(NewsItem, ids[1]).prep_tries == prep.MAX_TRIES
    before = len(server.calls)
    assert run(prep.run([])) == {"done": 0, "calls": 0, "error": ""} and len(server.calls) == before      # 3 回で諦めたら送らない


def test_prep_respects_per_run_limit(monkeypatch, keyed):
    """目的: 1 回の取得の上限（既定 100 件 = 30 件ずつ 4 回）を超えて送らないこと。
    根拠: 1 日の回数を抑えるため（docs/research-free-llm-batch.md）。"""
    _, rows = rows_of(130)
    server = Server()
    monkeypatch.setattr(llm, "_post", server)
    out = run(prep.run(rows))
    assert out["done"] == 100 and len(server.calls) == 7      # 15 件ずつ（2026-11-24 に 30 → 15）


def test_prep_leaves_tries_untouched_when_the_llm_is_unavailable(monkeypatch, keyed):
    """目的: 鍵が無い・通信できない・上限などで送れないときは、記事の試行回数を増やさない（記事のせいではないので、3 回で諦める数に入れない）。
    根拠: 一時的な障害で記事が「下ごしらえなし」のまま確定してしまうのを防ぐ。"""
    ids, rows = rows_of(2)

    async def down(body, key):
        raise llm.LlmUnavailable("OpenRouter に接続できません: ConnectError")

    monkeypatch.setattr(llm, "_post", down)
    out = run(prep.run(rows))
    assert out["done"] == 0 and "接続できません" in out["error"]
    with news_db.session() as db:
        assert all(r.prep_tries == 0 for r in db.scalars(select(NewsItem).where(NewsItem.id.in_(ids))))
    assert llm.stats()["failed"] == 1 and "接続できません" in llm.stats()["last_error"]


def test_prep_without_key_does_nothing_and_does_not_call(monkeypatch):
    """目的: 鍵が未設定なら何もせず理由を返す（HTTP を呼ばない）。
    根拠: 設計 2.4。ニュースハブは鍵が無くても動く（下ごしらえが止まるのが正しい）。"""
    _, rows = rows_of(2)
    posts = []

    async def post(body, key):
        posts.append(1)
        return chat("{}")

    monkeypatch.setattr(llm, "_post", post)
    out = run(prep.run(rows))
    assert out["done"] == 0 and "鍵が未設定" in out["error"] and posts == []


def test_daily_limit_stops_and_resumes_next_day(monkeypatch, keyed):
    """（1 回 30 件の前提で数を書いた試験。件数ではなく上限の働きを見るので BATCH を 30 に固定する）
    目的: 1 日の上限（設定 daily_limit）に達したら、それ以上送らず次の日まで止まる。日が変われば数え直す。記事の試行回数は増やさない。
    根拠: 無料のモデルの 1 日の上限に当たる前に自分で止める（docs/research-free-llm-batch.md「既定の上限は 1 日 300 回」）。"""
    monkeypatch.setattr(prep, "BATCH", 30)
    cfg = settings.load(); cfg["llm"]["daily_limit"] = 2; cfg["llm"]["quota"] = None; settings.save(cfg)
    ids, rows = rows_of(100)
    server = Server()
    monkeypatch.setattr(llm, "_post", server)
    out = run(prep.run(rows))
    assert len(server.calls) == 2 and out["calls"] == 2 and out["done"] == 60 and "1 日の上限（2 回）" in out["error"]
    assert llm.stats(2)["total"] == 2
    with news_db.session() as db:
        assert db.scalar(select(NewsItem.prep_tries).where(NewsItem.id == ids[99])) == 0      # 送れなかった分は触らない
    assert run(prep.run([]))["error"].startswith("1 日の上限") and len(server.calls) == 2    # 同じ日は何度呼んでも送らない
    monkeypatch.setattr(llm, "_today", lambda: "2099-01-01")                                  # 日が変わった
    assert llm.calls_today() == 0
    out = run(prep.run([]))
    assert len(server.calls) == 4 and out["calls"] == 2           # 取り残した 40 件のうち、新しい日の枠（2 回）が使える


def test_default_limits_are_800_a_day_and_15_a_minute():
    """目的: 既定の上限が 1 日 800 回・1 分 15 回（v0.2 で 300・10 から変えた）。根拠: ARCHITECTURE 5.1・利用者 2026-11-24「OpenRouter の無料 LLM を積極的に使う」。"""
    assert settings.load()["llm"]["daily_limit"] == 800 and settings.load()["llm"]["minute_limit"] == 15
    assert llm.DEFAULT_DAILY_LIMIT == 800 and llm.DEFAULT_MINUTE_LIMIT == 15


def test_minute_limit_waits_until_the_window_has_room(monkeypatch, keyed):
    """目的: 1 分の上限（設定 minute_limit）に達したら、空くまで待ってから送る（捨てない・失敗にしない）。
    根拠: 無料のモデルの 1 分の上限（20 回）に当たる前に自分で間を空ける。既定は 10 回。"""
    cfg = settings.load(); cfg["llm"]["minute_limit"] = 3; settings.save(cfg)
    now = [1000.0]
    slept = keyed
    monkeypatch.setattr(llm, "_clock", lambda: now[0])

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(llm, "_sleep", sleep)
    server = Server(lambda messages, n: chat("ok"))
    monkeypatch.setattr(llm, "_post", server)
    msgs = [{"role": "user", "content": "1. t"}]
    for _ in range(3):
        run(llm.complete("m", msgs, max_tokens=10))
    assert slept == []                                      # 3 回目までは待たない
    run(llm.complete("m", msgs, max_tokens=10))              # 4 回目: 1 分の窓が空くまで待つ
    assert len(slept) == 1 and 59 <= slept[0] <= 61 and len(server.calls) == 4


def test_busy_replies_wait_20_60_120_seconds_up_to_three_times(monkeypatch, keyed):
    """目的: 429（混み合い）は 20・60・120 秒待って最大 3 回やり直す。やり直しも 1 回と数える。3 回待っても通らなければ LlmUnavailable。
    根拠: 2026-11-23 利用者「混み合いの制限などで一時的に断られた場合は、少し時間を置いてから再度実行する」。無料のモデルがすぐ 429 を返した。"""
    server = Server(lambda messages, n: (429, json.dumps({"error": {"message": "model is temporarily rate-limited upstream"}})) if n < 4 else chat("まとめ"))
    monkeypatch.setattr(llm, "_post", server)
    assert run(llm.complete("m", [{"role": "user", "content": "x"}])) == "まとめ"
    assert keyed == [20.0, 60.0, 120.0] and len(server.calls) == 4 and llm.calls_today() == 4
    keyed.clear()
    always = Server(lambda messages, n: (429, "{}"))
    monkeypatch.setattr(llm, "_post", always)
    with pytest.raises(llm.LlmUnavailable, match="429"):
        run(llm.complete("m", [{"role": "user", "content": "x"}]))
    assert keyed == [20.0, 60.0, 120.0] and len(always.calls) == 4


def test_non_busy_failures_are_not_retried(monkeypatch, keyed):
    """目的: 待っても変わらない失敗（鍵が無効 401・モデルが無い 404・答えの形が違う）はやり直さない。200 で 429 の error を返す混み合いはやり直す。"""
    for status in (401, 404):
        server = Server(lambda messages, n, s=status: (s, json.dumps({"error": {"message": "nope"}})))
        monkeypatch.setattr(llm, "_post", server)
        with pytest.raises(llm.LlmUnavailable, match=str(status)):
            run(llm.complete("m", [{"role": "user", "content": "x"}]))
        assert len(server.calls) == 1
    assert keyed == []
    server = Server(lambda messages, n: (200, json.dumps({"error": {"code": 429, "message": "Provider returned error"}})) if n == 1 else chat("できた"))
    monkeypatch.setattr(llm, "_post", server)
    assert run(llm.complete("m", [{"role": "user", "content": "x"}])) == "できた" and keyed == [20.0]
    odd = Server(lambda messages, n: (200, json.dumps({"choices": []})))
    monkeypatch.setattr(llm, "_post", odd)
    with pytest.raises(llm.LlmUnavailable, match="形が違います"):
        run(llm.complete("m", [{"role": "user", "content": "x"}]))


def test_the_key_never_appears_in_logs_errors_or_stats(monkeypatch, keyed, caplog):
    """目的: 鍵の本文は、ログ・例外の文・設定の画面の値（stats・settings）に出ない。応答の本文に鍵らしい文字列が混ざっても伏せる。
    根拠: AGENTS.md 絶対ルール 4（秘密をログ・画面に出さない）。OpenRouter は 401 の本文に鍵の一部を含めて返すことがある。"""
    leak = "sk-or-" + "z" * 20
    server = Server(lambda messages, n: (401, json.dumps({"error": {"message": f"Invalid key {leak}"}})))
    monkeypatch.setattr(llm, "_post", server)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(llm.LlmUnavailable) as err:
            run(llm.complete("m", [{"role": "user", "content": "x"}]))
    everything = str(err.value) + caplog.text + json.dumps(llm.stats()) + json.dumps(settings.load())
    assert fake_key() not in everything and leak not in everything and "[鍵]" in str(err.value)


def test_digest_and_gist_count_against_the_same_daily_budget(monkeypatch, keyed):
    """目的: まとめ・一言要約も 1 日の上限に数える（用途別に見えるが、上限は合計）。
    根拠: OpenRouter の上限はアカウント全体。下ごしらえだけ数えると、まとめの回で上限を超える。"""
    cfg = settings.load(); cfg["llm"]["daily_limit"] = 3; cfg["llm"]["quota"] = None; settings.save(cfg)
    server = Server(lambda messages, n: chat("ok"))
    monkeypatch.setattr(llm, "_post", server)
    for purpose in ("prep", "digest", "gist"):
        run(llm.complete("m", [{"role": "user", "content": "x"}], purpose=purpose))
    assert {k: v for k, v in llm.stats(3)["calls"].items() if v} == {"prep": 1, "digest": 1, "gist": 1}
    with pytest.raises(llm.LlmDailyLimit):
        run(llm.complete("m", [{"role": "user", "content": "x"}], purpose="digest"))
    assert len(server.calls) == 3
