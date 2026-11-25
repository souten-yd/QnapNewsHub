"""目的: v0.8「収集の connector」（日銀・e-Stat・国会会議録・e-Gov 法令・EDINET・DefiLlama・CoinGecko・GDELT・PyPI・npm・OSV）の試験。外へ通信しない（connectors._http・_http_post を模擬）。LLM も呼ばない。
  ①利用条件の記録が全部にあり、新しいものは既定で切・鍵の要るもの（e-Stat・EDINET・CoinGecko）は鍵が無いと入れられず gate も止める（API は 422）・未確認は明示が要る
  ②設定の検査（系列・統計表・院・エコシステム・期間の形）③整形: 数値は数値のまま（欠損は点にしない・単位・時点・条件）・RWA トークンとデジタル証券は別の名前空間
  ④法令は版ごとの文書（段 proposed/enacted/in_force/repealed・施行日が先なら成立のみ）と版どうしの辺 ⑤国会会議録は見出しの行を発言にしない ⑥GDELT は L0・「未確認」の印・429 は失敗でなく注意
  ⑦まとめ書きを通って文書・Metric になり、2 回流しても増えない（同じ識別子は 1 件）⑧鍵は URL の query／ヘッダーで送り、応答・エラーに出ない ⑨OSV は POST。
根拠: 利用者の指示（v0.8・2026-11-25）・ControlDeck の PLAN-integrated-knowledge 8.2（採用の中・低）・AGENTS.md 絶対ルール 7（利用条件を確かめる）。形は 2026-11-25 の実走で確かめた応答に合わせた。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from qnapnewshub import conn_v08, connectors, db as news_db, docstore, net, writer
from qnapnewshub.connector_terms import TERMS
from qnapnewshub.db import Document, DocumentLink, MetricPoint
from tests.test_connectors import H, enable, fake, fresh, run  # noqa: F401  (fresh は autouse の fixture)

UTC = timezone.utc
NEW = ("boj", "estat", "kokkai", "egov_law", "edinet", "defillama", "coingecko", "gdelt", "pypi", "npm", "osv")
KEYED = ("estat", "edinet", "coingecko")


def fake_post(routes: dict, calls: list | None = None):
    async def _post(url, *, payload, headers, accept, max_bytes):
        if calls is not None:
            calls.append({"url": url, "payload": payload, "headers": headers})
        for prefix, (status, head, body) in routes.items():
            if url.startswith(prefix):
                return status, head, json.dumps(body).encode()
        raise AssertionError(f"模擬に無い URL: {url}")

    return _post


def n_docs() -> int:
    with news_db.session() as db:
        return db.scalar(select(func.count()).select_from(Document))


def n_metrics() -> int:
    with news_db.session() as db:
        return db.scalar(select(func.count()).select_from(MetricPoint))


# ---- ① 利用条件・既定・鍵 ----

def test_terms_default_off_and_key_required():
    """全部に利用条件の記録（公式の URL・確かめた日）がある。新しいものは既定で切。鍵の要るものは鍵が無いと入にできず、gate も「鍵が要ります」で止める。鍵があっても未確認なら確認の明示が要る。"""
    for cid in NEW:
        t = TERMS[cid]
        assert t["tos_url"].startswith("https://") and t["checked_at"] == "2026-11-25" and t["status"] in ("confirmed", "partial", "unconfirmed")
        assert connectors.conf(cid)["enabled"] is False
        assert connectors.SPECS[cid].key_required is (cid in KEYED)
    assert TERMS["boj"]["status"] == "confirmed" and TERMS["egov_law"]["status"] == "unconfirmed"
    with pytest.raises(connectors.ConnectorError, match="鍵が要ります"):
        connectors.set_enabled("coingecko", True)
    cfg = connectors.load()
    cfg["connectors"]["coingecko"]["enabled"] = True       # 設定を直接書いて入にしても、鍵が無ければ取らない
    connectors.save(cfg)
    ok, why = connectors.gate("coingecko")
    assert ok is False and "鍵が要ります" in why
    assert run(connectors.run_one("coingecko"))["ran"] is False
    connectors.set_key("coingecko", "CG-" + "k" * 20)
    assert connectors.gate("coingecko") == (True, "")
    connectors.set_enabled("coingecko", False)
    connectors.set_key("estat", "a" * 24)
    with pytest.raises(connectors.ConnectorError, match="未確認"):
        connectors.set_enabled("estat", True)
    connectors.set_enabled("estat", True, terms_ack=True)
    assert connectors.describe("estat")["key_required"] is True and "a" * 24 not in json.dumps(connectors.describe("estat"))


def test_settings_validation():
    """入力は URL・検索語・問いの本文に入るので形を絞る: 系列・統計表 ID・院・エコシステム・期間・件数の範囲外は断る。"""
    bad = {"boj": {"series": ["IR01 MADR1Z"]}, "estat": {"tables": ["abc"]}, "kokkai": {"house": "県議会"}, "egov_law": {"laws": ["../etc"]}, "edinet": {"days": 30},
           "osv": {"packages": ["Debian:curl"]}, "gdelt": {"timespan": "forever"}, "pypi": {"packages": ["a b"]}, "npm": {"packages": ["Bad Name"]}, "coingecko": {"vs": ["USD$"]}}
    for cid, settings in bad.items():
        cfg = copy.deepcopy(connectors.load())      # load は覚えを返すので、写してから書き換える
        cfg["connectors"][cid]["settings"] = settings
        with pytest.raises(connectors.ConnectorError):
            connectors.save(cfg)
    cfg = copy.deepcopy(connectors.load())
    cfg["connectors"]["npm"]["settings"] = {"packages": ["@scope/pkg", "vite"]}
    cfg["connectors"]["osv"]["settings"] = {"packages": ["npm:@scope/pkg", "PyPI:requests"]}
    connectors.save(cfg)


# ---- ③ 整形（数値は数値のまま） ----

def test_boj_metrics_keep_numbers_units_and_dates():
    """日銀: 欠損（null）は点にしない・0 にもしない。単位・系列名・期種が付く。四半期・半期・月・日の時点が日付になる。新しい last_points 点だけ。"""
    data = {"STATUS": 200, "RESULTSET": [
        {"SERIES_CODE": "Q1", "NAME_OF_TIME_SERIES_J": "短観", "UNIT_J": "％ポイント", "FREQUENCY": "QUARTERLY", "VALUES": {"SURVEY_DATES": ["202501", "202502", "202503"], "VALUES": [10, None, "12.5"]}},
        {"SERIES_CODE": "D1", "NAME_OF_TIME_SERIES_J": "金利", "UNIT_J": "年％", "FREQUENCY": "DAILY", "VALUES": {"SURVEY_DATES": ["20251124", "20251125"], "VALUES": [0.5, 0.5]}}]}
    pts = conn_v08.boj_metrics(data, db="CO", last_points=2)
    q = [p for p in pts if p["subject"].endswith(":Q1")]
    assert [(p["at"].month, p["value"]) for p in q] == [(1, 10.0), (7, 12.5)]      # 04 は欠損で点なし
    assert q[0]["unit"] == "％ポイント" and "短観" in q[0]["cond"] and "QUARTERLY" in q[0]["cond"] and q[0]["subject"] == "indicator:boj:CO:Q1"
    assert [p["at"].day for p in pts if p["subject"].endswith(":D1")] == [24, 25]
    assert conn_v08.boj_date("202502", "SEMIANNUAL").month == 7 and conn_v08.boj_date("2025", "ANNUAL").year == 2025 and conn_v08.boj_date("2025ZZ", "MONTHLY") is None


def test_estat_metrics_names_units_and_missing_marks():
    """e-Stat: 値は数値のまま・単位は @unit・時間軸は名前「2020年12月」から・分類は subject（コード）と cond（名前）。欠損の記号（-・…）は点にしない。エラーの STATUS は注意として返す。"""
    data = {"GET_STATS_DATA": {"RESULT": {"STATUS": 0}, "STATISTICAL_DATA": {
        "CLASS_INF": {"CLASS_OBJ": [{"@id": "time", "CLASS": [{"@code": "2020001212", "@name": "2020年12月"}, {"@code": "2021000000", "@name": "2021年"}]},
                                    {"@id": "cat01", "CLASS": {"@code": "001", "@name": "総数"}}]},
        "DATA_INF": {"VALUE": [{"@cat01": "001", "@time": "2020001212", "@unit": "人", "$": "28,547,900"}, {"@cat01": "001", "@time": "2021000000", "@unit": "人", "$": "-"}]}}}}
    pts, why = conn_v08.estat_metrics(data, table="0003410379")
    assert why == "" and len(pts) == 1
    p = pts[0]
    assert p["value"] == 28547900.0 and p["unit"] == "人" and p["at"] == datetime(2020, 12, 1, tzinfo=UTC) and p["subject"] == "stat:estat:0003410379:cat01=001" and p["cond"] == "総数"
    assert conn_v08.estat_metrics({"GET_STATS_DATA": {"RESULT": {"STATUS": 100, "ERROR_MSG": "認証に失敗"}}}, table="1")[1].startswith("e-Stat がエラー")
    assert conn_v08.estat_metrics({}, table="1") == ([], "応答の形が違います")


def test_defillama_rwa_token_and_digital_security_are_separate_kinds():
    """DefiLlama: category が RWA のものは rwa_token: の名前空間（TVL の上位）。デジタル証券は digital_security: で別に持つ（この取得では作らない）。ステーブルコインは通貨の単位つき。"""
    at = datetime(2026, 11, 25, tzinfo=UTC)
    protos = [{"slug": "ondo", "name": "Ondo", "category": "RWA", "tvl": 900.0}, {"slug": "aave", "name": "Aave", "category": "Lending", "tvl": 5000.0},
              {"slug": "small", "name": "S", "category": "RWA", "tvl": 1.0}, {"slug": "bad", "category": "RWA", "tvl": None}]
    pts = conn_v08.llama_protocol_metrics(protos, protocols=["aave"], rwa_top=1, at=at)
    assert {p["subject"] for p in pts} == {"rwa_token:ondo", "protocol:aave"} and all(p["unit"] == "USD" and p["metric"] == "tvl" for p in pts)
    assert "rwa_token" in docstore.SUBJECT_KINDS and "digital_security" in docstore.SUBJECT_KINDS and not any(p["subject"].startswith("digital_security:") for p in pts)
    st = conn_v08.llama_stable_metrics({"peggedAssets": [{"symbol": "USDC", "name": "USD Coin", "circulating": {"peggedUSD": 10}}, {"symbol": "EURC", "circulating": {"peggedEUR": 99}},
                                                         {"symbol": "X", "circulating": {}}]}, 5, at)
    assert [(p["subject"], p["unit"], p["value"]) for p in st] == [("stablecoin:EURC", "EUR", 99.0), ("stablecoin:USDC", "USD", 10.0)]
    assert conn_v08.llama_chain_metrics([{"name": "Ethereum", "tvl": 3.0}, {"name": "Other", "tvl": 1}], ["ethereum"], at)[0]["subject"] == "chain:Ethereum"


def test_coingecko_price_uses_last_updated_time():
    pts = conn_v08.coingecko_metrics({"bitcoin": {"usd": 90000.5, "usd_last_updated_at": 1764000000, "jpy": None}}, ["usd", "jpy"], datetime(2026, 1, 1, tzinfo=UTC))
    assert len(pts) == 1 and pts[0]["value"] == 90000.5 and pts[0]["unit"] == "USD" and pts[0]["metric"] == "price_usd" and pts[0]["at"] == datetime.fromtimestamp(1764000000, UTC)


# ---- ④⑤ 法令・国会会議録 ----

def law_rev(rid: str, status: str, enf: str, **kw) -> dict:
    return {"law_revision_id": rid, "law_title": "テスト法", "law_num": "令和七年法律第一号", "amendment_promulgate_date": "2025-06-04", "amendment_enforcement_date": enf,
            "amendment_law_title": kw.get("amend", ""), "current_revision_status": status, "repeal_status": kw.get("repeal", "None")}


def test_law_stage_and_revision_chain():
    """法令: 版ごとの文書（kind law）。施行日が先なら enacted（成立・未施行）・施行済みは in_force・廃止は repealed。版どうしは supersedes の辺（新しい版 → 古い版）。段は LAW_STAGES の中だけ。"""
    now = datetime(2026, 11, 25, tzinfo=UTC)
    data = {"law_info": {"law_id": "L1", "law_num": "令和七年法律第一号"}, "revisions": [law_rev("L1_20300101", "UnEnforced", "2030-01-01"), law_rev("L1_20250604", "PreviousEnforced", "2025-06-04"),
                                                                                    law_rev("L1_20260401", "CurrentEnforced", "2026-04-01", amend="改正法")]}
    docs, links = conn_v08.law_docs(data, today=now)
    assert [d["meta"]["stage"] for d in docs] == ["in_force", "in_force", "enacted"] and [d["meta"]["current"] for d in docs] == [False, True, False]
    assert all(d["kind"] == "law" and d["meta"]["stage"] in conn_v08.LAW_STAGES and d["ids"]["ref"].startswith("egov:") for d in docs)
    assert [(ln["src"]["version"], ln["dst"]["version"]) for ln in links] == [("L1_20260401", "L1_20250604"), ("L1_20300101", "L1_20260401")]
    assert conn_v08.law_stage(law_rev("x", "Repeal", "2020-01-01"), now) == ("repealed", False)
    assert conn_v08.law_stage({"amendment_enforcement_date": "2031-01-01"}, now)[0] == "enacted"


def test_kokkai_skips_header_row_and_keeps_speaker_house_meeting_date():
    rec = {"speechID": "1X_001", "speaker": "山田太郎", "nameOfHouse": "参議院", "nameOfMeeting": "内閣委員会", "issue": "第3号", "date": "2026-10-28", "speech": "○山田太郎君 " + "あ" * 900,
           "speechURL": "https://kokkai.ndl.go.jp/txt/1X/1", "meetingURL": "https://kokkai.ndl.go.jp/txt/1X"}
    docs = conn_v08.kokkai_docs({"speechRecord": [{**rec, "speechID": "1X_000", "speaker": "会議録情報"}, rec]}, term="人工知能")
    assert len(docs) == 1
    d = docs[0]
    assert d["kind"] == "record" and d["tier"] == 1 and d["meta"]["speaker"] == "山田太郎" and d["meta"]["house"] == "参議院" and d["meta"]["meeting"] == "内閣委員会" and d["meta"]["date"] == "2026-10-28"
    assert len(d["abstract"]) <= 600 and d["published_at"].year == 2026 and d["ai_use"] == "local_only"


def test_edinet_filters_by_sec_code_and_listed_only():
    rows = {"results": [{"docID": "S1", "secCode": "72030", "filerName": "A社", "docDescription": "有価証券報告書", "submitDateTime": "2026-11-25 09:00"},
                        {"docID": "S2", "secCode": None, "filerName": "B 投資事業組合", "docDescription": "届出"}, {"docID": "S3", "secCode": "67580", "filerName": "C社", "docDescription": "臨時報告書"}]}
    assert [d["meta"]["doc_id"] for d in conn_v08.edinet_docs(rows, sec_codes=[], limit=10)] == ["S1", "S3"]
    assert [d["meta"]["doc_id"] for d in conn_v08.edinet_docs(rows, sec_codes=["7203"], limit=10)] == ["S1"]
    assert len(conn_v08.edinet_docs(rows, sec_codes=[], limit=1)) == 1


def test_gdelt_marks_unverified_and_release_advisory_docs():
    g = conn_v08.gdelt_docs({"articles": [{"url": "https://news.example.test/a", "title": "ROCm 7 <b>released</b>", "seendate": "20261125T120000Z", "domain": "news.example.test", "language": "English", "sourcecountry": "US"},
                                          {"url": "javascript:alert(1)", "title": "x"}]}, query="ROCm")
    assert len(g) == 1 and g[0]["tier"] == 0 and g[0]["meta"]["verification"] == "unverified" and "未確認" in g[0]["reason"] and "<b>" not in g[0]["title"]
    py = conn_v08.pypi_docs({"info": {"version": "2.0", "summary": "s"}, "releases": {"1.0": [{"upload_time_iso_8601": "2026-01-01T00:00:00Z"}], "2.0": [{"upload_time_iso_8601": "2026-02-01T00:00:00Z"}],
                                                                                        "1.5": [{"upload_time_iso_8601": "2026-01-15T00:00:00Z", "yanked": True}], "0.1": []}}, "My_Pkg", 2)
    assert [d["version"] for d in py] == ["2.0", "1.5"] and py[0]["meta"]["latest"] and py[1]["meta"]["yanked"] and py[0]["ids"]["ref"] == "pypi:my-pkg@2.0" and py[0]["kind"] == "release"
    npmd = conn_v08.npm_docs({"dist-tags": {"latest": "5.0.0", "stable": "5.0.0", "next": "6.0.0-beta.1"}, "modified": "2026-11-20T00:00:00.000Z"}, "vite")
    assert sorted(d["version"] for d in npmd) == ["5.0.0", "6.0.0-beta.1"] and next(d for d in npmd if d["version"] == "5.0.0")["meta"]["tags"] == ["latest", "stable"]
    osv = conn_v08.osv_docs({"vulns": [{"id": "A-1", "summary": "old", "modified": "2026-01-01T00:00:00Z", "published": "2025-12-01T00:00:00Z"},
                                       {"id": "A-2", "summary": "new", "modified": "2026-05-01T00:00:00Z", "aliases": ["CVE-1"], "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N"}],
                                        "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.2"}]}]}]}]}, ecosystem="PyPI", name="x", per_package=1)
    assert len(osv) == 1 and osv[0]["kind"] == "advisory" and osv[0]["meta"]["fixed"] == ["1.2"] and osv[0]["meta"]["aliases"] == ["CVE-1"] and osv[0]["ids"]["ref"] == "osv:A-2"


# ---- ⑥〜⑨ 実行（模擬の通信）・まとめ書き・鍵 ----

def test_run_through_writer_is_idempotent_and_links(monkeypatch):
    """e-Gov の版は文書と辺になり、2 回流しても増えない（同じ識別子は 1 件）。BOJ の Metric も同じ点は 1 回だけ。"""
    law = {"law_info": {"law_id": "L1"}, "revisions": [law_rev("L1_1", "PreviousEnforced", "2025-06-04"), law_rev("L1_2", "CurrentEnforced", "2026-04-01", amend="改正法")]}
    boj = {"STATUS": 200, "RESULTSET": [{"SERIES_CODE": "S", "NAME_OF_TIME_SERIES_J": "n", "UNIT_J": "u", "FREQUENCY": "MONTHLY", "VALUES": {"SURVEY_DATES": ["202510", "202511"], "VALUES": [1, 2]}}]}
    calls: list = []
    monkeypatch.setattr(connectors, "_http", fake({"https://laws.e-gov.go.jp/api/2/law_revisions/": (200, {}, law), "https://www.stat-search.boj.or.jp/api/v1/getDataCode": (200, {}, boj)}, calls))
    enable("egov_law")
    enable("boj")
    cfg = connectors.load()
    cfg["connectors"]["boj"]["settings"]["series"] = ["CO:S"]
    connectors.save(cfg)
    for _ in range(2):
        assert run(connectors.run_one("egov_law", manual=True))["count"] == 3
        assert run(connectors.run_one("boj", manual=True))["count"] == 2
        writer.flush("t")
    assert n_docs() == 2 and n_metrics() == 2
    with news_db.session() as db:
        assert db.scalar(select(func.count()).select_from(DocumentLink)) == 1
        d = db.scalar(select(Document).where(Document.version == "L1_2"))
        assert d.kind == "law" and json.loads(d.meta)["stage"] == "in_force" and d.connector == "egov_law"
    assert calls[0]["url"].endswith("%E4%BB%A4%E5%92%8C%E4%B8%83%E5%B9%B4%E6%B3%95%E5%BE%8B%E7%AC%AC%E4%BA%94%E5%8D%81%E4%B8%89%E5%8F%B7")      # 法令番号は URL エンコードして path へ


def test_keys_are_sent_but_never_exposed(monkeypatch):
    """鍵: e-Stat は appId・EDINET は Subscription-Key（query）・CoinGecko はヘッダー。鍵の値は describe・結果・エラーに出ない。"""
    secret = "SECRET-" + "z" * 20
    calls: list = []
    monkeypatch.setattr(connectors, "_http", fake({"https://api.e-stat.go.jp/": (200, {}, {"GET_STATS_DATA": {"RESULT": {"STATUS": 100, "ERROR_MSG": "x"}}}),
                                                    "https://api.edinet-fsa.go.jp/": (200, {}, {"results": []}),
                                                    "https://api.coingecko.com/": (200, {}, {"bitcoin": {"usd": 1.0, "usd_last_updated_at": 1764000000}})}, calls))
    for cid in KEYED:
        connectors.set_key(cid, secret)
        enable(cid)
    cfg = connectors.load()
    cfg["connectors"]["estat"]["settings"]["tables"] = ["0003410379"]
    connectors.save(cfg)
    for cid in KEYED:
        res = run(connectors.run_one(cid, manual=True))
        assert secret not in json.dumps(res) and secret not in json.dumps(connectors.describe(cid))
    by = {c["url"].split("/")[2]: c for c in calls}
    assert by["api.e-stat.go.jp"]["params"]["appId"] == secret and by["api.edinet-fsa.go.jp"]["params"]["Subscription-Key"] == secret
    assert by["api.coingecko.com"]["headers"]["x-cg-demo-api-key"] == secret and "appId" not in (by["api.coingecko.com"]["params"] or {})
    assert "e-Stat がエラー" in connectors.state("estat")["last"]["note"] and secret not in json.dumps(connectors.state("estat"))


def test_osv_uses_post_and_gdelt_429_is_a_note_not_a_failure(monkeypatch):
    posts: list = []
    monkeypatch.setattr(connectors, "_http_post", fake_post({"https://api.osv.dev/v1/query": (200, {}, {"vulns": [{"id": "G-1", "summary": "s", "modified": "2026-11-01T00:00:00Z"}]})}, posts))
    enable("osv")
    cfg = connectors.load()
    cfg["connectors"]["osv"]["settings"]["packages"] = ["PyPI:Requests", "npm:@scope/pkg"]
    connectors.save(cfg)
    assert run(connectors.run_one("osv", manual=True))["count"] == 2      # 同じ ID は 1 件にまとまる前の件数（保存で 1 件）
    assert [p["payload"] for p in posts] == [{"package": {"name": "Requests", "ecosystem": "PyPI"}}, {"package": {"name": "@scope/pkg", "ecosystem": "npm"}}]
    writer.flush("t")
    assert n_docs() == 1

    monkeypatch.setattr(connectors, "_http", fake({"https://api.gdeltproject.org/": (429, {}, b"Please limit requests")}))
    enable("gdelt")
    cfg = connectors.load()
    cfg["connectors"]["gdelt"]["settings"]["queries"] = ["a", "b"]
    connectors.save(cfg)
    res = run(connectors.run_one("gdelt", manual=True))
    assert res["error"] == "" and res["count"] == 0 and "429" in res["note"]


def test_api_lists_new_connectors_and_refuses_enable_without_key(admin_client):
    """API: 新しい connector が一覧に出て、鍵の要るものは key_required・has_key false。鍵なしの入は 422、鍵を入れたあと（未確認なら確認も）で入る。"""
    body = admin_client.get("/api/v1/connectors").json()
    rows = {c["id"]: c for c in body["connectors"]}
    assert set(NEW) <= set(rows) and rows["estat"]["key_required"] is True and rows["estat"]["has_key"] is False and rows["boj"]["key_required"] is False and "stats" in body["kinds"]
    r = admin_client.put("/api/v1/connectors/coingecko", json={"enabled": True}, headers=H)
    assert r.status_code == 422 and "鍵が要ります" in r.text
    assert admin_client.put("/api/v1/connectors/coingecko/key", json={"value": "CG-" + "k" * 20}, headers=H).status_code == 200
    assert admin_client.put("/api/v1/connectors/coingecko", json={"enabled": True}, headers=H).json()["enabled"] is True
    assert admin_client.put("/api/v1/connectors/kokkai", json={"settings": {"house": "県議会"}}, headers=H).status_code == 422
    assert admin_client.get("/api/v1/documents", params={"kind": "law"}).status_code == 200
