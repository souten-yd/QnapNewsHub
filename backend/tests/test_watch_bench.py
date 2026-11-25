"""目的: v0.7 の続き（ウォッチリストと「前回から何が変わったか」・条件つきの数値の比較・変化点）の試験。外へ通信しない・LLM は呼ばない（機械的に集める）。
  ①条件の取り出し（規則・原文に無い LLM の条件を捨てる・指定の読み方・合う／違う／不明の判定）②下ごしらえの facts の cond が claims.cond に残る（原文に出る値だけ）
  ③比較: 同じ指標でも条件の違う数値を平均せず別の組に分ける・指定の条件で合う組と別の条件の組を分ける・Metric の時系列も同じ形・機密の物は読めない
  ④変化点: 平らな系列の急な外れを拾い、少ない点・ばらつく系列では言わない・言及の急な増減 ⑤ウォッチリスト: 既定は空・足す／外す・変化の要約は起点より新しい物だけ・読むだけでは「前回見た」が進まない・見たを押すと進む
  ⑥カードの context に変化点が出る。
根拠: 利用者の指示（2026-11-26 v0.7 の続き。LLM が引く検索の精度と密度・「前から何が変わったか」「条件つきの数値の比較」）・ARCHITECTURE 12 章（条件の違うものを平均しない）・AGENTS.md（区分の強制）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from qnapnewshub import bench, changepoint, extract, watch
from qnapnewshub import db as news_db
from qnapnewshub.db import AnaEntity, Claim, Entity, MetricPoint, WatchItem
from tests.test_kg import H, art, ent_id

UTC = timezone.utc


def fact(about: str, text_: str, value: str, kind: str = "benchmark", **cond) -> dict:
    return {"about": about, "kind": kind, "text": text_, "value": value, "quote": "", "tier": "none", **({"cond": cond} if cond else {})}


# ---- ① 条件 ----

def test_parse_cond_and_clean_cond_drop_unwritten_conditions():
    """目的: 文から条件を規則で取れる（量子化・GPU・エンジン・文脈の長さ・版）。LLM が書いた条件は原文に出る値だけ残す（書かれていない条件を作らせない）。不明・空は捨てる。"""
    c = bench.parse_cond("RTX 5090 上の llama.cpp で Q4_K_M・32k ctx のデコードが 120 tok/s")
    assert c == {"quant": "Q4_K_M", "hw": "RTX 5090", "engine": "llama.cpp", "ctx": "32K"}
    assert bench.parse_cond("数字だけの文 42") == {}
    src = "Qwen3 on RX 9700 with vLLM reaches 80 tok/s"
    assert bench.clean_cond({"hw": "RX 9700", "engine": "vLLM", "quant": "FP8", "ctx": "不明", "x": "y"}, src) == {"hw": "RX 9700", "engine": "vLLM"}      # FP8 は原文に無い
    assert bench.clean_cond("not a dict", src) == {} and bench.clean_cond({"hw": "RX 9700"}) == {"hw": "RX 9700"}      # 原文が渡されなければ検査しない


def test_filter_status_and_value_parsing():
    """目的: cond の指定（hw:R9700,quant:Q4_K_M）を読み、合う・違う・不明に分ける。値は数と単位に分ける（tokens/s・％・倍は寄せる）。"""
    f = bench.parse_filter("hw:R9700, quant=Q4_K_M, 知らない:x, 裸の語")
    assert f == {"hw": "R9700", "quant": "Q4_K_M", "*": "裸の語"}
    f = {"hw": "R9700", "quant": "Q4_K_M"}
    assert bench.cond_status(f, {"hw": "AMD Radeon R9700", "quant": "q4_k_m"}) == "matching"
    assert bench.cond_status(f, {"hw": "RTX 5090", "quant": "Q4_K_M"}) == "other"
    assert bench.cond_status(f, {"hw": "R9700"}) == "unspecified" and bench.cond_status({}, {}) == "matching"
    assert bench.parse_value("383.7", "383.7 tokens/s に到達") == (383.7, "tok/s") and bench.parse_value("91%", "") == (91.0, "%")
    assert bench.parse_value("5x", "")[1] == "倍" and bench.parse_value("なし", "") == (None, "")


def test_extract_clean_keeps_only_grounded_conditions():
    """目的: extract.clean は値のある fact の cond だけ、原文（題・抜粋）に出る値で残す。値の無い fact・原文に無い条件は cond が付かない。
    プロンプトは cond を案内しない（2026-11-26: 案内しても本物の LLM は 30 件で 1 度も書かなかった。トークンの無駄を戻さないため）。"""
    src = "Foo on RX 9700 hits 80 tok/s with llama.cpp"
    got = extract.clean({"entities": [{"name": "Foo", "type": "model"}], "facts": [
        {"about": "Foo", "kind": "benchmark", "text": "80 tok/s", "value": "80", "cond": {"hw": "RX 9700", "engine": "llama.cpp", "quant": "Q8_0"}},
        {"about": "Foo", "kind": "release", "text": "出た", "cond": {"hw": "RX 9700"}}]}, src)
    assert got["facts"][0]["cond"] == {"hw": "RX 9700", "engine": "llama.cpp"} and "cond" not in got["facts"][1]
    from qnapnewshub import prep

    assert '"cond"' not in prep.build_prompt([{"title": "T", "source": "S", "summary": "x", "id": 1}]) and extract.PROMPT_VERSION == "ex4"


# ---- ③ 比較 ----

def _bench_world():
    art(30, [("Foo", "model")], facts=[fact("Foo", "RTX 5090 で 120 tok/s を達成する", "120", hw="RTX 5090", quant="Q4_K_M"),
                                       fact("Foo", "R9700 で 60 tok/s を達成する", "60", hw="R9700", quant="Q4_K_M"),
                                       fact("Foo", "R9700 で FP16 は 30 tok/s", "30", hw="R9700", quant="FP16"),
                                       fact("Foo", "速度は 50 tok/s", "50")])
    art(5, [("Foo", "model")], facts=[fact("Foo", "R9700 で更新後 75 tok/s", "75", hw="R9700", quant="Q4_K_M")])


def test_claim_cond_is_stored_and_compare_never_averages(client):
    """目的: facts の cond が claims.cond に残り、compare は（単位・条件）の組に分ける。条件の違う数値（5090 と R9700・Q4 と FP16）は別の組で、平均を返さない。同じ条件の組は新しい順の点・最小・最大・最新。
    指定の条件に合う組・別の条件の組・条件不明の組を分け、text 形式でも別の節になる。"""
    _bench_world()
    with news_db.session() as db:
        assert db.scalar(select(Claim.cond).where(Claim.text.like("RTX%"))).startswith("{")
    r = client.get("/api/v1/metrics/compare", params={"thing": "Foo", "metric": "tok/s", "cond": "hw:R9700,quant:Q4_K_M"}).json()
    groups = r["things"][0]["groups"]
    assert len(groups) == 4 and "mean" not in str(groups) and "avg" not in str(groups)
    match = [g for g in groups if g["status"] == "matching"]
    assert len(match) == 1 and match[0]["n"] == 2 and match[0]["latest"]["value"] == 75.0 and (match[0]["min"], match[0]["max"]) == (60.0, 75.0)
    assert sorted(g["status"] for g in groups) == ["matching", "other", "other", "unspecified"]
    txt = client.get("/api/v1/metrics/compare", params={"thing": str(ent_id("Foo")), "metric": "tok/s", "cond": "hw:R9700,quant:Q4_K_M", "format": "text"}).text
    assert "## 指定の条件に合う" in txt and "## 別の条件（比べられない）" in txt and "75tok/s" in txt and "条件の違う数値は平均" in txt
    assert client.get("/api/v1/metrics/compare", params={"thing": "Nope"}).status_code == 404


def test_compare_includes_metric_points_and_hides_confidential(client):
    """目的: Metric の時系列（connector の点）も同じ形で出る（条件は点の cond の文）。機密の物は public の読み手に 404（無い物と区別しない）。"""
    art(2, [("Bar", "software")])
    now = datetime.now(UTC).replace(tzinfo=None)
    with news_db.session() as db:
        db.add_all([MetricPoint(subject="repo:acme/Bar", metric="stars", value=10 + i, unit="count", at=now - timedelta(days=3 - i), source="gh", cond="daily") for i in range(3)])
        db.execute(text("UPDATE entities SET sensitivity='confidential' WHERE key='secretthing'"))
        db.add(Entity(key="secretthing", name="SecretThing", type="tech", sensitivity="confidential"))
        db.commit()
    g = client.get("/api/v1/metrics/compare", params={"thing": "Bar", "metric": "stars"}).json()["things"][0]["groups"]
    assert len(g) == 1 and g[0]["n"] == 3 and g[0]["latest"]["value"] == 12.0
    assert client.get("/api/v1/metrics/compare", params={"thing": "SecretThing"}).status_code == 404
    assert client.get("/api/v1/metrics/compare", params={"thing": "SecretThing", "confidential": "true"}).status_code == 200


# ---- ④ 変化点 ----

def test_series_change_flags_jumps_only_with_enough_stable_points():
    """目的: 平らな系列の急な外れは拾う。点が少ない・ばらつきの中の動き・小さな動きは言わない（誤検出を避ける）。"""
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    flat = [(t0 + timedelta(days=i), 100.0) for i in range(8)] + [(t0 + timedelta(days=8), 180.0)]
    ch = changepoint.series_change(flat)
    assert len(ch) == 1 and ch[0]["direction"] == "up" and ch[0]["pct"] == 80.0
    assert changepoint.series_change(flat[:3] + [(t0 + timedelta(days=9), 500.0)]) == []      # 直前が 4 点未満
    noisy = [(t0 + timedelta(days=i), v) for i, v in enumerate([100, 140, 90, 130, 95, 135, 100, 125, 140])]
    assert changepoint.series_change(noisy) == []
    small = [(t0 + timedelta(days=i), 100.0) for i in range(8)] + [(t0 + timedelta(days=8), 105.0)]
    assert changepoint.series_change(small) == []


def test_mention_surge_up_and_down(client):
    """目的: 言及の数（ana_entity の日ごと）が直近 3 日で前の 4 週の 3 倍以上なら増、逆に 1/3 以下なら減。少ない件数では言わない。"""
    art(1, [("Hot", "tech"), ("Cold", "tech")])
    from qnapnewshub import analytics

    today = analytics._now_local().date()  # noqa: SLF001
    with news_db.session() as db:
        hot, cold = ent_id("Hot"), ent_id("Cold")
        db.execute(text("DELETE FROM ana_entity"))      # 保存で入った集計は消して、試験の数だけにする
        rows = [AnaEntity(grain="d", t=(today - timedelta(days=d)).isoformat(), entity_id=hot, n=1 if d > 2 else 4, indep=1) for d in range(0, 20)]
        rows += [AnaEntity(grain="d", t=(today - timedelta(days=d)).isoformat(), entity_id=cold, n=0 if d < 3 else 3, indep=1) for d in range(0, 20)]
        db.add_all(rows)
        db.commit()
        up, down = changepoint.mention_surge(db, hot), changepoint.mention_surge(db, cold)
        assert up and up["direction"] == "up" and up["recent"] == 12 and "急に増えた" in changepoint.line({"kind": "mentions", **up})
        assert down and down["direction"] == "down" and "急に減った" in changepoint.line({"kind": "mentions", **down})
        assert changepoint.mention_surge(db, 99999) is None


# ---- ⑤ ウォッチリスト ----

def _mark(client, ids=None):
    return client.post("/api/v1/watch/seen", json={"entity_ids": ids}, headers=H)


def test_watch_default_empty_and_changes_since_last_seen(client):
    """目的: 既定は空。足した物の変化の要約は「前回見た」より新しい主張・関係だけ。要約を読むだけでは起点が進まず（何度引いても同じ）、「見た」で進んで空になる。
    since の指定（7d・日付）で上書きでき、読めない形は 422。thing の指定なら監視でなくても引ける。text 形式は出どころの印と条件を含む。"""
    assert client.get("/api/v1/watch").json() == {"items": []}
    assert client.get("/api/v1/watch/changes").json()["items"] == []
    assert "監視の物がありません" in client.get("/api/v1/watch/changes", params={"format": "text"}).text
    art(30, [("Zed", "model"), ("Ys", "org")], facts=[fact("Zed", "古い主張", "", kind="claim")])
    zid = ent_id("Zed")
    r = client.post("/api/v1/watch", json={"entity_id": zid}, headers=H)
    assert r.status_code == 200 and client.post("/api/v1/watch", json={"entity_id": zid}, headers=H).json()["id"] == r.json()["id"]      # 同じ物は 1 つ
    assert client.post("/api/v1/watch", json={"entity_id": 99999}, headers=H).status_code == 404
    with news_db.session() as db:      # 2 時間前から監視していた形にする
        db.execute(text("UPDATE watch_items SET seen_at = datetime('now', '-2 hours')"))
        db.commit()
    first = client.get("/api/v1/watch/changes").json()
    assert first["items"][0]["quiet"] is True and first["llm"] is False      # 足した時点より前の主張は変化ではない
    art(1, [("Zed", "model"), ("Ys", "org")], facts=[fact("Zed", "新しい版で 90 tok/s", "90", hw="R9700")],
        relations=[{"subject": "Ys", "type": "develops", "object": "Zed", "quote": "", "grounded": False}])
    ch = client.get("/api/v1/watch/changes").json()["items"][0]
    assert not ch["quiet"] and [c["value"] for c in ch["claims"]] == ["90"] and ch["claims"][0]["cond"] == {"hw": "R9700"} and ch["relations"][0]["type"] == "develops"
    assert client.get("/api/v1/watch/changes").json()["items"][0]["claims"] == ch["claims"]      # 読むだけでは進まない
    txt = client.get("/api/v1/watch/changes", params={"format": "text"}).text
    assert "新しい主張" in txt and "GPU・端末 R9700" in txt and "src " in txt or "src" in txt
    assert _mark(client).json() == {"marked": 1}
    assert client.get("/api/v1/watch/changes").json()["items"][0]["quiet"] is True
    old = client.get("/api/v1/watch/changes", params={"since": "60d"}).json()["items"][0]
    assert len(old["claims"]) == 2
    assert client.get("/api/v1/watch/changes", params={"since": "xx"}).status_code == 422
    adhoc = client.get("/api/v1/watch/changes", params={"thing": "Ys", "since": "7d"}).json()
    assert adhoc["watched"] is False and adhoc["items"][0]["thing"]["name"] == "Ys" and adhoc["items"][0]["relations"]
    assert client.delete(f"/api/v1/watch/{zid}", headers=H).json() == {"removed": True} and client.get("/api/v1/watch").json() == {"items": []}


def test_watch_changes_report_metric_delta_versions_events_and_hide_confidential(client):
    """目的: 数値の変化（前の値との差）・版の更新（release の文書）を集める。機密の主張・物は public の要約に出ない。監視の物が消えても（物の削除）表は壊れない。"""
    art(40, [("Qux", "software")])
    qid = ent_id("Qux")
    now = datetime.now(UTC)
    with news_db.session() as db:
        from qnapnewshub.db import Document

        db.add_all([MetricPoint(subject="repo:o/Qux", metric="stars", value=100, unit="count", at=(now - timedelta(days=10)).replace(tzinfo=None), source="gh", cond="daily"),
                    MetricPoint(subject="repo:o/Qux", metric="stars", value=160, unit="count", at=(now - timedelta(days=1)).replace(tzinfo=None), source="gh", cond="daily"),
                    Document(kind="release", ident="repo:o/qux@v2", title="Qux v2.0 release", url="https://x.test/r", source="gh", published_at=now - timedelta(days=1), version="v2.0", sensitivity="public", ai_use="allowed"),
                    Document(kind="release", ident="repo:o/qux@v3", title="Qux v3.0 secret", source="gh", published_at=now - timedelta(days=1), version="v3.0", sensitivity="confidential")])
        db.commit()
    it = client.get("/api/v1/watch/changes", params={"thing": str(qid), "since": "7d"}).json()["items"][0]
    assert it["metrics"][0]["previous"] == 100.0 and it["metrics"][0]["pct"] == 60.0
    assert [v["version"] for v in it["versions"]] == ["v2.0"]      # 機密の版は出ない
    assert "Qux v2.0" in client.get("/api/v1/watch/changes", params={"thing": str(qid), "since": "7d", "format": "text"}).text


def test_context_shows_change_points(client):
    """目的: カードの context（LLM 用）に変化点が出る（無ければ出ない）。"""
    art(1, [("Spike", "tech")])
    sid = ent_id("Spike")
    assert "change_points" not in client.get(f"/api/v1/things/{sid}/context").json()
    now = datetime.now(UTC).replace(tzinfo=None)
    with news_db.session() as db:
        db.add_all([MetricPoint(subject="asset:Spike", metric="price_usd", value=1.0, unit="USD", at=now - timedelta(days=9 - i), source="cg", cond="現在値") for i in range(8)]
                   + [MetricPoint(subject="asset:Spike", metric="price_usd", value=2.0, unit="USD", at=now - timedelta(hours=2), source="cg", cond="現在値")])
        db.commit()
    c = client.get(f"/api/v1/things/{sid}/context").json()
    assert "上がった" in c["change_points"][0]
    assert "## 変化点" in client.get(f"/api/v1/things/{sid}/context", params={"format": "text"}).text
