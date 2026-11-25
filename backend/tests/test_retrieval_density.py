"""検索の改善（文脈の密度・主張の種・周りの点数）の試験。docs/DESIGN 17 章の物差し（./hub.sh eval-retrieval）で、基準値の弱い所だった 3 点を直した。

1. 文脈の密度: LLM に渡す文脈（text 形式の things/context・compact の /retrieve/evidence）に Google ニュースの長い転送 URL を出さず、「出どころ 日付 d<文書の id>」にする。
   根拠: 基準値で文脈のトークンの約 6 割が URL（物の文脈の密度 4.34 → 5.74 正解/1k トークン）。JSON の sources・src は URL のまま残す（機械が読む側は変えない）。
2. 主張を直接引く: 主張の本文・値・主語の物の名前と別名の全文索引（claim_fts_*）。POST /retrieve/seeds が kind=claim を返し、根拠の文書へ辿れる。根拠: 基準値の主張 R@20 最高 0.20。
3. 周りの点数: /graph/neighbors に seed_scores を渡すと辺・物に score が付き、必ず元の種より低い。確かさ・根拠の数・問いの語（物の名前・関係の型を尋ねる語）で重みづけ。
   根拠: 基準値で複数の文書の問いは種 0.77 → 種＋周り 0.66 と、周りを足すほど下がった。
"""
from __future__ import annotations

import json

from sqlalchemy import select, text

from qnapnewshub import retrieval, searchindex
from qnapnewshub import db as news_db
from qnapnewshub.db import Claim, EntityAlias
from tests.test_kg import H, art, ent_id
from tests.test_retrieval import ents, post, rel, _reset_cache  # noqa: F401  (_reset_cache は autouse の fixture)

GNEWS = "https://news.google.com/rss/articles/CBMi" + "x" * 160 + "?oc=5"


def test_context_text_has_no_long_url_but_json_keeps_it(client):
    """目的: text 形式の context は出どころを「名前 日付 d<id>」で出し、URL を出さない。JSON は URL（sources・src）と同じ並びの label を持つ。
    根拠: 基準値で文脈のトークンの約 6 割が Google ニュースの URL。"""
    art(2, [("Densware", "tech")], facts=[{"about": "Densware", "kind": "release", "text": "Densware 2 を出した", "value": "2"}], title="Densware 2 ships", url=GNEWS, source="Google News X")
    eid = ent_id("Densware")
    js = client.get(f"/api/v1/things/{eid}/context").json()
    assert js["sources"] == [GNEWS] and len(js["source_label"]) == 1 and js["source_label"][0].startswith("Google News X 20") and " d" in js["source_label"][0]
    tx = client.get(f"/api/v1/things/{eid}/context?format=text").text
    assert "news.google.com" not in tx and "http" not in tx and f"source: {js['source_label'][0]}" in tx
    assert len(GNEWS) > 150 and len(tx) < len(json.dumps(js, ensure_ascii=False))      # 転送 URL は 150 文字超。text はそれを含まない分だけ JSON より短い


def test_evidence_compact_drops_url_and_has_label(client):
    """目的: /retrieve/evidence は文書に label（出どころ 日付 d<id>）を持ち、compact=true なら url を省く（既定は url あり＝既存の利用側を変えない）。"""
    pub = client.post("/api/v1/kg/documents", json={"kind": "paper", "ident": "arxiv:2601.55555", "title": "T", "url": GNEWS, "source": "Arxiv", "published_at": "2026-01-02T00:00:00Z", "sensitivity": "public", "ai_use": "allowed"}, headers=H).json()
    c = client.post("/api/v1/kg/claims", json={"document_id": pub["id"], "entity": "Compactor", "kind": "claim", "text": "Compactor is small", "evidences": [{"quote": "is small"}]}, headers=H).json()
    full = post(client, "/retrieve/evidence", {"claims": [c["id"]]}).json()["claims"][str(c["id"])]["evidences"][0]["document"]
    small = post(client, "/retrieve/evidence", {"claims": [c["id"]], "compact": True}).json()["claims"][str(c["id"])]["evidences"][0]["document"]
    assert full["url"] == GNEWS and full["label"] == small["label"] == f"Arxiv 2026-01-02 d{pub['id']}" and "url" not in small


def _claim(client, text_, entity, doc_ident, sens="public"):
    d = client.post("/api/v1/kg/documents", json={"kind": "paper", "ident": doc_ident, "title": f"Doc {doc_ident}", "sensitivity": sens, "ai_use": "allowed" if sens == "public" else "local_only"}, headers=H).json()
    r = client.post("/api/v1/kg/claims", json={"document_id": d["id"], "entity": entity, "kind": "claim", "text": text_, "evidences": [{"quote": text_[:8]}]}, headers=H)
    assert r.status_code == 200, r.text
    return r.json(), d


def test_claim_seeds_by_text_and_subject_and_reach_documents_within_scope(client):
    """目的: /retrieve/seeds が主張も種として返す。主張の本文でも、主語の物の名前・別名でも当たり、各主張に根拠の文書（id・label）が付く。
    区分の範囲外の主張は出さない・kinds=["claim"] で主張だけ・索引はトリガーで追う（別名の追加・主張の更新）。根拠: 基準値の主張 R@20 最高 0.20（主張を直接引けなかった）。"""
    c1, d1 = _claim(client, "価格は 70 ドルと発表された", "Birdcam", "arxiv:2601.00001")
    c2, _ = _claim(client, "Latency dropped by half", "Fastnet", "arxiv:2601.00002")
    cs, _ = _claim(client, "価格は 99 ドルの秘密の計画", "Secretcam", "arxiv:2601.00003", sens="confidential")
    got = post(client, "/retrieve/seeds", {"q": "価格は 70 ドルという報道は何か", "kinds": ["claim"]}).json()
    assert got["counts"]["claim"] >= 1 and {i["kind"] for i in got["items"]} == {"claim"}
    top = got["items"][0]
    assert top["id"] == c1["id"] and top["entity"]["name"] == "Birdcam" and top["documents"][0]["id"] == d1["id"] and top["documents"][0]["label"].endswith(f"d{d1['id']}") and top["score"] == 1.0
    assert cs["id"] not in [i["id"] for i in got["items"]]      # 機密の主張は区分の外
    by_subject = post(client, "/retrieve/seeds", {"q": "Fastnet について", "kinds": ["claim"]}).json()["items"]
    assert [i["id"] for i in by_subject] == [c2["id"]]
    with news_db.session() as db:      # 別名・本文の更新が索引に届く（トリガー）
        db.add(EntityAlias(entity_id=ent_id("Fastnet"), alias="Quicknet"))
        db.commit()
        db.execute(Claim.__table__.update().where(Claim.id == c1["id"]).values(text="値段は 80 ドルに改定された"))
        db.commit()
    assert [i["id"] for i in post(client, "/retrieve/seeds", {"q": "Quicknet", "kinds": ["claim"]}).json()["items"]] == [c2["id"]]
    assert c1["id"] not in [i["id"] for i in post(client, "/retrieve/seeds", {"q": "価格は 70 ドル", "kinds": ["claim"]}).json()["items"]]      # 古い本文では当たらない
    assert c1["id"] in [i["id"] for i in post(client, "/retrieve/seeds", {"q": "値段は 80 ドルに改定", "kinds": ["claim"]}).json()["items"]]
    allk = post(client, "/retrieve/seeds", {"q": "Fastnet"}).json()      # 既定（kinds 空）は主張も返す
    assert allk["counts"]["claim"] == 1


def test_claim_index_is_filled_once_for_existing_rows(client):
    """目的: 主張の索引は、開くとき 1 回だけ既存の行を入れる（claim_fts_v1 の印）。作り直しの予約でも入れ直される。根拠: 既存の DB（この版より前の主張）を使い続けられること。"""
    c, _ = _claim(client, "Indexed claim about Zeta", "Zetaware", "arxiv:2601.00009")
    with news_db.engine().begin() as conn:
        conn.execute(text(f"DELETE FROM {searchindex.CLAIM_TRI}"))
        conn.execute(text(f"DELETE FROM {searchindex.CLAIM_WORD}"))
        conn.execute(text("DELETE FROM news_meta WHERE key = 'claim_fts_v1'"))
        assert searchindex.search_claims(conn, [], ["Zeta"]) == []
        assert searchindex.ensure_claims(conn)
        assert [i for i, _ in searchindex.search_claims(conn, [], ["Zeta"])] == [c["id"]]
        searchindex.request_rebuild(conn)
        assert [i for i, _ in searchindex.search_claims(conn, [], ["Zeta"])] == [c["id"]]


def test_neighbor_scores_are_below_the_seed_and_weighted(client):
    """目的: seed_scores を渡すと辺・物に score が付く。どの辺も元の種より低い（種を押しのけない）。確かさ・根拠の数が高いほど高く、問いの語（物の名前・関係の型を尋ねる語）に合う相手が合わない相手より高い。
    seed_scores が無ければ score を付けない（既存の利用側の応答は変わらない）。根拠: 基準値で複数の文書の問いが、周りを足すほど下がった（種 0.77 → 種＋周り 0.66）。"""
    with news_db.session() as db:
        a, b, c, d = ents(db, "Hubzed", "Namedpeer", "Strongpeer", "Weakpeer")
        for other, conf, n in ((b, 0.5, 1), (c, 0.9, 3), (d, 0.9, 0)):
            rid = rel(db, a, other, "uses", conf)
            r = db.get(retrieval.Relationship, rid)
            r.evidence_count = n
        db.commit()
    plain = post(client, "/graph/neighbors", {"ids": [a]}).json()
    assert all("score" not in e for e in plain["levels"][0]["edges"])
    body = {"ids": [a], "seed_scores": [0.8], "q": "Namedpeer について"}
    out = post(client, "/graph/neighbors", body).json()
    score = {e["target"]: e["score"] for e in out["levels"][0]["edges"]}
    assert all(0 < s < 0.8 for s in score.values()) and out["seeds"][0]["score"] == 0.8
    assert {n["id"]: n["score"] for n in out["levels"][0]["nodes"]} == score      # 物の score は、来る辺の最大
    assert score[b] > score[d] > 0 and score[b] > score[c]      # 名前が問いに出る相手は、確かさ・根拠が上の相手より上（問いの係数 1.0 対 NEIGHBOR_QUERY_MISS）
    cue = post(client, "/graph/neighbors", {**body, "q": "Hubzed を使っているものは何か"}).json()      # 関係の型を尋ねる語（使っ）は、相手の名前が無くても効く
    cs = {e["target"]: e["score"] for e in cue["levels"][0]["edges"]}
    assert cs[c] > score[c] and cs[c] > cs[d] > 0 and all(s < 0.8 for s in cs.values())
    assert cs[c] > cs[d]      # 同じ問いの係数なら、根拠の数が多い方が高い（確かさは同じ 0.9）
    first = {e["target"]: e["score"] for e in post(client, "/graph/neighbors", {**body, "seed_scores": [0.4]}).json()["levels"][0]["edges"]}
    assert all(first[k] < score[k] for k in score)      # 元の点数に比例して下がる
    assert 0 < retrieval.NEIGHBOR_DECAY < 1 and retrieval.NEIGHBOR_QUERY_MISS < 1
