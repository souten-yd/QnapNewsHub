"""目的: 検索の精度・密度を LLM なしで上げる 2 つの手（feat/retrieval-boost）の試験。外へ通信しない・LLM は呼ばない。
  ①3 文字未満の日本語の語（「認証」「量子」）: FTS5 の trigram は 3 文字未満の語に一致しない。索引の語の表（fts5vocab）で「その語で始まる 3 文字の語」へ広げて引く。
    短い語だけの問いは広げた語で探し、ほかの語がある問いでは順位づけの加点にだけ使う（問いの言い回しの語で無関係な文書が候補に入らない）。表は既存の DB でも開くときに 1 つ作るだけ（索引の作り直しは要らない）。
  ②命題（主張）の当たりを文書・物の種へ伝える: 主張の本文でだけ当たった記事・物を種に足し（match.via=claim）、既に種のものは重複させず加点だけ。区分（scopes）の外は出さない。
根拠: 評価の物差し（3 文字未満の語の問い 50 問で R@10 0.658→0.817・通常の問いは変わらない。命題の伝達で通常 60 問の ② R@10 0.682→0.702・nDCG 0.641→0.676）。
Dense X Retrieval（命題単位で Recall が上がる）・HippoRAG 2 の考え。測定の記録は評価の置き場（リポジトリには入れない）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from qnapnewshub import db as news_db
from qnapnewshub import rcache, searchindex
from qnapnewshub.db import Claim
from tests.test_kg import H, art
from tests.test_retrieval import post
from tests.test_retrieval_density import _claim


@pytest.fixture(autouse=True)
def _reset_cache():
    rcache.reset()
    yield
    rcache.reset()


def test_two_char_japanese_term_is_found_via_the_vocab_expansion(client):
    """目的: 「認証」だけの問い・「Passkey の 認証」の問いで、題に 2 文字の語を持つ記事が出る。ほかの語があるときは加点だけで、候補を増やさない。"""
    a1 = art(1, [("Passkey", "tech")], title="Passkey 対応で認証が簡単になった")
    a2 = art(2, [("Wallet", "tech")], title="Wallet の新機能と更新の一覧")
    a3 = art(3, [("Passkey", "tech")], title="Passkey の普及状況について")
    only = post(client, "/retrieve/seeds", {"q": "認証 について何が報じられたか", "kinds": ["document"]}).json()["items"]
    assert [i["article_id"] for i in only][:1] == [a1] and a2 not in [i["article_id"] for i in only]
    both = post(client, "/retrieve/seeds", {"q": "Passkey の 認証", "kinds": ["document"]}).json()["items"]
    ids = [i["article_id"] for i in both]
    assert ids[0] == a1 and a3 in ids and a2 not in ids      # 「認証」を含む a1 が、含まない a3 より上


def test_vocab_expansion_skips_ubiquitous_terms_and_works_on_an_old_db(client):
    """目的: どこにでも出る語（全記事の SHORT_DF_RATIO 超）は広げない。語の表が無い古い DB では何も起きず（例外にしない）、開くとき ensure が作る。"""
    for n in range(30):
        art(n, [("X", "tech")], title=f"Item {n} 発表 {n}")
    art(40, [("Y", "tech")], title="レアな 暗号 の話")
    with news_db.session() as db:
        conn = db.connection()
        total = conn.execute(text("SELECT count(*) FROM news_items")).scalar()
        assert searchindex.expand_short(conn, ["発表"], searchindex.TRI_V, total) == []      # ほぼ全記事に出る
        assert searchindex.expand_short(conn, ["暗号"], searchindex.TRI_V, total)
        conn.execute(text(f"DROP TABLE {searchindex.TRI_V}"))
        assert searchindex.expand_short(conn, ["暗号"], searchindex.TRI_V, total) == []      # 表が無い（古い DB）
        searchindex.ensure_vocab(conn)
        assert searchindex.expand_short(conn, ["暗号"], searchindex.TRI_V, total)      # 再び作られる（冪等）
        db.rollback()


def test_claim_hits_add_their_document_and_entity_as_seeds_without_duplicates(client):
    """目的: 主張の本文でだけ当たった文書・物が種に足され（via=claim）、全文で既に種のものは増えずに加点される。機密の主張の文書・物は出ない。"""
    c1, d1 = _claim(client, "Zorbulator は静かな冷却ファンを採用した", "Zorbulator", "arxiv:2601.10001")
    c2, d2 = _claim(client, "Zorbulator は静かな冷却ファンを採用した秘密の話", "Hidden", "arxiv:2601.10002", sens="confidential")
    out = post(client, "/retrieve/seeds", {"q": "静かな冷却ファンを採用したのは"}).json()["items"]
    docs = [i for i in out if i["kind"] == "document"]
    ents = [i for i in out if i["kind"] == "entity"]
    assert [d["id"] for d in docs] == [d1["id"]] and docs[0]["match"]["via"] == "claim" and docs[0]["sensitivity"] == "public"
    assert [e["name"] for e in ents] == ["Zorbulator"] and ents[0]["match"]["via"] == "claim"
    assert d2["id"] not in [d["id"] for d in docs] and "Hidden" not in [e["name"] for e in ents]      # 区分の外
    assert len({(i["kind"], i["id"]) for i in out}) == len(out)      # 重複しない
    # 種類を絞ったときは、その種類だけ
    only = post(client, "/retrieve/seeds", {"q": "静かな冷却ファンを採用したのは", "kinds": ["claim"]}).json()["items"]
    assert {i["kind"] for i in only} == {"claim"}
    # 物の名前でも当たる問いでは、物は重複せず点数だけが上がる
    named = post(client, "/retrieve/seeds", {"q": "Zorbulator は何を採用したか"}).json()["items"]
    assert [e["id"] for e in named if e["kind"] == "entity"].count(ents[0]["id"]) == 1
    assert c1["id"] and c2["id"]
    with news_db.session() as db:
        assert db.query(Claim).count() == 2
