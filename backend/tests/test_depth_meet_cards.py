"""目的: ①検索の「会合」（上位の種から別々に展開し、2 つ以上の種から届いた物・辺へ加点）と問いで決める深さ（既定 1・物を結ぶ問いだけ最大 5）②橋渡しの問い（経路 2〜6 段）の作り方
③知識カードの固定の形（context の text・並べ替え・空の節を出さない）④カードの夜の仕事の失敗の扱いと診断（通信の失敗は数えずやり直す・次に走る時刻・最後の結果・概要の無い数）
⑤地図のローカルの深さ 5（API の上限）を確かめる。外へ通信しない・LLM は模擬。
根拠: 利用者の指示（2026-11-25「知識の接続の深さを今の最大 3 から 5 まで試したい。無理な適用ではなく、評価して妥協点で」）・評価の置き場の「Things の抽出とカードの調査と測定」
（深さだけを上げても点数は 1 問も動かない／会合を足して初めて橋渡しの問いで効く／複数文書の問いは深いほど下がる＝既定は 1）・AGENTS.md（異常を見つける診断・新しい試験は目的と根拠）。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from qnapnewshub import db as news_db
from qnapnewshub import kg_job, llm, rcache, retrieval, settings, things
from qnapnewshub.db import Entity, EntityCard, NewsMeta
from tests.test_kg import H, art, ent_id
from tests.test_retrieval import _reset_cache, ents, rel  # noqa: F401  (_reset_cache は autouse の fixture)
from tools.eval_retrieval import build_questions


def _expand(ids, scores, q, **kw):
    with news_db.session() as db:
        return retrieval.expand_meet(db, ids, scores, q, scopes=kw.pop("scopes", ["public"]), budget=kw.pop("budget", rcache.Budget(5000)), **kw)


def _chain(n: int, names: list[str] | None = None) -> list[int]:
    """一本の鎖 e0 - e1 - … - e(n-1)（確かさは同じ）。"""
    names = names or [f"Node{chr(97 + i)}x" for i in range(n)]
    with news_db.session() as db:
        ids = ents(db, *names)
        for a, b in zip(ids, ids[1:]):
            rel(db, a, b)
        db.commit()
    return ids


# ---- ① 深さの決め方 ----

def test_depth_plan_default_is_one_and_only_bridging_questions_go_deep():
    """目的: 既定の深さは 1。種が 2 つ未満なら何を問うても 1（会合が起きない）。つなぐ語（関係・経由・between）があれば最大 5、比較・変化の語なら 2。
    名前を並べただけの「A と B」は深くしない（複数文書の問いが深いほど下がったため）。根拠: 測定の 3.3・3.4（複数文書 -0.05・種が 1 つなら深さは無益）。"""
    p = retrieval.depth_plan
    assert p("Alphax の最新の版は", 1, 3)["cap"] == 1 and p("Alphax と Betax をめぐる出来事は", 2, 3)["cap"] == 1
    assert p("Alphax と Betax の関係は", 2, 1)["cap"] == 1      # 種が 1 つ
    plan = p("Alphax と Betax はどうつながっているか", 2, 3)
    assert plan["cap"] == 5 and plan["kind"] == "connect" and "つなが" in plan["cues"]
    assert p("how are Alphax and Betax connected? relation between them", 2, 2)["cap"] == 5
    assert p("Alphax と Betax の違いは", 2, 3) == {"cap": 2, "kind": "compare", "named": 2, "cues": ["違い"]}
    assert p("", 0, 0)["cap"] == 1


def test_expand_meet_bonus_and_stops_at_first_meeting():
    """目的: 鎖 A-m-B（経路 2 段）で、種 A・B から別々に展開すると m が両方から届き（会合）、加点されて他の物より上になる。自動の深さは最初の会合の段 + 1 で止まる
    （長い鎖でも 5 まで行かない）。深さを数字で固定すればその段まで。根拠: 測定の最適な深さは経路の長さの半分前後。"""
    a, m, b = _chain(3, ["Alphax", "Midx", "Betax"])
    ex = _expand([a, b], [0.9, 0.8], "Alphax と Betax はどうつながっているか")
    nodes = {n["id"]: n for n in ex["nodes"]}
    assert ex["plan"]["kind"] == "connect" and nodes[m]["meet"] and sorted(nodes[m]["seeds"]) == sorted([a, b]) and ex["meet"]["nodes"] >= 1
    assert nodes[m]["score"] > 0.3 and ex["depth"] == 2 and all(n["score"] <= 0.99 for n in ex["nodes"])
    plain = _expand([a, b], [0.9, 0.8], "Alphax と Betax の最新の版", meet=False)
    assert plain["depth"] == 1 and plain["meet"] == {"nodes": 0, "edges": 0} and max(n["score"] for n in plain["nodes"]) < 0.9 * retrieval.NEIGHBOR_DECAY + 1e-9
    ids = _chain(7, [f"Longn{i}x" for i in range(7)])      # 6 段（端どうし）。真ん中 Longn3x へ届くのは深さ 3
    deep = _expand([ids[0], ids[-1]], [0.9, 0.8], "Longn0x と Longn6x はどうつながっているか")
    got = {n["id"]: n for n in deep["nodes"]}
    assert deep["depth"] == 4 and got[ids[3]]["meet"]      # 最初の会合は 3 段、+1
    assert _expand([ids[0], ids[-1]], [0.9, 0.8], "Longn0x と Longn6x はどうつながっているか", depth=5)["depth"] == 5
    assert _expand([ids[0], ids[-1]], [0.9, 0.8], "Longn0x と Longn6x はどうつながっているか", depth=1)["depth"] == 1


def test_expand_meet_respects_scope_and_time_and_candidate_caps():
    """目的: 区分の範囲外の物は出さない・期限が過ぎたら 2 段目以降へ行かない（1 段目は返す）・返す候補は上限を超えない。根拠: AGENTS.md（上限を守る）・PLAN 7 章（機密を公開の範囲に出さない）。"""
    a, m, b = _chain(3, ["Alphax", "Secrx", "Betax"])
    with news_db.session() as db:
        db.get(Entity, m).sensitivity = "confidential"
        db.commit()
    ex = _expand([a, b], [0.9, 0.8], "Alphax と Betax の関係")
    assert m not in {n["id"] for n in ex["nodes"]} and ex["meet"]["nodes"] == 0
    ids = _chain(6, [f"Slown{i}x" for i in range(6)])
    late = rcache.Budget(5000)
    late.deadline = 0.0      # もう期限切れ
    ex2 = _expand([ids[0], ids[-1]], [0.9, 0.8], "Slown0x と Slown5x の関係", depth=5, budget=late)
    assert ex2["truncated"] and max(n["depth"] for n in ex2["nodes"]) <= 1 and ex2["reason"] in ("time", "")
    with news_db.session() as db:      # 周りが多い種: ハブの抑制で候補が絞られる
        hub = ents(db, "Hubnodex")[0]
        hub2 = ents(db, "Hubnodey")[0]
        for i, other in enumerate(ents(db, *[f"Leafx{i:03d}" for i in range(200)])):
            rel(db, hub if i % 2 else hub2, other)
        db.commit()
    big = _expand([hub, hub2], [0.9, 0.8], "Hubnodex と Hubnodey の関係")
    assert len(big["nodes"]) <= 2 * retrieval.MEET_HUB_K + 2 and len(big["edges"]) <= 2 * retrieval.MEET_HUB_K + 2      # 200 本の周りでも、ハブの抑制（物 1 つにつき MEET_HUB_K 本）で絞られる


def test_expand_endpoint_and_neighbors_depth_five(client):
    """目的: POST /graph/expand が動き（自動・固定の深さ）、深さ 6 は 422。/graph/neighbors は地図のローカルのために深さ 5 まで受ける（6 は 422）。"""
    a, m, b = _chain(3, ["Alphax", "Midx", "Betax"])
    r = client.post("/api/v1/graph/expand", json={"ids": [a, b], "seed_scores": [0.9, 0.8], "q": "Alphax と Betax の関係"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["plan"]["cap"] == 5 and any(n["id"] == m and n["meet"] for n in body["nodes"]) and body["meta"]["elapsed_ms"] >= 0
    assert client.post("/api/v1/graph/expand", json={"ids": [a, b], "depth": 6}, headers=H).status_code == 422
    assert client.post("/api/v1/graph/expand", json={"ids": [a, b], "depth": 3, "meet": False}, headers=H).json()["depth"] == 3
    assert client.post("/api/v1/graph/neighbors", json={"ids": [a], "depth": 5}, headers=H).status_code == 200
    assert client.post("/api/v1/graph/neighbors", json={"ids": [a], "depth": 6}, headers=H).status_code == 422


# ---- ② 橋渡しの問い ----

def test_bridge_questions_have_exact_path_gold(client):
    """目的: 橋渡しの問いは、関係（co_occurs を除く）の最短経路がちょうど L 段の 2 物で、正解が途中の物・経路の関係・根拠の文書。決定的。
    根拠: 通常の問いの正解は 1 段先までで、深さを測れない（測定 3.1）。"""
    rl = lambda s, t, o: {"subject": s, "type": t, "object": o, "quote": f"{s} {t} {o}", "grounded": False}  # noqa: E731
    names = ["Aaaaa", "Bbbbb", "Ccccc", "Ddddd"]
    for x, y in zip(names, names[1:]):
        art(3, [(x, "tech"), (y, "tech")], relations=[rl(x, "uses", y)])
    data = build_questions.build_bridge(news_db.path(), per_length=5, seed=1)
    assert data == {**build_questions.build_bridge(news_db.path(), per_length=5, seed=1), "generated_at": data["generated_at"]}
    qs = data["questions"]
    assert qs and {q["length"] for q in qs} <= {2, 3} and all(q["type"] == "bridge" for q in qs)
    q3 = next(q for q in qs if q["length"] == 3)
    ids = [ent_id(n) for n in names]
    assert sorted(q3["gold"]["entities"]) == sorted(ids) and len(q3["gold"]["relationships"]) == 3 and q3["gold"]["docs"]
    assert {q3["anchor"]["a"], q3["anchor"]["b"]} == {ids[0], ids[-1]} and "つながって" in q3["q"] or "関係" in q3["q"]
    assert all(len(q["gold"]["relationships"]) == q["length"] for q in qs)


def test_runner_meet_path_runs_end_to_end(client):
    """目的: 評価の道 ③' が /graph/expand を通して流れ、点数・時間・深さの情報が出る。根拠: 前後の比較を同じ道具で測るため。"""
    from tools.eval_retrieval import runner

    rl = lambda s, t, o: {"subject": s, "type": t, "object": o, "quote": f"{s} {t} {o}", "grounded": False}  # noqa: E731
    names = ["Aaaaa", "Bbbbb", "Ccccc"]
    for x, y in zip(names, names[1:]):
        art(3, [(x, "tech"), (y, "tech")], relations=[rl(x, "uses", y)])
    qs = build_questions.build_bridge(news_db.path(), per_length=3, seed=1)["questions"]
    rows = runner.run_all(client, news_db.path(), qs, warmup=0)
    assert rows and all("meet" in r["paths"] and r["paths"]["meet"]["info"]["depth"] >= 1 and 0 <= r["paths"]["meet"]["r10"] <= 1 for r in rows)
    fixed = runner.run_all(client, news_db.path(), qs, warmup=0, meet_depth=3)
    assert all(r["paths"]["meet"]["info"]["depth"] == 3 for r in fixed)


# ---- ③ カードの形 ----

def _card_entity():
    """定義・別名・関係・主張（原文一致と不一致）・数値・問い・反対の見方を持つ物。"""
    rl = lambda s, t, o: {"subject": s, "type": t, "object": o, "quote": "", "grounded": False}  # noqa: E731
    for i in range(3):
        art(24 * i + 1, [("Brainx", "model"), ("Partx", "tech")], facts=[{"about": "Brainx", "kind": "benchmark", "text": f"Brainx は R9700 で速い {i}", "value": ""},
                                                                          {"about": "Brainx", "kind": "price", "text": "Brainx の価格（月額）", "value": "20"}],
            relations=[rl("Brainx", "part_of", "Partx")])
    with news_db.session() as db:
        db.add(EntityCard(entity_id=ent_id("Partx"), summary="", definition="Brainx の部品となる技術。", state="official", questions="[]", opposing="[]"))
        db.add(EntityCard(entity_id=ent_id("Brainx"), summary="Brainx は話題。", definition="推論に使うモデル。", state="experimental",
                          questions=json.dumps(["長い文脈で劣化するか？"], ensure_ascii=False), opposing=json.dumps(["別の見方もある"], ensure_ascii=False)))
        db.execute(__import__("sqlalchemy").text("UPDATE evidences SET grounded=1 WHERE id = (SELECT min(id) FROM evidences WHERE claim_id IS NOT NULL)"))
        db.commit()
    return ent_id("Brainx")


def test_context_text_has_fixed_order_and_omits_empty_sections(client):
    """目的: context の text は固定の順（定義 → 種類・状態・確かさ・別名 → 型ごとの関係 → 主張と根拠 → 数値 → 未解決の問い → 反対の見方 → 出典の数と新しさ）で、空の節を出さない。
    値を持つ主張は「数値」にだけ出す（重複させない）。原文に照合できた根拠の主張が先で ◎ が付く。JSON は facts の並びが同じ（grounded 先頭）で、metrics・source_count を持つ。
    根拠: 利用者の指示（カードの形を固定）・評価の置き場の提案 P6（Evergreen・LLM wiki に合わせた 1 枚の形・空の節は出さない）。"""
    bid = _card_entity()
    tx = client.get(f"/api/v1/things/{bid}/context?format=text").text
    order = ["# Brainx", "推論に使うモデル。", "type: model | status: experimental", "## 上位", "## 主張と根拠", "## 数値", "## 未解決の問い", "## 反対の見方", "## 出典 3 件"]
    pos = [tx.index(x) for x in order]
    assert pos == sorted(pos), tx
    assert "## 数値\n- Brainx の価格（月額）: 20" in tx and tx.count("Brainx の価格（月額）") == 1 and "◎ " in tx.split("## 主張と根拠")[1].split("\n")[1]
    js = client.get(f"/api/v1/things/{bid}/context").json()
    assert js["source_count"] == 3 and js["metrics"][0] == {"label": "Brainx の価格（月額）", "value": "20"} and js["facts"][0].get("grounded") is True
    pid = ent_id("Partx")      # 関係・問い・数値が無い物: その節は出ない
    short = client.get(f"/api/v1/things/{pid}/context?format=text").text
    assert "## 数値" not in short and "## 未解決の問い" not in short and "## 反対の見方" not in short and "Brainx の部品となる技術。" in short
    assert things.context_text({"id": 1, "name": "Emptyx", "type": "tech", "status": "unknown", "confidence": 0.0}) == "# Emptyx (#1)\ntype: tech | status: unknown | confidence: 0.0"


def test_card_api_sorts_claims_by_evidence_strength(client):
    """目的: カード API の主張は、強い根拠（原文に照合できたもの）が先。根拠の段階 tier（exact＞paraphrase＞none）があればそれで並べ、無ければ grounded だけ。"""
    from qnapnewshub import kg_view

    assert kg_view.evidence_rank([{"grounded": True}]) == 0 and kg_view.evidence_rank([{"grounded": False}]) == 1 and kg_view.evidence_rank([]) == 2
    assert kg_view.evidence_rank([{"grounded": False, "tier": "paraphrase"}, {"grounded": False, "tier": "none"}]) == 1 and kg_view.evidence_rank([{"grounded": False, "tier": "none"}]) == 2
    bid = _card_entity()
    res = client.get(f"/api/v1/kg/entities/{bid}/card")
    assert res.status_code == 200
    ranks = [kg_view.evidence_rank(c["evidences"]) for c in res.json()["claims"]]
    assert ranks[0] == 0 and ranks == sorted(ranks)


# ---- ④ カードの夜の仕事 ----

def _night():
    cfg = settings.load()
    cfg["llm"]["night_from"] = 0
    settings.save(cfg)


def test_card_job_unavailable_llm_is_not_counted_and_retried_and_diag(client, monkeypatch):
    """目的: 通信・ゲートウェイが使えない失敗（名前解決の失敗など）は tries を数えず次の回にやり直す。最後の結果（理由・回数）が診断に出て、まだ概要の無い物の数・次に走る時刻が分かる。
    返事が検査に落ちた失敗だけ数える（同じ材料で MAX_TRIES 回）。根拠: 評価の置き場の調査 4 章 P7（今日 card 0 回・openrouter.ai の名前を解決できません。LLM の節 0/50）・AGENTS.md（異常を見つける診断）。"""
    _night()
    for i in range(3):
        art(24 * i + 1, [("Zorblax", "model"), ("Qorp", "org")], facts=[{"about": "Zorblax", "kind": "release", "text": f"Zorblax {i} を公開", "value": str(i)}])

    async def down(*a, **k):
        raise llm.LlmUnavailable("OpenRouter に接続できません: ConnectError")

    monkeypatch.setattr("qnapnewshub.llm_task.ask", down)
    out = asyncio.run(kg_job.run_cards())
    assert out["calls"] == 0 and "接続できません" in out["error"] and out["todo"] >= 1 and out["failed"] == 0
    with news_db.session() as db:
        assert db.get(EntityCard, ent_id("Zorblax")) is None      # tries を数えていない（行も作らない）
        d = kg_job.diag(db)
    assert d["last"]["error"].startswith("OpenRouter") and d["pending"] >= 1 and d["window_open"] is True and d["gave_up"] == 0 and d["max_tries"] == kg_job.MAX_TRIES
    assert {t["name"] for t in kg_job.targets()} >= {"Zorblax"}      # 次の回の対象に残る

    async def ok(cfg, purpose, system, prompt, *, max_tokens):
        n = len([ln for ln in prompt.splitlines() if ln.startswith("[")])
        return json.dumps({str(i): {"definition": "Zorblax は Qorp のモデルです。", "summary": "Zorblax は Qorp のモデルとして公開が続いています。", "state": "official", "questions": [], "opposing": []}
                           for i in range(1, n + 1)}, ensure_ascii=False), "m/x"

    monkeypatch.setattr("qnapnewshub.llm_task.ask", ok)
    out2 = asyncio.run(kg_job.run_cards())
    assert out2["error"] == "" and out2["done"] >= 1
    with news_db.session() as db:
        d2 = kg_job.diag(db)
    assert d2["last"]["done"] >= 1 and d2["last"]["error"] == "" and d2["made"] >= 1 and d2["pending"] < d["pending"]


def test_card_job_diag_next_run_and_blocked(client):
    """目的: 窓の外では次に走る時刻が night_from 時になる。窓の中ならいま。下ごしらえが無効なら止まっている理由を出す。診断の問題の一覧にも最後の失敗が出る。"""
    assert kg_job.next_run(datetime(2026, 1, 1, 12, 5), 21) == datetime(2026, 1, 1, 21, 0)
    assert kg_job.next_run(datetime(2026, 1, 1, 22, 5), 21) == datetime(2026, 1, 1, 22, 5)
    cfg = settings.load()
    cfg["llm"]["prep_enabled"] = False
    settings.save(cfg)
    with news_db.session() as db:
        assert kg_job.diag(db)["blocked"] == "下ごしらえが無効"
        db.add(NewsMeta(key=kg_job.LAST_KEY, value=json.dumps({"at": "2026-01-01T22:00:00", "done": 0, "calls": 0, "error": "接続できません", "failed": 0, "todo": 4}, ensure_ascii=False)))
        db.commit()
    res = client.get("/api/v1/diagnostics")
    assert res.status_code == 200
    assert res.json()["llm"]["cards"]["last"]["error"] == "接続できません"
    assert any("知識カード" in p["text"] for p in res.json()["problems"])
