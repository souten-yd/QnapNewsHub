"""目的: 軽い翻訳（QuickMT・translate.py / translate_flow.py）の試験。エンジンは模擬（本物のモデルは使わない・外へ通信しない）。
  用語の保護（印の置き換えと戻し・URL を書き換えない・対訳表・Things の名前）・訳の検査（落ちた文は原文のまま〔未訳〕）・文ごとのキャッシュ・日本語は訳さない・
  LLM が使えるとき／使えないときの流れ（題と本文だけ）・保存と全文検索の索引（日本語の問いで英語の記事を引く）・お休みモード・translate_hq の仕事・モデルの取得（sha256・ホスト）・設定の検査。
根拠: 2026-11-25 の実測（docs/ARCHITECTURE.md 8.5）。QuickMT は URL の /en/ を /ja/ に書き換え、quantization を「定量化」と誤訳した。目的は LLM が引く検索の精度と密度
（日本語の問いで英語の記事を引く）。AGENTS.md: 永続化は Write Coordinator・LLM の答えと同様に機械の訳も検査する・外への取得はホストと大きさを縛る。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from qnapnewshub import db as news_db
from qnapnewshub import jobs, llm, settings, translate, translate_flow, writer, ztext
from qnapnewshub.db import NewsBody, NewsItem, NewsTr
from tests.test_news import add

NOW = datetime.now(timezone.utc)


class Mock(translate.Engine):
    """英語の小文字の語を消して「これは翻訳です。」を付ける模擬。印（ZQ1Z）・大文字の語・数字は残る。/en/ は /ja/ に書き換える（URL を保護できているかの確認）。"""

    name, version = "mock", "mock@1"

    def __init__(self, fn=None):
        self.calls: list[list[str]] = []
        self.fn = fn or (lambda s: "これは翻訳です。" + re.sub(r"\b[a-z][a-z'-]*\b", "", s).replace("/en/", "/ja/"))

    def translate(self, sentences):
        self.calls.append(list(sentences))
        return [self.fn(s) for s in sentences]


@pytest.fixture
def eng():
    e = Mock()
    translate.use_engine(e)
    translate._STATS.update(units=0, cache_hits=0, checked=0, failed=0, chars=0, seconds=0.0)  # noqa: SLF001
    translate._STATS["reasons"].clear()  # noqa: SLF001
    yield e
    translate.use_engine(None)


def ctx(entities=(), **cfg):
    c = settings.load()["translate"]
    c.update(cfg)
    return translate.Ctx(list(entities), c)


def one(text, engine, c=None, cache=None):
    return translate.translate_units([text], c or ctx(), engine, {} if cache is None else cache)[0]


def put_body(item_id: int, text: str):
    writer.get().stage_body(item_id, {"state": 1, "body": ztext.pack(text), "raw_len": len(text.encode()), "reason": "", "at": NOW})
    writer.flush("test")


def get_tr(item_id):
    with news_db.session() as db:
        row = db.get(NewsTr, item_id)
        return None if row is None else {c.key: getattr(row, c.key) for c in NewsTr.__table__.columns}


def run_work(**cfg):
    c = settings.load()["translate"]
    c.update(cfg)
    out = translate_flow.work(c, budget=60)
    writer.flush("test")
    return out


# ---- 用語の保護 ----

def test_urls_code_versions_and_units_are_masked_and_restored(eng):
    """目的: URL・コード・版・単位つきの数値は印に置き換えて訳し、同じ文字列に戻す。模擬は /en/ を /ja/ に書き換えるが、URL は印の中なので変わらない。
    根拠: 実測で QuickMT が URL の /en/latest/ を /ja/latest/ に書き換えた（事故）。"""
    src = "Run `pip install vllm==0.11.2` and open https://docs.example.org/en/latest/ on llama.cpp b12388 at 74 tok/s with 32GB VRAM."
    masked, slots = translate.protect(src, ctx())
    assert "https://docs.example.org" not in masked and "0.11.2" not in masked and masked.count("ZQ") == len(slots) >= 5
    u = one(src, eng)
    assert u.status == "ok"
    for keep in ("`pip install vllm==0.11.2`", "https://docs.example.org/en/latest/", "llama.cpp", "b12388", "74 tok/s", "32GB"):
        assert keep in u.out
    assert "/ja/" not in u.out


def test_glossary_and_entities_are_applied(eng):
    """目的: 対訳表（既定 quantization→量子化 ほか・設定で足せる）の語は日本語へ置き換わり、Things の名前（別名）と「そのまま残す語」は原文のまま残る。
    根拠: 実測で quantization が「定量化」に誤訳された。"""
    u = one("Quantization and inference on Qwen Zeta need a Foo tool.", eng, ctx(["Qwen Zeta"], keep=["Foo"], glossary={"tool": "道具"}))
    assert "量子化" in u.out and "推論" in u.out and "Qwen Zeta" in u.out and "道具" in u.out and "定量化" not in u.out
    assert "Foo" in u.out


def test_marker_sentence_with_only_url_is_not_translated(eng):
    """目的: URL・コードだけの文は訳さず（エンジンを呼ばず）原文のまま。根拠: 訳す語が無い文を訳すと壊す。"""
    u = one("https://example.org/en/a/b", eng)
    assert u.status == "keep" and u.out == "https://example.org/en/a/b" and eng.calls == []


# ---- 訳の検査 ----

@pytest.mark.parametrize("name,fn,reason", [
    ("印が欠けた", lambda s: "これは翻訳です。", "marks"),
    ("印が増えた", lambda s: s + " ZQ9Z", "marks"),
    ("数値が違う", lambda s: "これは翻訳です。" + re.sub(r"\b[a-z][a-z'-]*\b", "", s).replace("42", "43"), "numbers"),
    ("英語のまま", lambda s: s, "english"),
    ("繰り返し", lambda s: " ".join(re.findall(r"ZQ\d+Z", s)) + "あいう" * 30, "repeat"),
    ("空", lambda s: "  ", "empty"),
])
def test_failed_checks_keep_the_original(name, fn, reason):
    """目的: 検査（印がすべて 1 回ずつ戻る・数値の一致・英語のまま残らない・繰り返し・空）に落ちた文は原文のまま残り、理由が数えられる。根拠: 機械の訳を信用しない（実測の誤訳）。"""
    e = Mock(fn)
    translate.use_engine(e)
    try:
        u = one("The server reached 42 requests at https://x.test/a in the late evening.", e)
        assert u.status == f"fail:{reason}", name
        assert u.out == "The server reached 42 requests at https://x.test/a in the late evening."
        assert translate.stats()["reasons"].get(reason, 0) >= 1
    finally:
        translate.use_engine(None)


def test_body_failed_sentences_stay_in_english_with_a_mark(eng):
    """目的: 本文の中で落ちた文は原文のまま「〔未訳〕」を付けて残り、落ちた数を返す。ほかの文は訳される。"""
    eng.fn = lambda s: s if "poison" in s else "これは翻訳です。" + re.sub(r"\b[a-z][a-z'-]*\b", "", s)
    text = "The first sentence is quite normal here. The poison sentence stays unchanged here.\nSecond paragraph begins with a sentence today."
    ja, n, fail = translate.translate_text(text, ctx(), eng, {})
    assert n == 3 and fail == 1 and translate.UNTRANSLATED + "The poison sentence" in ja and ja.count("\n") == 1 and ja.startswith("これは翻訳です。")


# ---- キャッシュ ----

def test_same_sentence_is_translated_once(eng):
    """目的: 同じ文（URL だけ違う文を含む）は覚えから出し、エンジンを呼ばない。エンジンの版が変われば別の鍵で訳し直す。根拠: 配信記事・同じ出来事の重複で同じ文を訳さない。"""
    cache: dict = {}
    s1 = "Meta said the new chip ships next month, see https://a.test/x for details."
    s2 = "Meta said the new chip ships next month, see https://b.test/y for details."
    one(s1, eng, cache=cache)
    n = sum(len(c) for c in eng.calls)
    got = translate.translate_units([s1, s2], ctx(), eng, {})      # 同じ印に置き換わる = 同じ鍵（cache は書き手のバッファ経由。先に積む）
    assert n == 1 and sum(len(c) for c in eng.calls) == 2      # 同じ call の中の同じ文は 1 つにまとめる
    writer.get().stage_translation(1, {"kind": "mt"}, cache)
    eng.calls.clear()
    got = translate.translate_units([s1, s2], ctx(), eng, {})
    assert eng.calls == [] and [u.status for u in got] == ["cached", "cached"] and "https://b.test/y" in got[1].out
    eng.version = "mock@2"
    translate.translate_units([s1], ctx(), eng, {})
    assert len(eng.calls) == 1


# ---- 流れ ----

def en_item(n, *, title=None, summary="Meta released a new open model today with long context and strong results in several tests.", prep_tries=0, summary_ja=""):
    i = add(f"https://en{n}.test/a", title=title or f"Meta releases open model number {n}", summary=summary, published=NOW - timedelta(minutes=n))
    with news_db.session() as db:
        r = db.get(NewsItem, i)
        r.prep_tries, r.summary_ja = prep_tries, summary_ja
        r.fetched_at = NOW.replace(tzinfo=None) - timedelta(hours=30)
        db.commit()
    return i


def set_llm_up(monkeypatch, up: bool):
    monkeypatch.setattr(translate_flow, "llm_state", lambda db, cfg=None: "ok" if up else "none")


def test_japanese_articles_are_not_translated(eng, monkeypatch):
    """目的: 日本語の記事は訳さない（印だけ残し、二度と調べない）。根拠: 言語の判定（利用者の指示「日本語の記事は訳さない」）。"""
    set_llm_up(monkeypatch, False)
    ja = add("https://ja.test/a", title="新しいモデルが公開されました", summary="日本語の概要です。", published=NOW)
    en = en_item(1)
    run_work()
    assert get_tr(ja)["kind"] == "ja" and get_tr(ja)["title_ja"] == "" and get_tr(en)["kind"] == "mt" and get_tr(en)["title_ja"]
    assert all("新しいモデル" not in s for call in eng.calls for s in call)
    before = len(eng.calls)
    run_work()
    assert len(eng.calls) == before      # 済んだ記事は再び訳さない


def test_llm_available_waits_for_japanese_summary_then_uses_fallback(eng, monkeypatch):
    """目的: LLM が使えるとき、題は訳し、概要は LLM の日本語の概要を待つ（機械翻訳しない）。LLM が 3 回失敗して諦めたら RSS の概要を訳す。LLM の概要が付いた記事は概要を訳さない。"""
    set_llm_up(monkeypatch, True)
    waiting = en_item(1)
    given_up = en_item(2, prep_tries=3)
    prepped = en_item(3, prep_tries=100, summary_ja="LLM の日本語の概要です")
    run_work()
    assert get_tr(waiting)["title_ja"] and get_tr(waiting)["summary_state"] == "wait" and get_tr(waiting)["summary_ja"] == ""
    assert get_tr(given_up)["summary_ja"] and get_tr(given_up)["summary_src"] == "rss"
    assert get_tr(prepped)["summary_ja"] == "" and get_tr(prepped)["title_ja"]
    with news_db.session() as db:
        r = db.get(NewsItem, waiting)
        r.prep_tries = 3
        db.commit()
    run_work()
    assert get_tr(waiting)["summary_ja"] and get_tr(waiting)["summary_state"] == "done"


def test_llm_down_translates_title_and_body_only(eng, monkeypatch):
    """目的: LLM が長く使えないとき（無い・上限・止まっている）は題と本文だけを訳す。RSS の概要は訳さず、概要は本文の訳の先頭の文から作る（本文が無い記事は概要なし）。
    根拠: 利用者の指示「LLM が長く使えないときは題と本文だけを訳す」。"""
    set_llm_up(monkeypatch, False)
    with_body, no_body = en_item(1), en_item(2)
    body = "\n".join(f"Paragraph {k} says that the new model runs fast on small machines and keeps accuracy high." for k in range(6))
    put_body(with_body, body)
    monkeypatch.setattr(translate_flow, "importance", lambda db, rows: {r.id: (1.0, "チャンネル「テスト」") for r in rows})
    run_work()      # 1 回目は題（まとめ書きで保存してから）、2 回目に本文
    run_work()
    a, b = get_tr(with_body), get_tr(no_body)
    assert a["title_ja"] and a["body_ja"] and a["summary_ja"] and a["summary_src"] == "body" and a["n_units"] >= 7
    assert b["title_ja"] and b["summary_ja"] == "" and b["body_ja"] == ""


def test_bodies_are_translated_only_for_important_items_and_within_the_daily_limit(eng, monkeypatch):
    """目的: 本文は重要な記事（チャンネル・監視のThings・ホットトピック）だけを空いている時間に、1 日の上限まで訳す。重要でない記事は記事を開いたときに訳す（API）。
    根拠: 利用者の指示（重要な記事を先に・1 日の上限・残りは開いたとき）。実測で本文は 1,000 件で約 4 時間かかる見込み。"""
    set_llm_up(monkeypatch, False)
    ids = [en_item(n) for n in range(1, 5)]
    for i in ids:
        put_body(i, "A long enough paragraph about the new model running on small machines with care.")
    monkeypatch.setattr(translate_flow, "importance", lambda db, rows: {r.id: ((5.0 if r.id in ids[:3] else 0.0), "チャンネル" if r.id in ids[:3] else "") for r in rows})
    run_work(body_per_day=2)      # 題を先に保存する
    run_work(body_per_day=2)
    done = [i for i in ids if get_tr(i)["body_ja"]]
    assert len(done) == 2 and ids[3] not in done
    run_work(body_per_day=2)
    assert len([i for i in ids if get_tr(i)["body_ja"]]) == 2      # 1 日の上限（再び動かしても増えない）
    out = translate_flow.ondemand(ids[3])      # 開いたとき（上限の外）
    writer.flush("test")
    assert out["row"]["body_ja"] and get_tr(ids[3])["body_ja"]


def test_busy_rest_off_and_no_model_do_not_translate(eng, monkeypatch):
    """目的: 設定が切・お休みモード・取得の最中・モデルが無いときは翻訳しない（お休み中は書き込みを起こさない）。"""
    set_llm_up(monkeypatch, False)
    en_item(1)
    cfg = settings.load()
    settings.save({**cfg, "translate": {**cfg["translate"], "engine": "off"}})
    assert asyncio.run(translate_flow.run_once())["reason"] == "off"
    settings.save({**cfg, "translate": {**cfg["translate"], "engine": "quickmt"}, "rest": {"enabled": True, "start": "00:00", "end": "23:59", "mode": "keep_memory"}})
    from qnapnewshub import rest
    rest.invalidate()
    assert asyncio.run(translate_flow.run_once())["reason"] == "rest"
    settings.save({**cfg, "rest": {**cfg["rest"], "enabled": False}})
    rest.invalidate()
    monkeypatch.setattr(translate_flow, "idle", lambda: False)
    assert asyncio.run(translate_flow.run_once())["reason"] == "busy"
    translate.use_engine(None)
    assert asyncio.run(translate_flow.run_once())["reason"] == "no_model"
    assert translate.get_engine() is None


# ---- 保存・索引・API ----

def test_translation_is_saved_separately_and_searchable_in_japanese(admin_client, eng, monkeypatch):
    """目的: 訳は原文と別の表にエンジン・版・時刻つきで保存され（原文の列は変わらない）、全文検索の索引に入って、日本語の問いで英語の記事を引ける。一覧には mt（機械翻訳の印・題・概要）が付く。
    根拠: 本機能の目的（日本語の問いで英語の記事を引く精度を上げる）。"""
    set_llm_up(monkeypatch, False)
    eng.fn = lambda s: "量子化に関する新しい手法のラマです。" + "".join(re.findall(r"ZQ\d+Z", s))
    i = en_item(1, title="Llama quantization update", summary="A short English excerpt describing a new compression approach for large language models.")
    assert admin_client.get("/api/v1/news/items", params={"q": "量子化の手法"}).json()["items"] == []      # 訳す前は引けない
    run_work()
    row = get_tr(i)
    assert row["engine"] == "mock@1" and row["at"] and row["title_ja"].startswith("量子化に関する")
    with news_db.session() as db:
        assert db.get(NewsItem, i).title == "Llama quantization update" and db.get(NewsItem, i).summary_ja == ""
    got = admin_client.get("/api/v1/news/items", params={"q": "量子化に関する新しい手法"}).json()["items"]
    assert [r["id"] for r in got] == [i] and got[0]["mt"]["title"] == row["title_ja"] and got[0]["mt"]["engine"] == "mock@1" and got[0]["title"] == "Llama quantization update"
    detail = admin_client.get(f"/api/v1/translate/articles/{i}").json()
    assert detail["translated"] and detail["title_ja"] == row["title_ja"]


def test_requeue_with_better_engine_replaces_translation(eng, monkeypatch):
    """目的: 訳は後で良いエンジンの版で上書きできる（reset で件数も数え直す）。高品質（hq）の訳は軽いエンジンで上書きされない。"""
    i = en_item(1)
    writer.get().stage_translation(i, {"kind": "mt", "engine": "old@1", "title_ja": "古い訳", "add_units": 1, "add_fail": 1, "at": NOW})
    writer.flush("test")
    writer.get().stage_translation(i, {"kind": "mt", "engine": "new@2", "title_ja": "新しい訳", "add_units": 1, "add_fail": 0, "reset": True, "at": NOW})
    writer.flush("test")
    r = get_tr(i)
    assert r["engine"] == "new@2" and r["title_ja"] == "新しい訳" and (r["n_units"], r["n_fail"]) == (1, 0)
    writer.get().stage_translation(i, {"kind": "hq", "engine": "hq:big", "title_ja": "高品質の訳", "reset": True, "at": NOW})
    writer.get().stage_translation(i, {"kind": "mt", "engine": "new@3", "title_ja": "軽い訳", "at": NOW})
    writer.flush("test")
    writer.get().stage_translation(i, {"kind": "mt", "engine": "new@4", "title_ja": "軽い訳 2", "at": NOW})
    writer.flush("test")
    r = get_tr(i)
    assert r["kind"] == "hq" and r["title_ja"] == "高品質の訳"


def test_translate_hq_job_is_created_for_important_items_and_result_is_stored(client, eng, monkeypatch):
    """目的: 重要な英語の記事（本文あり）にだけ translate_hq の仕事を作り、ControlDeck の結果（日本語を含む題・概要・本文）を検査して hq として保存する。日本語でない・形が違う結果は断る。"""
    a, b = en_item(1), en_item(2)
    put_body(a, "A long enough paragraph about the new model running on small machines with care.")
    put_body(b, "Another long enough paragraph about a different thing in the world today.")
    monkeypatch.setattr(translate_flow, "importance", lambda db, rows: {r.id: ((3.0, "チャンネル") if r.id == a else (0.0, "")) for r in rows})
    got = jobs.claim(1, ["translate_hq"], 10, 300)["jobs"]
    assert [j["ref_id"] for j in got] == [a] and got[0]["input"]["item"]["body"].startswith("A long enough")
    bad = jobs.complete(1, [{"job_id": got[0]["id"], "result": {"title_ja": "english only"}, "model": "m"}])["results"][0]
    assert bad["status"] == "rejected"
    got = jobs.claim(1, ["translate_hq"], 10, 300)["jobs"]
    ok = jobs.complete(1, [{"job_id": got[0]["id"], "result": {"title_ja": "高品質の題", "summary_ja": "要約です。", "body_ja": "本文の訳です。"}, "model": "plamo"}])["results"][0]
    assert ok["status"] == "done"
    r = get_tr(a)
    assert r["kind"] == "hq" and r["engine"] == "hq:plamo" and r["body_ja"] == "本文の訳です。"
    cfg = settings.load()
    settings.save({**cfg, "translate": {**cfg["translate"], "hq_jobs": False}})
    assert jobs.create_translate_hq(news_db.session().__enter__()) == 0


def test_ondemand_api_and_status(admin_client, eng, monkeypatch):
    """目的: 記事を開いたとき POST /translate/articles/{id} で本文を訳せる（日本語の記事は断る・モデルが無ければ 409）。状態の API にモデル・件数・検査の率が出る。"""
    i = en_item(1)
    put_body(i, "A long enough paragraph about the new model running on small machines with care.")
    r = admin_client.post(f"/api/v1/translate/articles/{i}", headers={"X-Requested-With": "NewsHub"})
    assert r.status_code == 200 and r.json()["body_ja"]
    ja = add("https://ja2.test/a", title="日本語の題です", published=NOW)
    assert admin_client.post(f"/api/v1/translate/articles/{ja}", headers={"X-Requested-With": "NewsHub"}).status_code == 409
    writer.flush("test")
    st = admin_client.get("/api/v1/translate/status").json()
    assert st["counts"]["bodies"] == 1 and st["stats"]["units"] >= 1 and "fail_pct" in st["stats"] and st["model"]["license"] == "CC-BY-4.0"
    diag = admin_client.get("/api/v1/diagnostics").json()["translate"]
    assert diag["counts"]["bodies"] == 1 and "cache_hit_pct" in diag["run"]
    translate.use_engine(None)
    assert admin_client.post(f"/api/v1/translate/articles/{i + 0}", headers={"X-Requested-With": "NewsHub"}).status_code in (200, 409)


def test_settings_validation_and_glossary(admin_client):
    """目的: 設定の翻訳の節の検査（エンジン・スレッド・CPU の命令・対訳表の形・上限）。足りない項目は既定で補う。"""
    base = admin_client.get("/api/v1/news/settings").json()["settings"]
    assert base["translate"]["engine"] == "quickmt" and base["translate"]["threads"] == 2 and base["translate"]["batch"] == 32 and base["translate"]["beam"] == 2
    def put(**c):
        return admin_client.put("/api/v1/news/settings", json={"translate": c})
    assert put(engine="nope").status_code == 422 and put(threads=0).status_code == 422 and put(cpu_isa="AVX9").status_code == 422
    assert put(glossary={"日本語": "x"}).status_code == 422 and put(max_en_ratio=2).status_code == 422
    r = put(glossary={"Prefill": "プリフィル"}, keep=["Foo"], cpu_isa="generic", engine="off")
    assert r.status_code == 200 and r.json()["settings"]["translate"]["glossary"] == {"prefill": "プリフィル"} and r.json()["settings"]["translate"]["cpu_isa"] == "GENERIC"


# ---- モデルの取得 ----

def test_model_download_verifies_sha256_and_host(tmp_path, monkeypatch):
    """目的: モデルの取得は https の huggingface.co（と hf.co の配信）だけ・ファイルごとの大きさと sha256 を確かめる。違えば入れない（.verified も作らない）。根拠: 外の取得の決まり（AGENTS.md 7）・版と hash を固定。"""
    data = b"model-bytes" * 10
    files = {"model.bin": (hashlib.sha256(data).hexdigest(), len(data))}
    monkeypatch.setitem(translate.MODEL, "files", files)
    monkeypatch.setenv("NEWSHUB_DATA", str(tmp_path))
    served = {"body": data}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "huggingface.co":
            return httpx.Response(302, headers={"location": "https://cas-bridge.xethub.hf.co/x/model.bin"})
        assert request.url.host == "cas-bridge.xethub.hf.co"
        return httpx.Response(200, content=served["body"])

    monkeypatch.setattr(translate, "_new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    served["body"] = data[:-1] + b"X"
    with pytest.raises(translate.ModelError):
        asyncio.run(translate.download_model())
    assert not translate.installed() and not (translate.model_dir() / "model.bin").exists()
    served["body"] = data
    asyncio.run(translate.download_model())
    assert translate.installed() and translate.download_state()["phase"] == "done"
    for bad in ("http://huggingface.co/a", "https://evil.test/a", "https://huggingface.co.evil.test/a", "https://user@huggingface.co/a", "https://huggingface.co:8443/a"):
        with pytest.raises(translate.ModelError):
            translate._check_url(bad)  # noqa: SLF001
    translate._check_url("https://cdn-lfs.huggingface.co/a")  # noqa: SLF001
    assert translate.remove_model() and not translate.installed()
