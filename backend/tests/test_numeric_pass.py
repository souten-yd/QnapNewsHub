"""目的: 数値パス（numeric.py。記事の数値と測定の条件を取り出す第 2 パス）の試験。外へ通信しない・LLM は模擬。
  ①候補の文の選び方（規則）: 数字＋単位を含む文を前後 1 文・見出し・表の行と一緒に取る・上限・数字の無い記事は候補なし・本文の途中も拾う
  ②検査: 引用が渡した文に無い・値が渡した文に無い・条件の値が渡した文に無いものを捨てる。null の欄は通る・単位が文に無ければ単位だけ捨てる
  ③合流: 規則が先・LLM の条件は検査を通ったときだけ優先・同じ数字の既存の主張には cond の足りない欄だけ足す（新しい主張を作らない）
  ④実行: 候補のある記事だけ数件まとめて 1 回・構造化出力（response_format）を付ける・断られたら付けずにやり直す・済みの印・1 日の上限・設定が切・取り出し前の記事は対象にしない
  ⑤夜の埋め戻し: 夜だけ・古い記事だけ・用途 backfill  ⑥numeric-try の集計（何も書かない）。
根拠: 2026-11-26 の調査（下ごしらえの 1 回の呼び出しに cond の指示を足しても、本物の LLM は 30 件で 1 度も書かなかった。20 記事を一度に・任意の入れ子の欄・渡した文に条件が無い・facts が題と頭の数字に偏る）。
利用者「必要な情報を確実に取り出す」作り。ARCHITECTURE 12.1（条件の違う数値を平均しない）。例の数字・名前は架空（公開リポジトリに実データを入れない）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from qnapnewshub import bench, llm, numeric, numeric_try, settings, writer
from qnapnewshub import db as news_db
from qnapnewshub.db import Claim, NewsItem
from tests.test_kg import art
from tests.test_news_llm_prep import chat, keyed, run  # noqa: F401  (keyed は fixture)

BODY = ("Benchmarks\n"
        "We ran the Foo-7B model on a workstation. The setup used ExampleCpp b1234 with the Q4_K_M quant. "
        "Foo-7B reached 120.5 tok/s on an RTX 9999 at 32k context. The result was stable across runs.\n"
        "Pricing\n"
        "The Bar Pro costs $1,299 at launch. Shipping starts next month.\n"
        "Unrelated paragraph about the weather. It was sunny. Nobody cared about numbers in 2026.")


def test_candidate_windows_pick_unit_sentences_with_neighbours_and_heading():
    """目的: 数字＋単位を含む文を、前後 1 文と直前の見出しと一緒に取る（条件は隣の文にあることが多い）。数字だけ（年）の文・天気の文は取らない。"""
    wins = numeric.candidate_windows(BODY)
    joined = " ".join(w["text"] for w in wins)
    assert "120.5 tok/s" in joined and "$1,299" in joined and "Q4_K_M" in joined      # 条件の隣の文が一緒に付く
    assert "[見出し] Benchmarks" in joined and "[見出し] Pricing" in joined
    assert "weather" not in joined and "sunny" not in joined
    got = numeric.candidates_for("Foo-7B benchmark", BODY)
    assert got["source"].startswith("Foo-7B benchmark ") and got["known"].get("quant") == "Q4_K_M" and got["known"].get("hw") == "RTX 9999"


def test_candidate_windows_none_for_plain_text_and_caps():
    """目的: 数字＋単位の文が無い記事は候補なし（呼び出さない）。候補の塊は記事あたり MAX_WINDOWS・MAX_CHARS まで（トークンの上限）。表の行も拾う。"""
    assert numeric.candidate_windows("It was a nice day.\nNobody said anything in 2026.") == [] and numeric.candidates_for("t", "") is None
    big = "\n".join(f"Section {i}\nModel {i} runs at {i + 10}.5 tok/s on a GPU. The next sentence is filler number {i}. More filler." for i in range(30))
    wins = numeric.candidate_windows(big)
    assert len(wins) == numeric.MAX_WINDOWS and sum(len(w["text"]) for w in wins) <= numeric.MAX_CHARS + 20
    rows = numeric.candidate_windows("Table\n| Model | Speed |\n| Foo | 88.1 tok/s |\n| Bar | 70.2 tok/s |\nEnd of the table text here.")
    assert rows and "88.1 tok/s" in rows[0]["text"]


def _art(**kw) -> dict:
    return {"source": "T Foo-7B reached 120.5 tok/s on an RTX 9999 using ExampleCpp at Q4_K_M. The Bar Pro costs $1,299.", **kw}


def _row(**over) -> dict:
    base = {"n": 1, "quote": "Foo-7B reached 120.5 tok/s on an RTX 9999", "about": "Foo-7B", "metric": "生成速度", "value": "120.5", "unit": "tok/s",
            "model": "Foo-7B", "quant": "Q4_K_M", "hw": "RTX 9999", "engine": None, "ctx": None, "version": None}
    return {**base, **over}


def test_check_item_drops_what_is_not_in_the_given_text():
    """目的: 引用・値・条件の値が、LLM に渡した文に無いものは捨てる（幻覚の防止）。null の欄は通る。単位が文に無ければ数値は残して単位だけ捨てる。"""
    ok, why = numeric.check_item(_row(), _art())
    assert why == "" and ok["llm_cond"] == {"model": "Foo-7B", "quant": "Q4_K_M", "hw": "RTX 9999"} and ok["tier"] == "exact"
    assert numeric.check_item(_row(quote="Foo-7B reached 999 tok/s"), _art())[1] == "quote"
    assert numeric.check_item(_row(value="999"), _art())[1] == "value"
    assert numeric.check_item(_row(about=""), _art())[1] == "form" and numeric.check_item("x", _art())[1] == "form"
    item, _ = numeric.check_item(_row(hw="RTX 1111", engine="vLLM", unit="furlongs"), _art())
    assert item["llm_cond"] == {"model": "Foo-7B", "quant": "Q4_K_M"} and item["unit"] == "" and sorted(item["dropped_cond"]) == ["engine", "hw"]
    price, _ = numeric.check_item({**_row(), "quote": "The Bar Pro costs $1,299", "about": "Bar Pro", "metric": "価格", "value": "1299", "unit": "USD", "model": None, "quant": None, "hw": None}, _art())
    assert price["unit"] == "USD" and price["llm_cond"] == {}      # USD は渡した文に無いが既知の単位


def test_merge_prefers_checked_llm_cond_and_parse_dedupes():
    """目的: 条件の合流は規則が先・LLM（検査済み）が規則より優先。同じ数値の重複は 1 つ・番号が範囲外の行は捨てる・読めない返事は ValueError。"""
    assert numeric.merge_cond({"hw": "RTX 9999", "quant": "Q8"}, {"quant": "Q4_K_M"}) == {"hw": "RTX 9999", "quant": "Q4_K_M"}
    text = json.dumps({"numbers": [_row(), _row(), _row(n=7), {"n": 1, "quote": "x"}]})
    got, stats = numeric.parse(text, [_art()])
    assert len(got[1]) == 1 and stats == {"returned": 4, "passed": 1, "dropped": {"n": 1, "form": 1}}
    assert got[1][0]["rule_cond"] == {"hw": "RTX 9999"}
    with pytest.raises(ValueError):
        numeric.parse("not json at all", [_art()])


def _seed(title: str, summary: str, *, extracted: bool = True, hours: float = 1) -> int:
    aid = art(hours, [("Foo-7B", "model")], title=title)
    with news_db.session() as db:
        row = db.get(NewsItem, aid)
        row.summary = summary
        row.extract_v = 1 if extracted else 0
        db.commit()
    return aid


def _numbers_reply(messages, n):
    count = len([ln for ln in messages[1]["content"].splitlines() if ln[:1].isdigit() and ". 題:" in ln[:12]])
    rows = [_row(n=i) for i in range(1, count + 1)]
    return chat(json.dumps({"numbers": rows}))


class Server:
    def __init__(self, fn=_numbers_reply):
        self.calls: list[dict] = []
        self.fn = fn

    async def __call__(self, body, key):
        self.calls.append(body)
        return self.fn(body["messages"], len(self.calls), body) if self.fn.__code__.co_argcount == 3 else self.fn(body["messages"], len(self.calls))


SUMMARY = "Foo-7B reached 120.5 tok/s on an RTX 9999 using ExampleCpp at Q4_K_M. Details follow."


def test_run_sends_only_candidate_articles_in_one_call_and_stores_cond(monkeypatch, keyed):
    """目的: 候補の文のある記事だけを 1 回にまとめて送り（数字の無い記事は送らず印だけ）、構造化出力を付け、検査を通った数値を cond つきの claims に入れる。
    既に同じ数字の主張がある記事には新しい主張を作らず cond を足す。済みの印が付き、2 度目は呼ばない。"""
    a = _seed("Foo-7B speed test", SUMMARY)
    b = _seed("Plain article", "Nothing numeric here at all, just words.")
    with news_db.session() as db:      # a には、下ごしらえが取った同じ数字の主張（条件なし）が既にある
        ent = db.scalar(select(Claim.entity_id).where(Claim.article_id == a)) or None
        db.add(Claim(article_id=a, entity_id=ent, kind="benchmark", text="速度 120.5 tok/s", value="120.5tok/s", ai_extracted=True, cond=""))
        db.commit()
    server = Server()
    monkeypatch.setattr(llm, "_post", server)
    out = run(numeric.run())
    assert out["calls"] == 1 and out["done"] == 1 and len(server.calls) == 1
    body = server.calls[0]
    assert body["response_format"]["json_schema"]["strict"] is True and "Plain article" not in body["messages"][1]["content"]
    assert llm.calls_today("prep") == 1 and llm.extra_today("numeric") == 1
    writer.flush("t")
    with news_db.session() as db:
        claims = list(db.scalars(select(Claim).where(Claim.article_id == a, Claim.value != "")))
        assert len(claims) == 1      # 同じ数字の主張に足しただけ
        assert bench.cond_of(claims[0].cond, "") == {"model": "Foo-7B", "quant": "Q4_K_M", "hw": "RTX 9999"}
        assert db.get(NewsItem, a).num_v == numeric.NUM_DONE_EXCERPT and db.get(NewsItem, b).num_v == numeric.NUM_DONE_EXCERPT      # 候補の無い記事にも印
    assert run(numeric.run())["calls"] == 0 and len(server.calls) == 1


def test_run_adds_new_claim_with_value_and_cond(monkeypatch, keyed):
    """目的: 既存の主張に無い数字は、追加の取り出し（additive）として新しい主張にする。値と条件が入り、前の取り出しは消えない。"""
    a = _seed("Foo-7B speed test", SUMMARY)
    monkeypatch.setattr(llm, "_post", Server())
    assert run(numeric.run())["numbers"] == 1
    writer.flush("t")
    with news_db.session() as db:
        c = db.scalar(select(Claim).where(Claim.article_id == a, Claim.value != ""))
        assert c.kind == "benchmark" and c.value == "120.5 tok/s" and bench.cond_of(c.cond, "")["hw"] == "RTX 9999" and c.prompt_version == numeric.PROMPT_VERSION
        assert "Foo-7B reached 120.5 tok/s" in c.text


def test_run_falls_back_to_plain_json_when_schema_is_refused(monkeypatch, keyed):
    """目的: 構造化出力（response_format）を断られたモデル（HTTP 400）には、付けずにやり直し、以後は最初から付けない。"""
    _seed("Foo-7B speed test", SUMMARY)
    _seed("Foo-7B second test", SUMMARY.replace("120.5", "98.7"), hours=2)

    def reply(messages, n, body):
        return (400, json.dumps({"error": {"message": "response_format is not supported"}})) if "response_format" in body else _numbers_reply(messages, n)

    server = Server(reply)
    monkeypatch.setattr(llm, "_post", server)
    out = run(numeric.run())
    assert out["calls"] == 1 and out["error"] == "" and ["response_format" in c for c in server.calls] == [True, False]
    assert run(numeric.run())["calls"] == 0      # 済み
    _seed("Foo-7B third test", SUMMARY.replace("120.5", "77.7"), hours=3)
    run(numeric.run())
    assert "response_format" not in server.calls[-1]


def test_run_skips_unextracted_articles_disabled_and_daily_cap(monkeypatch, keyed):
    """目的: 取り出し（extract_v）が済んでいない記事は対象にしない（取り出し直しが主張を消すので）。設定が切・1 日の上限では呼ばない。"""
    _seed("Foo-7B speed test", SUMMARY, extracted=False)
    server = Server()
    monkeypatch.setattr(llm, "_post", server)
    assert run(numeric.run())["calls"] == 0 and server.calls == []
    _seed("Foo-7B another", SUMMARY.replace("120.5", "55.5"), hours=2)
    cfg = settings.load()
    cfg["llm"]["numeric_enabled"] = False
    settings.save(cfg)
    assert run(numeric.run())["calls"] == 0
    cfg["llm"]["numeric_enabled"] = True
    settings.save(cfg)
    for _ in range(numeric.NUMERIC_DAILY_MAX):
        llm.count_extra("numeric")
    assert run(numeric.run())["calls"] == 0 and server.calls == []


def test_full_body_is_used_when_available_and_second_look_after_body(monkeypatch, keyed):
    """目的: 本文が取れた記事は本文の全体から候補を選ぶ（抜粋に数字が無くても拾う）。抜粋で済んだ記事（num_v=1）は、本文が取れたらもう 1 回だけ見る（num_v=2）。"""
    from qnapnewshub import ztext
    from qnapnewshub.db import NewsBody

    a = _seed("Foo-7B notes", "Short excerpt without figures.")
    server = Server()
    monkeypatch.setattr(llm, "_post", server)
    assert run(numeric.run())["calls"] == 0
    writer.flush("t")
    with news_db.session() as db:
        assert db.get(NewsItem, a).num_v == numeric.NUM_DONE_EXCERPT
        db.add(NewsBody(item_id=a, state=1, body=ztext.pack(BODY), raw_len=len(BODY), reason="", at=datetime.now(timezone.utc)))
        db.get(NewsItem, a).body_state = 1
        db.commit()
    assert run(numeric.run())["calls"] == 1
    assert "[見出し] Benchmarks" in server.calls[0]["messages"][1]["content"]
    writer.flush("t")
    with news_db.session() as db:
        assert db.get(NewsItem, a).num_v == numeric.NUM_DONE_BODY


def test_backfill_only_at_night_for_old_articles_with_backfill_purpose(monkeypatch, keyed):
    """目的: 古い記事の埋め戻しは夜（night_from 以降）だけ・RECENT_DAYS より古い記事だけ・用途 backfill（使い残した枠）・1 日 NUMERIC_BACKFILL_DAILY_MAX 回まで。昼は呼ばない。"""
    old = _seed("Foo-7B old test", SUMMARY, hours=24 * 10)
    server = Server()
    monkeypatch.setattr(llm, "_post", server)
    import qnapnewshub.numeric as nm

    class Clock(datetime):
        hour_value = 10

        @classmethod
        def now(cls, tz=None):
            real = datetime.now(tz)
            return real.replace(hour=cls.hour_value)

    monkeypatch.setattr(nm, "datetime", Clock)
    monkeypatch.setattr(llm, "datetime", Clock)
    assert run(numeric.run_backfill())["calls"] == 0      # 昼
    Clock.hour_value = 23
    assert run(numeric.run())["calls"] == 0      # 新しい記事の道は古い記事を拾わない
    out = run(numeric.run_backfill())
    assert out["calls"] == 1 and out["done"] == 1 and llm.calls_today("backfill") == 1 and llm.extra_today("numeric_bf") == 1
    writer.flush("t")
    with news_db.session() as db:
        assert db.get(NewsItem, old).num_v == numeric.NUM_DONE_EXCERPT


def test_numeric_try_counts_fields_and_writes_nothing(monkeypatch):
    """目的: numeric-try は候補のある記事だけを送り、返った数・通った数・条件の欄ごとの埋まり方（規則だけ／LLM で増えた）を数える。DB の印・台帳には何も書かない。鍵は出力に出ない。"""
    a = _seed("Foo-7B speed test", SUMMARY)
    server = Server()

    async def post(body, key):
        return await server(body, key)

    monkeypatch.setattr(llm, "_post", post)
    arts, info = numeric_try.load_articles(10)
    assert info["with_candidates"] == 1 and arts[0]["id"] == a
    secret = "sk-test-secret-value"
    result = run(numeric_try.try_once("openrouter", "", "m/x:free", secret, arts))
    assert result["calls"] == 1 and result["returned"] == 1 and result["passed"] == 1 and result["mode"] == "json_schema"
    f = result["fields"]
    assert f["hw"]["both_same"] == 1 and f["quant"]["llm_added"] == 1 and f["engine"]["none"] == 1
    text = numeric_try.report(info, result)
    assert "候補の文のある記事 1 件" in text and secret not in text
    writer.flush("t")
    with news_db.session() as db:
        assert db.get(NewsItem, a).num_v == 0 and llm.calls_today("prep") == 0
