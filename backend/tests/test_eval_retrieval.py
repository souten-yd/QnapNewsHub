"""目的: 検索の評価の物差し（tools/eval_retrieval）の点数の計算と、問いの集めの正解の付け方が正しいことの試験。外へ通信しない・LLM は呼ばない・実データは使わない（小さな模擬だけ）。
  ①Recall@k・MRR・nDCG・密度・百分位・トークンの見積り・score_one を、手で数えた値と突き合わせる ②RRF と交互の並べ方 ③模擬のハブから作った問いの正解が、DB の構造どおり（主張・関係・イベント・日本語の主張）
  ④5 つの道を ASGI で通しで流せる（点数が 0〜1・時間が入る・正解の根拠を直接渡せば満点になる）。
根拠: 利用者の指示（2026-11-25「検索の評価の物差し。LLM が引く検索の結果の精度と密度を数字で比べる」）。以後の検索の変更はこの点数で良し悪しを決めるので、物差しそのものの狂いを試験で止める。
"""
from __future__ import annotations

import json
import math

import pytest

from qnapnewshub import db as news_db
from tests.test_kg import art, ent_id
from tools.eval_retrieval import build_questions, metrics, report, runner


# ---- ① 点数の計算 ----

def test_recall_mrr_ndcg_by_hand():
    """目的: 手で数えた値と一致する。正解 {a,b,c}・並び [x,a,y,b,z]: R@2=1/3・R@4=2/3・MRR=1/2・nDCG@5=(1/log2(3)+1/log2(5))/(1+1/log2(3)+1/log2(4))。重複は 1 回だけ数える。"""
    ranked, gold = ["x", "a", "y", "b", "z"], {"a", "b", "c"}
    assert metrics.recall_at(ranked, gold, 2) == pytest.approx(1 / 3)
    assert metrics.recall_at(ranked, gold, 4) == pytest.approx(2 / 3)
    assert metrics.mrr(ranked, gold) == 0.5
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert metrics.ndcg_at(ranked, gold, 5) == pytest.approx((1 / math.log2(3) + 1 / math.log2(5)) / ideal)
    assert metrics.recall_at(["a", "a", "a", "b"], gold, 2) == pytest.approx(2 / 3)      # 重複は数えない（a の次が b）
    assert metrics.mrr(["x"], gold) == 0 and metrics.recall_at([], gold, 5) == 0 and metrics.recall_at(["a"], set(), 5) == 0
    assert metrics.ndcg_at(["a", "b", "c"], gold, 10) == pytest.approx(1.0)      # 理想の並びは満点


def test_density_percentile_tokens():
    """目的: 密度＝正解の数 ÷ トークン × 1000。百分位は最近傍順位法（P50 は 4 件で 2 番目・P95 は最大）。トークンの見積りは日本語のほうが 1 文字あたり重く、空は 0。"""
    assert metrics.density(3, 1500) == pytest.approx(2.0) and metrics.density(3, 0) == 0
    xs = [10.0, 20.0, 30.0, 40.0]
    assert metrics.percentile(xs, 0.5) == 20.0 and metrics.percentile(xs, 0.95) == 40.0 and metrics.percentile([], 0.5) == 0
    assert metrics.est_tokens("") == 0 and metrics.est_tokens("a" * 35) == 10 and metrics.est_tokens("あ" * 13) == 10


def test_score_one_context_and_kind_recall():
    """目的: 文脈に入った参照（context_refs）の数が密度の分子になる（無ければ上位 10 件）。種類ごとの R@20 は、正解にその種類が無ければ None（0 と区別する）。"""
    gold = {"doc:1", "doc:2", "ent:7"}
    s = metrics.score_one(["doc:1", "ent:7", "doc:9"], gold, None, 500)
    assert s["ctx_hits"] == 2 and s["ctx_density"] == pytest.approx(4.0) and s["ent_r20"] == 1.0 and s["clm_r20"] is None and s["rel_r20"] is None
    assert s["doc_r10"] == pytest.approx(0.5)      # 文書だけの正解 {1,2} のうち 1 が出た
    s2 = metrics.score_one(["doc:9"], gold, {"doc:1", "doc:2", "doc:3"}, 100)
    assert s2["ctx_hits"] == 2 and s2["ctx_cover"] == pytest.approx(2 / 3) and s2["r10"] == 0


def test_rrf_and_interleave():
    """目的: 両方の並びで上位の参照が先になる。片方にしか無い参照も残る。交互の並べ方は 1 件ずつ・重複なし。"""
    assert runner.rrf(["a", "b", "c"], ["b", "d"])[0] == "b"
    assert set(runner.rrf(["a"], ["d"])) == {"a", "d"}
    assert runner.interleave(["d1", "d2", "d3"], ["e1"], ["v1", "v2"]) == ["d1", "e1", "v1", "d2", "v2", "d3"]


# ---- ③ 問いの集め ----

def _corpus():
    """名前が 3 文字以上で重ならない物と、主張・関係・イベントの材料を持つ模擬のハブ。"""
    fx = lambda about, kind, text: {"about": about, "kind": kind, "text": text, "value": ""}  # noqa: E731
    rel = lambda s, t, o: {"subject": s, "type": t, "object": o, "quote": f"{s} {t} {o}", "grounded": False}  # noqa: E731
    a1 = art(30, [("Alphaware", "software")], facts=[fx("Alphaware", "release", "新版を公開した")])
    a2 = art(1, [("Alphaware", "software")], facts=[fx("Alphaware", "release", "対応を広げた")])
    c1 = art(5, [("Coolchip", "product"), ("Dullchip", "product")], relations=[rel("Coolchip", "competes", "Dullchip")])
    r1 = art(6, [("Basekit", "tech"), ("Eagleapp", "software"), ("Fooapp", "software")], relations=[rel("Eagleapp", "uses", "Basekit"), rel("Fooapp", "uses", "Basekit")])
    en = art(7, [("Gammalib", "tech")], facts=[fx("Gammalib", "release", "ライブラリが安定版になった")], title="Gammalib reaches stable release in english")
    return a1, a2, c1, r1, en


def _db_path():
    return news_db.path()


def test_questions_gold_follows_db_structure(client):
    """目的: 問いの正解が DB の構造と一致する。事実＝その物の主張・根拠の文書・物／比較＝関係・両方の物／関係＝subject の物すべて／変化＝日をまたぐ主張／日本語の問い＝題が英語の記事。
    LLM を使わず、同じ DB・同じ seed なら同じ問い。"""
    _corpus()
    data = build_questions.build(_db_path(), per_type=5, seed=1)
    by: dict[str, list[dict]] = {}
    for q in data["questions"]:
        by.setdefault(q["type"], []).append(q)
    assert data == {**build_questions.build(_db_path(), per_type=5, seed=1), "generated_at": data["generated_at"]}      # 決定的
    alpha = ent_id("Alphaware")
    fact = next(q for q in by["fact"] if "Alphaware" in q["q"])
    assert alpha in fact["gold"]["entities"] and len(fact["gold"]["claims"]) == 2 and len(fact["gold"]["docs"]) == 2
    cmp_ = by["compare"][0]
    assert set(cmp_["gold"]["entities"]) == {ent_id("Coolchip"), ent_id("Dullchip")} and len(cmp_["gold"]["relationships"]) == 1 and cmp_["gold"]["docs"]
    rel = by["relation"][0]
    assert "Basekit" in rel["q"] and set(rel["gold"]["entities"]) == {ent_id("Basekit"), ent_id("Eagleapp"), ent_id("Fooapp")} and len(rel["gold"]["relationships"]) == 2
    chg = next(q for q in by["change"] if "Alphaware" in q["q"])      # 30 時間前と 1 時間前＝日をまたぐ
    assert len(chg["gold"]["claims"]) == 2 and len(chg["gold"]["docs"]) == 2
    xl = by["crosslingual"]
    assert any(q["gold"]["entities"] == [ent_id("Gammalib")] for q in xl) and len(xl) >= 2      # 題が英語の記事の日本語の主張
    assert any(q["anchor"]["named"] for q in xl) and any(not q["anchor"]["named"] for q in xl)
    for q in xl:      # 名前つきは問いに物の名前が入り、名前なしは入らない（日本語の文だけで引く）
        name = "Gammalib" if q["gold"]["entities"] == [ent_id("Gammalib")] else "Alphaware"
        assert (name in q["q"]) == q["anchor"]["named"]
    assert all(q["id"].startswith("q") for q in data["questions"]) and len({q["id"] for q in data["questions"]}) == len(data["questions"])
    assert all(q["gold"]["docs"] or q["gold"]["entities"] for q in data["questions"])      # 正解が無い問いは作らない


def test_questions_exclude_confidential_and_short_names(client):
    """目的: 区分が public でない物・2 文字以下の名前の物は問いにしない（区分を守る・曖昧な名前を避ける）。"""
    fx = lambda about: {"about": about, "kind": "release", "text": "公開した", "value": ""}  # noqa: E731
    art(1, [("Qq", "tech")], facts=[fx("Qq")])
    art(1, [("Secretware", "tech")], facts=[fx("Secretware")])
    with news_db.session() as db:
        db.connection().exec_driver_sql("UPDATE entities SET sensitivity='confidential' WHERE name='Secretware'")
        db.commit()
    qs = build_questions.build(_db_path(), per_type=5)["questions"]
    assert not any("Qq" in q["q"] or "Secretware" in q["q"] for q in qs)


# ---- ④ 通しで流す ----

def test_runner_end_to_end_on_mock(client):
    """目的: 5 つの道を ASGI で通しで流せる。点数は 0〜1・時間が入る・どの道も参照の並びを返す。物の名前を問いにした事実の問いでは、種が正解の物を拾い、文脈の道が主張まで届く。
    レポートの表が作れる（全体・種類ごと）。"""
    _corpus()
    questions = build_questions.build(_db_path(), per_type=5)["questions"]
    assert questions
    rows = runner.run_all(client, _db_path(), questions, warmup=1)
    assert len(rows) == len(questions)
    for r in rows:
        assert set(r["paths"]) == set(runner.PATHS)
        for p in r["paths"].values():
            assert 0 <= p["r10"] <= 1 and 0 <= p["ndcg10"] <= 1 and 0 <= p["mrr"] <= 1 and p["ms"] >= 0 and p["ctx_tokens"] >= 0
    fact = next(r for r in rows if r["type"] == "fact" and "Alphaware" in r["q"])
    assert fact["paths"]["seeds"]["ent_r20"] == 1.0
    assert fact["paths"]["context"]["clm_r20"] == 1.0      # 物の文脈の facts から主張の参照が取れる
    md = report.render(rows, {"db": "模擬"})
    assert "① 全文だけ" in md and "文脈の密度" in md and "R@10" in md
    agg = report.aggregate(rows)
    assert set(agg) == set(runner.PATHS) and agg["seeds"]["p95"] >= agg["seeds"]["p50"]


def test_gold_refs_given_directly_scores_perfect():
    """目的: 正解の根拠そのものが並びに入っていれば満点（物差しの上限の確認）。種類ごとの参照の文字列の形が `doc:` `ent:` `clm:` `rel:` `evt:`。"""
    q = {"gold": {"docs": [1, 2], "entities": [3], "claims": [4], "relationships": [5], "events": [6]}}
    g = runner.gold_refs(q)
    assert g == {"doc:1", "doc:2", "ent:3", "clm:4", "rel:5", "evt:6"}
    s = metrics.score_one(sorted(g), g, None, 100)
    assert s["r10"] == s["mrr"] == s["ndcg10"] == 1.0 and s["r5"] == pytest.approx(5 / 6)      # 6 件の正解のうち上位 5 件
