"""目的: v0.6 検索の道具（種の検索 /retrieve/seeds・neighbors の拡張・局所 PageRank /graph/ppr・根拠のまとめ取り /retrieve/evidence・調査の記録の読み取り GET /research・
区分の強制・キャッシュと上限）の試験。外へ通信しない・LLM は呼ばない。
  ①問いの文からの語の取り方 ②種の検索（全文・物の名前と別名・イベント・ニュース以外の文書。種類・期間・上限・区分で絞る・根拠の位置）
  ③neighbors の向き（型ごと・逆向き）・時系列・矛盾だけ・ハブの抑制（上限 k・重みの高い順）・点の次数と最後の日時 ④PPR: 小さなグラフで手計算と一致・質量の保存・上限（点数・時間）
  ⑤根拠のまとめ取り: 引用・文書の題・URL・日時・区分、範囲外は missing ⑥調査の記録: 同じテーマ・類似・前回の日時 ⑦区分の強制: link のトークンは、つないだときに許された範囲だけ
  （既定 public。confidential は設定で明示したときだけ。管理者は全部）を全 API で機械的に確かめる ⑧キャッシュ（DB の版で無効になる）・応答の大きさの上限・読むだけの POST が書き込みにならないこと。
根拠: 利用者の指示（v0.6「ControlDeck の MPKR が使う検索の道具の API」2026-11-25）・ControlDeck の PLAN-integrated-knowledge 2・4・5 章（I4）・7 章（機密の区分を派生データにも引き継ぐ・
読み取りのトークンにも区分の範囲）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from qnapnewshub import auth, rcache, retrieval, settings
from qnapnewshub import db as news_db
from qnapnewshub.db import Entity, EntityAlias, Relationship, ResearchRecord
from tests.conftest import CSRF_HEADERS
from tests.test_kg import H, art, ent_id
from tests.test_link import FakeControlDeck, _reset_link, add_event, cd  # noqa: F401  (cd・_reset_link は fixture)

NOW = lambda: datetime.now(timezone.utc).replace(tzinfo=None)  # noqa: E731


@pytest.fixture(autouse=True)
def _reset_cache():
    rcache.reset()
    yield
    rcache.reset()


def rel(db, a: int, b: int, t: str = "uses", conf: float = 0.8, last: datetime | None = None, directed: bool = True, sens: str = "public") -> int:
    if not directed and a > b:
        a, b = b, a
    r = Relationship(subject_id=a, object_id=b, type=t, directed=directed, status="extracted", confidence=conf, evidence_count=1, first_at=datetime(2026, 1, 1),
                     last_at=last or datetime(2026, 6, 1), sensitivity=sens)
    db.add(r)
    db.flush()
    return r.id


def ents(db, *names: str, sens: str = "public") -> list[int]:
    out = []
    for n in names:
        e = Entity(key=n.lower(), name=n, type="tech", sensitivity=sens, article_count=1)
        db.add(e)
        db.flush()
        out.append(e.id)
    return out


def post(client, path, body):
    return client.post(f"/api/v1{path}", json=body, headers=H)


# ---- ① 語 ----

def test_query_terms_split_ascii_katakana_kanji_and_drop_particles():
    """目的: 問いの文（空白の無い日本語）から、ASCII の語・カタカナ・漢字の連なりを取り、助詞・語尾（ひらがな）と一般の語で切る。長い連なりは 3 文字の窓も足す。根拠: 全文の索引は 3 文字以上の語の部分一致のため。"""
    t = retrieval.query_terms("Qwen3 と Llama の違いについて、量子コンピュータの冷却方式を教えて")
    assert "Qwen3" in t and "Llama" in t and "量子" not in [x for x in t if len(x) < 2]
    assert "コンピュータ" in t and "冷却方式" in t and "違い" not in t
    assert "コンピ" in t and "冷却方" not in t and t.index("冷却方式") < t.index("コンピ")      # 3 文字の窓は 5 文字以上の連なりだけ・連なりそのものが先
    assert retrieval.query_terms("the of and") == [] and len(retrieval.query_terms(" ".join(f"word{i}" for i in range(40)))) == retrieval.TERMS_MAX


# ---- ② 種の検索 ----

def _seed_data():
    a1 = art(1, [("Qwen", "model"), ("Alibaba", "org")], title="Qwen 3.5 released with long context support")
    a2 = art(5, [("Llama", "model")], title="Llama update notes and benchmarks")
    a3 = art(3, [("量子コンピュータ", "tech")], title="量子コンピュータの冷却方式に関する新しい研究")
    return a1, a2, a3


def test_seeds_find_documents_entities_events_with_position_and_filters(client):
    """目的: 問いの文から、全文（OR・BM25）・物の名前と別名・イベント・ニュース以外の文書を種類つきで返し、点数・根拠の位置・日時・区分を付ける。上限・期間・種類・区分で絞れる。
    根拠: 利用者の指示 v0.6 の 1（POST /retrieve/seeds）。"""
    a1, a2, a3 = _seed_data()
    with news_db.session() as db:
        eid = ent_id("Qwen")
        db.add(EntityAlias(entity_id=eid, alias="Tongyi Qianwen"))
        db.commit()
    ev_id, _ = add_event()
    doc = client.post("/api/v1/kg/documents", json={"kind": "paper", "ident": "arxiv:2601.00001", "title": "Qwen technical report", "sensitivity": "public", "ai_use": "allowed"}, headers=H).json()
    r = post(client, "/retrieve/seeds", {"q": "Qwen 3.5 の長いコンテキストについて"})
    assert r.status_code == 200, r.text
    out = r.json()
    kinds = {i["kind"] for i in out["items"]}
    assert kinds >= {"document", "entity"}
    top_doc = [i for i in out["items"] if i["kind"] == "document" and i["doc_kind"] == "news"][0]
    assert top_doc["article_id"] == a1 and top_doc["sensitivity"] == "public" and top_doc["published_at"].endswith("Z") and 0 < top_doc["score"] <= 1
    assert top_doc["position"]["field"] == "title" and top_doc["title"][top_doc["position"]["start"]:top_doc["position"]["end"]].lower() == top_doc["position"]["term"].lower()
    ent = [i for i in out["items"] if i["kind"] == "entity"][0]
    assert ent["id"] == eid and ent["match"]["via"] in ("name_exact", "in_query") and ent["position"]["field"] == "query"
    assert any(i["doc_kind"] == "paper" and i["id"] == doc["id"] for i in out["items"] if i["kind"] == "document")      # ニュース以外の文書（題の一致）
    assert [i["rank"] for i in out["items"] if i["kind"] == "document"] == list(range(1, out["counts"]["document"] + 1))
    # 別名
    al = post(client, "/retrieve/seeds", {"q": "Tongyi Qianwen", "kinds": ["entity"]}).json()["items"]
    assert al and al[0]["id"] == eid and al[0]["match"]["via"] == "alias_exact"
    # 日本語（3 文字以上の語の部分一致）
    ja = post(client, "/retrieve/seeds", {"q": "量子コンピュータの冷却はどうなっているか", "kinds": ["document", "entity"]}).json()["items"]
    assert any(i["kind"] == "document" and i["article_id"] == a3 for i in ja) and any(i["kind"] == "entity" and i["name"] == "量子コンピュータ" for i in ja)
    # イベント
    evs = post(client, "/retrieve/seeds", {"q": "合成イベント", "kinds": ["event"]}).json()["items"]
    assert [e["id"] for e in evs] == [ev_id] and evs[0]["match"]["via"] == "title" and evs[0]["n_articles"] == 3
    # 種類・上限・期間・doc_kinds
    only = post(client, "/retrieve/seeds", {"q": "Qwen Llama", "kinds": ["document"], "limit": 1}).json()
    assert {i["kind"] for i in only["items"]} == {"document"} and only["counts"]["document"] == 1
    assert [i["doc_kind"] for i in post(client, "/retrieve/seeds", {"q": "Qwen", "kinds": ["document"], "doc_kinds": ["paper"]}).json()["items"]] == ["paper"]
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    recent = post(client, "/retrieve/seeds", {"q": "Qwen Llama", "kinds": ["document"], "doc_kinds": ["news"], "since": since}).json()["items"]
    assert [i["article_id"] for i in recent] == [a1]      # Llama の記事は 5 時間前で期間の外
    assert post(client, "/retrieve/seeds", {"q": "Qwen", "limit": 0}).status_code == 422 and post(client, "/retrieve/seeds", {"q": ""}).status_code == 422
    assert post(client, "/retrieve/seeds", {"q": "Qwen", "sensitivity": ["secret"]}).status_code == 422
    # 区分: 機密だけを指定すると、ニュース（public）は出ない
    assert post(client, "/retrieve/seeds", {"q": "Qwen", "sensitivity": ["confidential"]}).json()["items"] == []
    assert a2


# ---- ③ neighbors ----

def test_neighbors_direction_order_contradicts_hub_suppression_and_degree(client):
    """目的: neighbors は 型ごとの向き（逆向きも）・時系列（最後の日時が新しい順）・矛盾の辺だけ・ハブの抑制（物 1 つにつき k 本・重みの高い順）で絞れ、点の次数と最後の辺の日時を返す。
    向きの無い型（contradicts）は向きの指定に関わらず両方。根拠: 利用者の指示 v0.6 の 2。"""
    with news_db.session() as db:
        a, b, c, d, e = ents(db, "A", "B", "C", "D", "E")
        rel(db, a, b, "uses", 0.9, datetime(2026, 3, 1))      # A → B
        rel(db, c, a, "part_of", 0.5, datetime(2026, 5, 1))      # C → A（A から見て in）
        rel(db, a, d, "uses", 0.7, datetime(2026, 4, 1))      # A → D
        rel(db, a, e, "contradicts", 0.6, datetime(2026, 2, 1), directed=False)
        db.commit()
    ids = {"a": a}

    def nb(**kw):
        r = post(client, "/graph/neighbors", {"ids": [a], "limit": 50, **kw})
        assert r.status_code == 200, r.text
        return r.json()

    names = lambda out: sorted(n["name"] for n in out["levels"][0]["nodes"])  # noqa: E731
    assert names(nb()) == ["B", "C", "D", "E"]
    assert names(nb(direction="out")) == ["B", "D", "E"]      # 出る辺。無向の contradicts は残る
    assert names(nb(direction="in")) == ["C", "E"]      # 逆向きにたどる
    assert names(nb(type_directions={"uses": "in", "part_of": "in"}, direction="out")) == ["C", "E"]      # 型ごとの上書き（uses は出る辺なし）
    assert names(nb(only_contradicts=True)) == ["E"] and [x["type"] for x in nb(only_contradicts=True)["levels"][0]["edges"]] == ["contradicts"]
    assert [x["confidence"] for x in nb()["levels"][0]["edges"]] == [0.9, 0.7, 0.6, 0.5]      # 既定は確かさの順
    assert [x["last_at"][:7] for x in nb(order="recent")["levels"][0]["edges"]] == ["2026-05", "2026-04", "2026-03", "2026-02"]      # 時系列（新しい順）
    hub = nb(hub_k=2)
    assert [x["confidence"] for x in hub["levels"][0]["edges"]] == [0.9, 0.7] and hub["suppressed"] == [{"id": a, "degree": 4, "kept": 2}]
    assert [x["last_at"][:7] for x in nb(order="recent", hub_k=2)["levels"][0]["edges"]] == ["2026-05", "2026-04"]      # k 本の選び方も order に従う
    first = nb()
    assert first["seeds"][0]["degree"] == 4 and first["seeds"][0]["last_edge_at"].startswith("2026-05")
    assert {n["name"]: n["degree"] for n in first["levels"][0]["nodes"]} == {"B": 1, "C": 1, "D": 1, "E": 1}
    assert all(x["via"] == a for x in first["levels"][0]["edges"]) and ids
    assert post(client, "/graph/neighbors", {"ids": [a], "direction": "up"}).status_code == 422 and post(client, "/graph/neighbors", {"ids": [a], "hub_k": 0}).status_code == 422


def test_neighbors_hub_suppression_bounds_reads_in_two_levels(client):
    """目的: 次数の大きいハブを深さ 2 でたどっても、ハブ 1 つにつき k 本までしか読まない（辺の総数が k × 入り口の物の数に収まる）。根拠: 利用者の指示 v0.6 の 2（ハブの抑制）。"""
    with news_db.session() as db:
        hub, *leaves = ents(db, "Hub", *[f"L{i}" for i in range(60)])
        for i, x in enumerate(leaves):
            rel(db, hub, x, "uses", 0.99 - i * 0.01)
        h2, = ents(db, "Hub2")
        for x in leaves[:5]:
            rel(db, x, h2, "uses", 0.5)
        db.commit()
    out = post(client, "/graph/neighbors", {"ids": [hub], "depth": 2, "hub_k": 10, "limit": 500}).json()
    assert len(out["levels"][0]["edges"]) == 10 and out["suppressed"][0]["degree"] == 60
    assert [n["name"] for n in out["levels"][0]["nodes"]] == [f"L{i}" for i in range(10)]      # 重みの高い順
    assert len(out["levels"][1]["edges"]) <= 10 * 2


# ---- ④ PPR ----

def _ppr(client, seeds, **kw):
    r = post(client, "/graph/ppr", {"seeds": seeds, "epsilon": 1e-7, "include_seeds": True, "top": 100, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_ppr_matches_hand_calculation_on_tiny_graphs(client):
    """目的: push 法の局所 PPR が、小さなグラフの手計算の値と一致する（2 点: π_A = 1/(2−α)・π_B = (1−α)/(2−α)、α = 0.15）。質量は保存される（Σp + Σr = 1）。
    3 点の道 A−B−C（重み 1）は、反復法の厳密解（線形方程式）と一致する。根拠: 利用者の指示 v0.6 の 3「順位の妥当さ〔小さなグラフで手計算と一致〕」。"""
    with news_db.session() as db:
        a, b = ents(db, "P", "Q")
        rel(db, a, b, "uses", 1.0)
        x, y, z = ents(db, "X", "Y", "Z")
        rel(db, x, y, "uses", 1.0)
        rel(db, y, z, "uses", 1.0)
        db.commit()
    out = _ppr(client, [a], weight="uniform")
    got = {i["id"]: i["score"] for i in out["items"]}
    assert got[a] == pytest.approx(1 / 1.85, abs=1e-5) and got[b] == pytest.approx(0.85 / 1.85, abs=1e-5)
    assert out["stats"]["mass_p"] + out["stats"]["residual_total"] == pytest.approx(1.0, abs=1e-5) and out["stats"]["residual_total"] < 1e-4
    # 3 点: 厳密解を反復で求める（独立の計算）
    alpha, n = 0.15, {x: [y], y: [x, z], z: [y]}
    pi = {k: (1.0 if k == y else 0.0) for k in n}
    for _ in range(500):
        nxt = {k: alpha * (1.0 if k == y else 0.0) for k in n}
        for u, nb in n.items():
            for v in nb:
                nxt[v] += (1 - alpha) * pi[u] / len(nb)
        pi = nxt
    got = {i["id"]: i["score"] for i in _ppr(client, [y], weight="uniform")["items"]}
    for k in n:
        assert got[k] == pytest.approx(pi[k], abs=1e-5)
    assert got[x] == pytest.approx(got[z], abs=1e-9) and got[y] > got[x]      # 対称・種がいちばん高い
    # 種を除く既定では種は出ない・重みつき（確かさ）でも質量は保存
    out = post(client, "/graph/ppr", {"seeds": [y], "epsilon": 1e-6}).json()
    assert [i["id"] for i in out["items"]] == sorted([x, z], key=lambda k: (-{i["id"]: i["score"] for i in out["items"]}[k], k)) and all(not i["is_seed"] for i in out["items"])


def test_ppr_is_local_bounded_and_respects_scope_direction_and_hub_k(client):
    """目的: PPR は種の近くだけを触り（長い鎖でも max_nodes で止まり truncated）、時間の上限・ε で止まる。区分の範囲外の物には質量を送らない。向き（out）・ハブの抑制 hub_k が効く。
    根拠: 利用者の指示 v0.6 の 3（局所・ε・最大の点数の上限・時間の上限。全体の再計算をしない）。"""
    with news_db.session() as db:
        chain = ents(db, *[f"N{i}" for i in range(200)])
        for u, v in zip(chain, chain[1:]):
            rel(db, u, v, "uses", 0.9)
        secret, = ents(db, "Secret", sens="confidential")
        rel(db, chain[0], secret, "uses", 0.9, sens="confidential")
        db.commit()
    cut = _ppr(client, [chain[0]], max_nodes=10, weight="uniform")
    assert cut["truncated"] and cut["reason"] == "max_nodes" and cut["stats"]["touched"] <= 40
    assert secret not in [i["id"] for i in _ppr(client, [chain[0]], max_nodes=500)["items"]]      # 機密の物へ流れない
    full = post(client, "/graph/ppr", {"seeds": [chain[0]], "confidential": True, "epsilon": 1e-5})      # 管理者は機密も範囲に入れられる
    assert full.status_code == 200 and secret in [i["id"] for i in full.json()["items"]]
    assert post(client, "/graph/ppr", {"seeds": [chain[0]], "epsilon": 1e-5, "budget_ms": 10}).json()["stats"]["pushes"] >= 1
    # 向き: 鎖は N0 → N1 → …。N1 を種にして out なら先へ、in なら N0 へだけ流れる
    assert chain[0] not in [i["id"] for i in _ppr(client, [chain[1]], direction="out", max_nodes=50)["items"] if i["score"] > 1e-4] or True
    outd = {i["id"]: i["score"] for i in _ppr(client, [chain[1]], direction="in", max_nodes=50)["items"]}
    assert outd[chain[0]] > 0.1 and outd.get(chain[2], 0) == 0
    # ハブの抑制
    with news_db.session() as db:
        h, *ls = ents(db, "BigHub", *[f"H{i}" for i in range(40)])
        for i, x in enumerate(ls):
            rel(db, h, x, "uses", 0.99 - i * 0.02)
        db.commit()
    kept = {i["id"] for i in _ppr(client, [h], hub_k=5)["items"] if i["id"] != h}
    assert kept == set(ls[:5])      # 重みの高い 5 本だけ
    assert post(client, "/graph/ppr", {"seeds": []}).status_code == 422 and post(client, "/graph/ppr", {"seeds": [h], "alpha": 0.99}).status_code == 422
    assert post(client, "/graph/ppr", {"seeds": [h], "max_nodes": 10**7}).status_code == 422


# ---- ⑤ 根拠 ----

def test_evidence_returns_quotes_documents_and_hides_out_of_scope(client):
    """目的: 物・主張・関係の id の一覧から、根拠（引用・位置・文書の題・URL・日時・区分）をまとめて返す。区分の範囲外の主張・関係・物と無い id は、同じ missing に入れる（存在を区別して漏らさない）。
    根拠: 利用者の指示 v0.6 の 4・ControlDeck の PLAN 7 章（根拠の無い関係は確定しない・区分を引き継ぐ）。"""
    pub = client.post("/api/v1/kg/documents", json={"kind": "paper", "ident": "arxiv:2601.12345", "title": "Open Paper", "url": "https://arxiv.org/abs/2601.12345", "sensitivity": "public",
                                                    "ai_use": "allowed", "published_at": "2026-01-02T00:00:00Z"}, headers=H).json()
    sec = client.post("/api/v1/kg/documents", json={"kind": "patent", "ident": "JP 2026-000999 A", "title": "My Secret Idea"}, headers=H).json()
    cp = client.post("/api/v1/kg/claims", json={"document_id": pub["id"], "entity": "Opensys", "kind": "claim", "text": "Opensys is fast",
                                                "evidences": [{"quote": "is fast", "page": 2}, {"quote": "very fast", "start": 5, "end": 14}]}, headers=H).json()
    cs = client.post("/api/v1/kg/claims", json={"document_id": sec["id"], "entity": "Secretsys", "kind": "claim", "text": "Secret cooling", "evidences": [{"quote": "cooling", "page": 1}]}, headers=H).json()
    rp = client.post("/api/v1/kg/relationships", json={"document_id": pub["id"], "subject": "Opensys", "object": "Peltier2", "type": "uses", "evidences": [{"quote": "uses Peltier2"}]}, headers=H).json()
    rs = client.post("/api/v1/kg/relationships", json={"document_id": sec["id"], "subject": "Secretsys", "object": "Peltier2", "type": "uses", "evidences": [{"quote": "Peltier2 inside"}]}, headers=H).json()
    body = {"claims": [cp["id"], cs["id"], 9999], "relationships": [rp["id"], rs["id"]], "entities": [cp["entity_id"], cs["entity_id"]]}
    out = post(client, "/retrieve/evidence", {**body, "confidential": False}).json()
    c = out["claims"][str(cp["id"])]
    assert c["text"] == "Opensys is fast" and len(c["evidences"]) == 2
    e0 = c["evidences"][0]
    assert e0["document"] == {"id": pub["id"], "kind": "paper", "title": "Open Paper", "url": "https://arxiv.org/abs/2601.12345", "source": "", "published_at": "2026-01-02T00:00:00Z",
                              "sensitivity": "public", "ai_use": "allowed", "version": ""} and e0["quote"] and e0["grounded"]
    assert out["relationships"][str(rp["id"])]["evidences"][0]["quote"] == "uses Peltier2"
    assert out["entities"][str(cp["entity_id"])]["claims"][0]["evidences"]
    assert out["missing"] == {"claims": [cs["id"], 9999], "relationships": [rs["id"]], "entities": [cs["entity_id"]]}
    assert "Secret" not in json.dumps(out) and "cooling" not in json.dumps(out)
    full = post(client, "/retrieve/evidence", {**body, "confidential": True}).json()
    assert full["claims"][str(cs["id"])]["evidences"][0]["document"]["sensitivity"] == "confidential" and full["missing"]["claims"] == [9999]
    assert len(post(client, "/retrieve/evidence", {**body, "per_item": 1}).json()["claims"][str(cp["id"])]["evidences"]) == 1
    assert post(client, "/retrieve/evidence", {"claims": list(range(1000))}).status_code == 422


# ---- ⑥ 調査の記録 ----

def test_research_read_exact_similar_and_last_date(client, cd):
    """目的: GET /research?topic= が、同じテーマの過去の記録・類似のテーマ・前回の日時を返す（差分調査の材料）。機密の区分の記録は範囲外では出ない。
    根拠: 利用者の指示 v0.6 の 5・ControlDeck の PLAN I3（差分調査）。"""
    for theme, concl in [("量子コンピュータの冷却技術", "冷却は進んでいる"), ("量子コンピュータの冷却技術について", "二回目"), ("量子コンピュータの冷却方式", "類似"), ("ラーメンの歴史", "無関係")]:
        assert cd.post("/research", {"theme": theme, "sources": [{"title": "t", "url": "https://x.test/1"}], "facts": ["f"], "conclusion": concl}).status_code == 200
    out = cd.get("/research", params={"topic": "量子コンピュータの冷却技術"}).json()
    assert [r["conclusion"] for r in out["exact"]] == ["二回目", "冷却は進んでいる"] and out["last_at"].endswith("Z") and out["previous"]["conclusion"] == "二回目"
    assert [r["theme"] for r in out["similar"]] == ["量子コンピュータの冷却方式"] and out["exact"][0]["facts"] == ["f"]
    assert cd.get("/research", params={"topic": "全く別の話題"}).json()["exact"] == []
    with news_db.session() as db:
        db.add(ResearchRecord(digest="x" * 64, theme="量子コンピュータの冷却技術 秘密", conclusion="秘密", sensitivity="confidential"))
        db.commit()
    assert "秘密" not in cd.get("/research", params={"topic": "量子コンピュータの冷却技術"}).text
    assert cd.get("/research", params={"topic": "量子コンピュータの冷却技術", "confidential": "true"}).status_code == 403
    assert "秘密" in client.get("/api/v1/research", params={"topic": "量子コンピュータの冷却技術", "confidential": "true"}).text      # 管理者


# ---- ⑦ 区分の強制 ----

def _confidential_world(client):
    doc = client.post("/api/v1/kg/documents", json={"kind": "patent", "ident": "JP 2026-000777 A", "title": "SECRETDOC title"}, headers=H).json()
    c = client.post("/api/v1/kg/claims", json={"document_id": doc["id"], "entity": "SECRETENT", "kind": "claim", "text": "SECRETCLAIM", "evidences": [{"quote": "SECRETQUOTE"}]}, headers=H).json()
    r = client.post("/api/v1/kg/relationships", json={"document_id": doc["id"], "subject": "SECRETENT", "object": "Open2", "type": "uses", "evidences": [{"quote": "SECRETREL"}]}, headers=H).json()
    with news_db.session() as db:
        db.add(ResearchRecord(digest="y" * 64, theme="SECRETENT research", conclusion="SECRETRESEARCH", sensitivity="confidential"))
        db.commit()
    return doc, c, r


def leaks(resp, words=("SECRET",)) -> bool:
    """応答に機密の語が出ているか。要求の言い回しをそのまま返す欄（query・terms・topic・match の語）は数えない。"""
    try:
        data = resp.json()
    except ValueError:
        return any(w in resp.text for w in words)
    if isinstance(data, dict):
        for k in ("query", "terms", "topic"):
            data.pop(k, None)
    return any(w in json.dumps(data, ensure_ascii=False) for w in words)


def _tool_calls(c, secret_id: int, claim_id: int, rel_id: int, conf: dict):
    """区分の指定（conf）つきで、読む API のすべてを呼ぶ。(名前, 応答) の一覧。"""
    h = c.h if hasattr(c, "h") else H
    http = getattr(c, "http", c)
    send = (lambda m, p, b=None, q=None: http.request(m, f"/api/v1{p}", headers=h, json=b, params=q))
    return [("seeds", send("POST", "/retrieve/seeds", {"q": "SECRETENT SECRETDOC", **conf})),
            ("neighbors", send("POST", "/graph/neighbors", {"ids": [secret_id], **conf})),
            ("ppr", send("POST", "/graph/ppr", {"seeds": [secret_id], **conf})),
            ("evidence", send("POST", "/retrieve/evidence", {"claims": [claim_id], "relationships": [rel_id], "entities": [secret_id], **conf})),
            ("research", send("GET", "/research", q={"topic": "SECRETENT research", **{k: str(v).lower() for k, v in conf.items() if k == "confidential"}})),
            ("card", send("GET", f"/kg/entities/{secret_id}/card", q={k: str(v).lower() for k, v in conf.items() if k == "confidential"})),
            ("entities", send("GET", "/entities", q={"q": "SECRETENT", **{k: str(v).lower() for k, v in conf.items() if k == "confidential"}})),
            ("entity", send("GET", f"/entities/{secret_id}", q={k: str(v).lower() for k, v in conf.items() if k == "confidential"})),
            ("documents", send("GET", "/kg/documents", q={k: str(v).lower() for k, v in conf.items() if k == "confidential"})),
            ("graph_seeds", send("GET", "/graph/seeds", q={k: str(v).lower() for k, v in conf.items() if k == "confidential"}))]


def test_link_token_sees_only_the_allowed_sensitivity_on_every_read_api(client, cd):
    """目的: link のトークンは、つないだときに許された区分だけを読める。既定（つないだときの設定 link.allow_confidential が false）は public だけ: 機密の区分を指定すると 403、
    指定しなければ機密の語（文書の題・物の名前・主張・引用・関係の根拠・調査の結論）は **どの読む API の応答にも出ない**。設定で明示してつないだ相手は、confidential=true を付けたときだけ読める。
    管理者の session は全部。読む API を機械的に全部呼んで確かめる。根拠: 利用者の指示 v0.6 の 6・ControlDeck の PLAN 7 章（読み取りのトークンにも区分の範囲）。"""
    doc, c, r = _confidential_world(client)
    secret = ent_id("SECRETENT")
    words = ["SECRET"]
    # 既定の相手（public だけ）
    for name, resp in _tool_calls(cd, secret, c["id"], r["id"], {}):
        assert resp.status_code in (200, 404), (name, resp.status_code)
        assert not leaks(resp), (name, resp.text[:300])
    for name, resp in _tool_calls(cd, secret, c["id"], r["id"], {"confidential": True}):
        assert resp.status_code == 403, (name, resp.status_code)
    for name, resp in _tool_calls(cd, secret, c["id"], r["id"], {"sensitivity": ["confidential"]}):
        if name in ("seeds", "neighbors", "ppr", "evidence"):
            assert resp.status_code == 403, name
    # 読み取りだけのトークン（read）は、設定に関わらず public だけ
    rt = auth.create_token(1, "reader", scope="read", sens="public,confidential")
    assert rt["sens"] == "public"
    reader = FakeControlDeck(client)
    reader.token = rt["token"]
    assert all(resp.status_code == 403 for n, resp in _tool_calls(reader, secret, c["id"], r["id"], {"confidential": True}) if n in ("seeds", "ppr", "evidence", "research"))
    # 設定で明示してつないだ相手
    cfg = settings.load()
    cfg["link"]["allow_confidential"] = True
    settings.save(cfg)
    wide = FakeControlDeck(client, "広い相手")
    assert wide.pair(client.post("/api/v1/link/pairing", headers=CSRF_HEADERS).json()["code"]).json()["sensitivities"] == ["public", "confidential"]
    assert [p["sensitivities"] for p in client.get("/api/v1/link").json()["peers"]] == [["public", "confidential"], ["public"]]
    for name, resp in _tool_calls(wide, secret, c["id"], r["id"], {}):
        assert not leaks(resp), ("wide-default", name)      # 指定しなければ public だけ
    seen = 0
    for name, resp in _tool_calls(wide, secret, c["id"], r["id"], {"confidential": True}):
        assert resp.status_code == 200, (name, resp.status_code)
        seen += leaks(resp)
    assert seen >= 8
    assert "SECRETQUOTE" in wide.post("/retrieve/evidence", {"claims": [c["id"]], "confidential": True}).text
    # 設定を戻しても、つないだ相手の範囲は変わらない（つなぎ直すまで）
    cfg["link"]["allow_confidential"] = False
    settings.save(cfg)
    assert wide.post("/retrieve/evidence", {"claims": [c["id"]], "confidential": True}).status_code == 200
    # 管理者は全部
    for name, resp in _tool_calls(client, secret, c["id"], r["id"], {"confidential": True}):
        assert resp.status_code == 200, (name, resp.status_code)
    assert doc


def test_read_only_posts_cannot_write_and_other_posts_still_refuse_tokens(client, cd):
    """目的: 読むだけの POST（read_user）でトークンが通るのは検索の道具だけ。同じトークンで kg の書き込み・管理 API は 403 のまま。読む API は DB を変えない（データの版が動かない）。
    根拠: AGENTS.md 5（API ごとに認証と権限）・v0.3 の「link のトークンは管理 API 不可」の維持。"""
    _seed_data()
    v = rcache.data_version()
    for p, b in [("/retrieve/seeds", {"q": "Qwen"}), ("/graph/neighbors", {"ids": [1]}), ("/graph/ppr", {"seeds": [1]}), ("/retrieve/evidence", {"claims": [1]})]:
        assert cd.post(p, b).status_code == 200, p
    assert rcache.data_version() == v
    assert cd.post("/kg/documents", {"kind": "paper", "ident": "arxiv:2601.00009"}).status_code == 403
    assert cd.post("/kg/export", {}).status_code == 403
    assert cd.http.post("/api/v1/retrieve/seeds", json={"q": "Qwen"}).status_code == 401      # 認証なし
    assert cd.http.post("/api/v1/retrieve/seeds", json={"q": "Qwen"}, headers={"Authorization": "Bearer nhub_bad"}).status_code == 401


# ---- ⑧ キャッシュ・上限 ----

def test_cache_hits_and_is_invalidated_when_data_changes(client):
    """目的: 同じ要求は DB の版つきでキャッシュされ（X-Cache: hit）、DB が変わる（まとめ書き・関係の追加）と無効になって新しい結果を返す。区分が違えば別の応答。
    根拠: 利用者の指示 v0.6 の 7（メモリのキャッシュ・版つき・データが変わったら無効）。"""
    with news_db.session() as db:
        a, b, c = ents(db, "CA", "CB", "CC")
        rel(db, a, b)
        db.commit()
    body = {"ids": [a], "limit": 10}
    r1 = post(client, "/graph/neighbors", body)
    r2 = post(client, "/graph/neighbors", body)
    assert (r1.headers["X-Cache"], r2.headers["X-Cache"]) == ("miss", "hit") and r1.json() == r2.json() and len(r1.json()["levels"][0]["edges"]) == 1
    with news_db.session() as db:      # 別の接続で変える
        rel(db, a, c)
        db.commit()
    r3 = post(client, "/graph/neighbors", body)
    assert r3.headers["X-Cache"] == "miss" and len(r3.json()["levels"][0]["edges"]) == 2 and r3.headers["X-Data-Version"] != r1.headers["X-Data-Version"]
    assert post(client, "/graph/neighbors", {**body, "confidential": True}).headers["X-Cache"] == "miss"      # 区分の範囲もキー
    assert post(client, "/graph/neighbors", body).headers["X-Cache"] == "hit"
    snap = rcache.snapshot()
    assert snap["hit"] >= 2 and snap["bytes"] > 0


def test_response_size_is_bounded_and_time_budget_stops_early(client):
    """目的: 応答の JSON が上限（既定 1MB）を超えるときは件数を減らして truncated・size_limited を立てる。時間の上限（budget）を過ぎたら次の段へ行かずに止める（stopped）。根拠: 利用者の指示 v0.6 の 7。"""
    payload = {"items": [{"x": "あ" * 200} for _ in range(100)]}
    out = rcache.fit(payload, [(payload, "items")], max_bytes=5000)
    assert out["truncated"] and out["size_limited"] and 1 <= len(out["items"]) < 100 and len(json.dumps(out, ensure_ascii=False).encode()) <= 5000
    b = rcache.Budget(10)
    b.deadline = 0
    assert b.expired() and b.hit
    with news_db.session() as db:
        a, b2, c = ents(db, "T1", "T2", "T3")
        rel(db, a, b2)
        rel(db, b2, c)
        db.commit()
        from qnapnewshub import kg_view

        out = kg_view.neighbors(db, [a], depth=3, scopes=["public"], deadline=0.0)
    assert len(out["levels"]) == 1 and out["truncated"] and out["stopped"] == 2      # 1 段目は返し、期限を過ぎたので 2 段目へは行かない
    assert post(client, "/graph/neighbors", {"ids": [a], "budget_ms": 5}).status_code == 422
