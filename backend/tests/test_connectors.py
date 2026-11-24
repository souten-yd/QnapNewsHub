"""目的: v0.7 前半「収集の connector の枠組み」の試験。外へ通信しない（connectors._http・bodies._http を模擬）。LLM も呼ばない。
  ①利用条件の記録が全 connector にあり、未確認の新しい connector は既定で切・入れるには明示が要る（既存の取得を載せ替えたものは入のまま）
  ②予算（1 日の件数・通信量）を超えたら止まり、診断に出て、日が変わると戻る ③既存の取得（arXiv など）も入切・予算の下にある
  ④保存の段 L0〜L4: 既存のニュースの文書は L1〜L3 に当てはまり（移行は冪等）、昇格は上げるだけで理由と時刻が残る ⑤昇格の規則（チャンネル・ホットトピック・監視の物・スター急増・派生版）
  ⑥論文の同定: arXiv ID・DOI・OpenAlex ID・Semantic Scholar ID が同じ論文なら 1 件（arXiv の DOI は arXiv ID）⑦HF の派生版の一致と量子化の種類 ⑧GitHub の検索の整形・スターの日ごとの差（Metric）
  ⑨Sitemap は robots.txt の Disallow を守る・DTD を断る ⑩変化の検出（ETag・hash・初回は基準だけ）⑪L0 の文書も検索できる ⑫API の権限（管理者だけ・鍵の値を返さない）・まとめ書きを通る・お休みモードで止まる。
根拠: 利用者の指示（v0.7 前半・2026-11-25）・ControlDeck の PLAN-integrated-knowledge 8 章（connector ごとに頻度・件数・通信量・鍵・利用条件を持つ／L0〜L4と昇格／利用条件を確かめて記録）・AGENTS.md 絶対ルール 7。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from qnapnewshub import bodies, conn_impl, connectors, db as news_db, docstore, net, probe, rest, service, sources, writer
from qnapnewshub.connector_terms import TERMS
from qnapnewshub.db import Document, DocumentAlias, DocumentLink, DocumentPromotion, MetricPoint
from tests.test_kg import art

H = {"X-Requested-With": "NewsHub"}
UTC = timezone.utc


@pytest.fixture(autouse=True)
def fresh(isolated_data, monkeypatch):
    connectors.reset_memory()
    bodies._robots.clear()  # noqa: SLF001
    bodies._deferred.clear()  # noqa: SLF001

    async def nosleep(_s):
        return None

    monkeypatch.setattr(connectors, "_sleep", nosleep)
    cfg = connectors.load()      # 既定のチャンネルの語に偶然合って昇格しないよう、語による規則は切（規則そのものの試験は apply_rules を直接呼ぶ）
    cfg["promotion"].update(channel=False, hot=False, watch=False)
    connectors.save(cfg)
    yield
    connectors.reset_memory()


def fake(routes: dict, calls: list | None = None):
    """connectors._http の模擬。routes: {URL の前方一致: (status, headers, body か JSON の値)}。"""
    async def _http(url, *, params, headers, accept, max_bytes):
        if calls is not None:
            calls.append({"url": url, "params": params, "headers": headers})
        for prefix, (status, head, body) in routes.items():
            if url.startswith(prefix):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return status, head, raw
        raise AssertionError(f"模擬に無い URL: {url}")

    return _http


def run(coro):
    return asyncio.run(coro)


def enable(cid: str, **row):
    cfg = connectors.load()
    cfg["connectors"][cid].update(enabled=True, terms_ack=True, **row)
    connectors.save(cfg)


def docs_in_db() -> list[Document]:
    with news_db.session() as db:
        return list(db.scalars(select(Document).order_by(Document.id)))


def gh_row(name: str, stars: int, **kw) -> dict:
    return {"full_name": name, "html_url": f"https://github.com/{name}", "description": kw.get("desc", "an llm inference engine"), "stargazers_count": stars, "language": "C++",
            "topics": ["llm"], "created_at": "2026-11-20T00:00:00Z", "pushed_at": "2026-11-24T00:00:00Z", "id": 1, "license": {"spdx_id": "MIT"}}


# ---- ① 利用条件・既定 ----

def test_every_connector_has_terms_and_new_ones_default_off():
    """利用条件の記録が全 connector にあり（確かめた日・公式の URL）、新しい connector は既定で切、既存の載せ替えは入。"""
    for cid, spec in connectors.SPECS.items():
        t = TERMS[cid]
        assert t["checked_at"] == "2026-11-25" and t["tos_url"].startswith("https://") and t["status"] in ("confirmed", "partial", "per_site", "unconfirmed")
        assert connectors.conf(cid)["enabled"] is spec.adapted
    assert TERMS["hn_search"]["status"] == "unconfirmed" and connectors.needs_ack("hn_search")
    assert not connectors.needs_ack("hn")      # 既存の取得は今までの動きを変えない


def test_unconfirmed_connector_cannot_be_enabled_without_ack():
    """利用条件が未確認の connector は、確認の明示なしに入にできない（API は 422）。明示すれば入る。"""
    with pytest.raises(connectors.ConnectorError):
        connectors.set_enabled("hn_search", True)
    assert connectors.gate("hn_search")[0] is False
    connectors.set_enabled("hn_search", True, terms_ack=True)
    assert connectors.gate("hn_search") == (True, "")


def test_connector_settings_are_validated():
    """設定の検査: 知らない項目・形の違う値・http の URL・段の範囲外は断る（入力は URL・検索語に入るため）。"""
    cfg = connectors.load()
    cfg["connectors"]["github_discover"]["settings"] = {"topics": ["llm; drop"]}
    with pytest.raises(connectors.ConnectorError):
        connectors.save(cfg)
    for cid, bad in (("sitemap", {"sites": [{"name": "x", "url": "http://a.test/s.xml"}]}), ("semanticscholar", {"depth": 3}), ("hf_models", {"zzz": 1}), ("openalex", {"filter": "a b"})):
        cfg = connectors.load()
        cfg["connectors"][cid]["settings"] = bad
        with pytest.raises(connectors.ConnectorError):
            connectors.save(cfg)


# ---- ② 予算 ----

def test_budget_stops_and_shows_in_diagnostics(monkeypatch):
    """1 日の件数の予算を超えたら取った分を切って止まり、次は取らず、診断に理由が出る。日が変わると戻る。"""
    calls: list = []
    rows = {"items": [gh_row(f"o/r{i}", 100 + i) for i in range(8)]}
    monkeypatch.setattr(connectors, "_http", fake({"https://api.github.com/search/repositories": (200, {}, rows)}, calls))
    enable("github_discover", daily_items=5)
    cfg = connectors.load()
    cfg["connectors"]["github_discover"]["settings"].update(topics=["llm"], watch_stars=False)
    connectors.save(cfg)
    res = run(connectors.run_one("github_discover"))
    assert res["ran"] and res["stopped"] and res["count"] == 5
    writer.flush("t")
    assert len(docs_in_db()) == 5
    ok, why = connectors.gate("github_discover")
    assert not ok and "予算" in why and len(calls) == 1
    assert run(connectors.run_one("github_discover"))["ran"] is False and len(calls) == 1
    from qnapnewshub import diagnostics

    with news_db.session() as db:
        stops = diagnostics.connector_section(db)["budget_stops"]
        assert stops and stops[0]["id"] == "github_discover"
        assert any("予算" in p["text"] for p in diagnostics.build(db)["problems"])
    connectors.state("github_discover")["usage"]["day"] = "2000-01-01"      # 日が変わった
    assert connectors.gate("github_discover") == (True, "")


def test_byte_budget_stops_midway(monkeypatch):
    """通信量の予算（MB）に達したら、次の通信の前に止まり、そこまでの分は保存する。"""
    big = {"items": [gh_row("o/a", 100)], "pad": "x" * 2_000_000}
    monkeypatch.setattr(connectors, "_http", fake({"https://api.github.com/search/repositories": (200, {}, big)}))
    enable("github_discover", daily_mb=1)
    cfg = connectors.load()
    cfg["connectors"]["github_discover"]["settings"].update(topics=["llm", "gguf"], watch_stars=False)
    connectors.save(cfg)
    res = run(connectors.run_one("github_discover"))
    assert res["stopped"] and res["count"] >= 1
    assert connectors.usage("github_discover")["bytes"] > 1024 * 1024


def test_adapted_sources_obey_gate_and_budget():
    """既存の取得（arXiv）も connector の入切の下にある: 切にすると取得先は見送りになり、通信をしない。"""
    assert connectors.for_source("rss:https://rss.arxiv.org/rss/cs.AI") == "arxiv"
    assert connectors.for_source("github:ggml-org/llama.cpp") == "github" and connectors.for_source("hn") == "hn" and connectors.for_source("search:c1:hn") == "hn"
    assert connectors.for_source("hf") == "huggingface" and connectors.for_source("rss:https://example.test/f") == "feed"
    cfg = connectors.load()
    cfg["connectors"]["arxiv"]["enabled"] = False
    connectors.save(cfg)
    hit = []

    async def fetch():
        hit.append(1)
        return []

    src = sources.Source("rss:https://rss.arxiv.org/rss/cs.AI", "arXiv", "news", fetch)
    _s, items, info = run(service._fetch_one(src, asyncio.Semaphore(1)))      # noqa: SLF001
    assert items == [] and "切" in info["skipped"] and not hit


def test_arxiv_feed_items_become_paper_documents():
    """arXiv の RSS の記事は、論文の文書（L1・arXiv ID）にもなる。同じ論文は 1 件のまま。"""
    item = {"title": "A Paper", "url": "https://arxiv.org/abs/2411.01234", "summary": "arXiv:2411.01234v1 Announce Type: new Abstract: We show things.", "guid": "oai:arXiv.org:2411.01234v1",
            "published_at": datetime.now(UTC)}
    conn_impl.stage_arxiv_papers([item])
    conn_impl.stage_arxiv_papers([item])
    writer.flush("t")
    papers = [d for d in docs_in_db() if d.kind == "paper"]
    assert len(papers) == 1 and papers[0].ident == "arxiv:2411.01234" and papers[0].tier == 1 and papers[0].abstract.startswith("We show")


# ---- ④ 段 ----

def test_news_documents_get_tiers_and_migration_is_idempotent():
    """既存のニュースの文書は L1〜L3（要素の取り出しが済んだ→L3・本文を取り出せた→L2・それ以外 L1）。移行は何度流しても同じで、昇格の記録は増えない。古い（段の列が無かった）状態からも同じ結果になる。"""
    from qnapnewshub.db import NewsDocument, NewsItem

    extracted = art(1, [("Alpha", "tech")])
    plain = art(2, [])
    bodied = art(3, [])
    with news_db.session() as db:
        db.query(NewsItem).filter(NewsItem.id == extracted).update({"extract_v": 1})
        db.query(NewsItem).filter(NewsItem.id == plain).update({"extract_v": 0})
        db.query(NewsItem).filter(NewsItem.id == bodied).update({"extract_v": 0, "body_state": 1})
        db.query(Document).update({"tier": 1, "tier_reason": "", "tier_at": None})      # 移行の前の状態
        db.query(DocumentPromotion).delete()
        db.commit()

        def tier_of(aid: int) -> int:
            return db.scalar(select(Document.tier).join(NewsDocument, NewsDocument.document_id == Document.id).where(NewsDocument.news_item_id == aid))

        assert docstore.migrate(db) == 2
        db.commit()
        assert (tier_of(extracted), tier_of(bodied), tier_of(plain)) == (3, 2, 1)
        rows = db.scalars(select(DocumentPromotion).order_by(DocumentPromotion.id)).all()
        assert len(rows) == 2 and {r.to_tier for r in rows} == {2, 3} and all(r.reason for r in rows)
        assert docstore.migrate(db) == 0
        db.commit()
        assert db.scalar(select(func.count(DocumentPromotion.id))) == 2 and docstore.tier_counts(db)["news"] == {"3": 1, "2": 1, "1": 1}


def test_promote_only_goes_up_and_records_reason():
    """段は上げるだけ（下げない・同じ段なら何もしない）。上げるたびに理由と時刻が残る。API は管理者だけ。"""
    with news_db.session() as db:
        doc = docstore.upsert(db, {"kind": "paper", "ids": {"arxiv": "2411.00001"}, "title": "t", "tier": 0})[0]
        db.commit()
        assert doc.tier == 0
        assert docstore.promote(db, doc, 2, "テスト") and not docstore.promote(db, doc, 1, "下げる") and not docstore.promote(db, doc, 2, "同じ")
        db.commit()
        assert doc.tier == 2 and doc.tier_reason == "テスト" and doc.tier_at is not None
        rows = docstore.promotions(db, doc.id)
        assert rows == [{"from": 0, "to": 2, "reason": "テスト", "at": rows[0]["at"]}]
        with pytest.raises(ValueError):
            docstore.promote(db, doc, 9, "x")


def test_tier_follows_extraction_in_write_coordinator_flush():
    """まとめ書きの中で、要素の取り出し（extract_v の更新）が保存された記事の文書は、その場で L3 に上がる（夜の移行を待たない）。"""
    from qnapnewshub.db import NewsDocument, NewsItem

    aid = art(1, [])
    with news_db.session() as db:
        assert db.scalar(select(Document.tier).join(NewsDocument, NewsDocument.document_id == Document.id).where(NewsDocument.news_item_id == aid)) == 1
    writer.get().patch_item(aid, set={"extract_v": 1})
    writer.flush("t")
    with news_db.session() as db:
        d = db.scalar(select(Document).join(NewsDocument, NewsDocument.document_id == Document.id).where(NewsDocument.news_item_id == aid))
        assert d.tier == 3 and "L3" in d.tier_reason
        assert db.scalar(select(NewsItem.extract_v).where(NewsItem.id == aid)) == 1


# ---- ⑤ 昇格の規則 ----

def _promote_run(rec: dict, promo: dict | None = None, **ctx_over) -> Document:
    with news_db.session() as db:
        doc = docstore.upsert(db, rec)[0]
        promo = promo or dict(connectors.DEFAULT_PROMOTION)
        ctx = {"channels": [], "hot": [], "watch": [], "surge": 100.0, **ctx_over}
        docstore.apply_rules(db, [doc], promo, ctx)
        db.commit()
        db.refresh(doc)
        return doc


def test_promotion_rules_with_reasons():
    """昇格の規則: チャンネルに合う・ホットトピックを含む・監視の物を含む・スターの急増・監視のモデルの派生版 → L2。どれにも合わなければ上がらない。除外語のチャンネルは合わない。"""
    base = {"kind": "paper", "title": "Fast ROCm kernels", "abstract": "gguf quantization on rocm", "tier": 1}
    d = _promote_run({**base, "ids": {"arxiv": "2411.10001"}}, channels=[{"name": "GPU", "include": ["rocm"], "exclude": []}])
    assert d.tier == 2 and "チャンネル「GPU」" in d.tier_reason
    d = _promote_run({**base, "ids": {"arxiv": "2411.10002"}}, channels=[{"name": "GPU", "include": ["rocm"], "exclude": ["gguf"]}])
    assert d.tier == 1
    d = _promote_run({**base, "ids": {"arxiv": "2411.10003"}}, hot=["quantization"])
    assert d.tier == 2 and "ホットトピック" in d.tier_reason
    d = _promote_run({**base, "ids": {"arxiv": "2411.10004"}}, watch=["kernels"])
    assert d.tier == 2 and "監視の物" in d.tier_reason
    d = _promote_run({**base, "ids": {"arxiv": "2411.10005"}, "title": "plain", "abstract": "plain"})
    assert d.tier == 1
    d = _promote_run({"kind": "repo", "ids": {"url": "https://github.com/o/surge"}, "title": "o/surge", "tier": 1, "meta": {"stars_delta": 250}})
    assert d.tier == 2 and "スターの急増" in d.tier_reason
    d = _promote_run({"kind": "model", "ids": {"url": "https://huggingface.co/x/y-gguf"}, "title": "x/y-gguf", "tier": 1, "meta": {"derivative_of": "a/b", "relation": "quantized"}})
    assert d.tier == 2 and "派生版" in d.tier_reason
    off = dict(connectors.DEFAULT_PROMOTION, surge=False)
    d = _promote_run({"kind": "repo", "ids": {"url": "https://github.com/o/surge2"}, "title": "o/surge2", "tier": 1, "meta": {"stars_delta": 250}}, promo=off)
    assert d.tier == 1


# ---- ⑥ 論文の同定 ----

def test_paper_identity_merges_arxiv_doi_openalex_and_s2():
    """同じ論文は識別子が違っても 1 件: arXiv の DOI は arXiv ID にそろう・後から別の識別子が分かれば別名に足す・題と引用数は補う・L0 から L1 へ上がる。"""
    with news_db.session() as db:
        a, created = docstore.upsert(db, {"kind": "paper", "ids": {"s2": "a" * 40, "arxiv": "2411.77777v2"}, "title": "", "tier": 0})
        assert created and a.ident == "arxiv:2411.77777"
        b, created_b = docstore.upsert(db, {"kind": "paper", "ids": {"doi": "https://doi.org/10.48550/arXiv.2411.77777", "openalex": "https://openalex.org/W123456"},
                                            "title": "The Paper", "tier": 1, "meta": {"citations": 12}})
        assert not created_b and b.id == a.id and a.title == "The Paper" and a.tier == 1
        c, created_c = docstore.upsert(db, {"kind": "paper", "ids": {"openalex": "W123456", "doi": "10.1000/xyz.1"}, "title": "x", "tier": 1, "meta": {"citations": 30}})
        assert not created_c and c.id == a.id
        d, created_d = docstore.upsert(db, {"kind": "paper", "ids": {"doi": "10.1000/XYZ.1"}, "title": "x", "tier": 1})
        assert not created_d and d.id == a.id
        db.commit()
        assert db.scalar(select(func.count(Document.id))) == 1
        assert json.loads(a.meta)["citations"] == 30
        aliases = {x for x in db.scalars(select(DocumentAlias.ident))}
        assert {"doi:10.1000/xyz.1", "openalex:W123456", f"s2:{'a' * 40}"} <= aliases
        assert docstore.upsert(db, {"kind": "paper", "ids": {}, "title": "no id"}) == (None, False)


def test_openalex_and_crossref_and_s2_parse():
    """OpenAlex の 1 件（書誌と引用数・arXiv の DOI は arXiv ID に）・Crossref の解決（要旨は保存しない）・Semantic Scholar の引用（L0・辺の向き）。"""
    row = {"id": "https://openalex.org/W999999", "doi": "https://doi.org/10.48550/arxiv.2411.55555", "display_name": "Cited Paper", "publication_date": "2026-11-01", "cited_by_count": 7,
           "authorships": [{"author": {"display_name": "A B"}}], "primary_location": {"source": {"display_name": "arXiv"}}, "primary_topic": {"display_name": "NLP"}}
    rec = conn_impl.openalex_doc(row)
    assert rec["kind"] == "paper" and rec["meta"]["citations"] == 7 and rec["meta"]["authors"] == ["A B"] and rec["tier"] == 1
    from qnapnewshub import docstore as ds

    assert ds.paper_idents(rec["ids"])[0] == "arxiv:2411.55555"
    cr = conn_impl.crossref_doc("10.1000/abc", {"title": ["Resolved"], "author": [{"given": "C", "family": "D"}], "container-title": ["J"], "issued": {"date-parts": [[2025, 3]]},
                                                  "is-referenced-by-count": 4, "abstract": "<p>secret abstract</p>"})
    assert cr["title"] == "Resolved" and cr["abstract"] == "" and cr["meta"]["venue"] == "J"
    s2 = conn_impl.s2_paper_rec({"paperId": "b" * 40, "externalIds": {"DOI": "10.1/x", "ArXiv": "2411.00002"}, "title": "T", "year": 2025, "citationCount": 3})
    assert s2["tier"] == 0 and s2["meta"]["citations"] == 3 and ds.paper_idents(s2["ids"])[0] == "arxiv:2411.00002"


def test_semantic_scholar_depth_budget_and_links(monkeypatch):
    """引用は 1〜2 段・件数つき: 段 1 は引用元と参考文献の辺（cites の向き）、段 2 は上位の数件だけ。辺は document_links に 1 回だけ入る。"""
    def paper(pid, n):
        return {"paperId": pid * 40, "externalIds": {"ArXiv": f"2411.{n:05d}"}, "title": f"P{n}", "citationCount": n}

    routes = {f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:2411.00100/citations": (200, {}, {"data": [{"citingPaper": paper("c", 1)}]}),
              f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:2411.00100/references": (200, {}, {"data": [{"citedPaper": paper("d", 2)}]}),
              "https://api.semanticscholar.org/graph/v1/paper/": (200, {}, {"data": []})}
    calls: list = []
    monkeypatch.setattr(connectors, "_http", fake(routes, calls))
    cfg = connectors.load()
    cfg["connectors"]["semanticscholar"]["settings"].update(depth=2, second_level=1)
    connectors.save(cfg)
    enable("semanticscholar")
    res = run(connectors.run_one("semanticscholar", seeds=[("ARXIV:2411.00100", {"ident": "arxiv:2411.00100", "title": "Seed"})]))
    assert res["ran"] and not res["error"]
    assert len(calls) == 4      # 段 1 が 2 回・段 2（上位 1 件）が 2 回
    writer.flush("t")
    run(connectors.run_one("semanticscholar", seeds=[("ARXIV:2411.00100", {"ident": "arxiv:2411.00100", "title": "Seed"})]))
    writer.flush("t")
    with news_db.session() as db:
        assert db.scalar(select(func.count(DocumentLink.id))) == 2
        assert db.scalar(select(func.count(Document.id))) == 3
        seed = db.scalar(select(Document).where(Document.ident == "arxiv:2411.00100"))
        assert "s2_expanded_at" in seed.meta


# ---- ⑦ HF ----

def test_hf_derivative_match_and_quant_kinds():
    """監視のモデルの派生版: タグ base_model:<関係>:<id>（大文字小文字を区別しない）・cardData.base_model で一致。量子化の種類は GGUF・AWQ・GPTQ・EXL2・MLX など。関係のないモデルは一致しない。"""
    row = {"id": "bartowski/Foo-8B-GGUF", "tags": ["gguf", "base_model:quantized:Org/Foo-8B", "license:apache-2.0"], "library_name": "gguf", "pipeline_tag": "text-generation"}
    assert conn_impl.derivative_of(row, ["org/foo-8b"]) == ("org/foo-8b", "quantized")
    assert conn_impl.derivative_of(row, ["other/model"]) is None
    assert conn_impl.quant_kinds(row) == ["gguf"]
    card = {"id": "u/Foo-AWQ", "tags": ["awq"], "cardData": {"base_model": ["org/Foo-8B"]}}
    assert conn_impl.derivative_of(card, ["org/Foo-8B"]) == ("org/Foo-8B", "quantized") and conn_impl.quant_kinds(card) == ["awq"]
    ft = {"id": "u/Foo-chat", "tags": ["base_model:finetune:org/Foo-8B"]}
    assert conn_impl.derivative_of(ft, ["org/Foo-8B"])[1] == "finetune"
    assert conn_impl.quant_kinds({"id": "u/m-exl2-4bpw", "tags": []}) == ["exl2"]
    doc = conn_impl.model_doc(row, ["org/Foo-8B"])
    assert doc["meta"]["derivative_of"] == "org/Foo-8B" and doc["meta"]["quant"] == ["gguf"] and doc["kind"] == "model"


def test_hf_connector_collects_new_models_and_derivatives(monkeypatch):
    """HF connector: 新しいモデルと、監視のモデルごとの派生版を取り、文書（L1）にする。派生版は昇格の規則で L2 になる。"""
    new = [{"id": "a/new-model", "tags": ["gguf"], "downloads": 5, "likes": 1, "createdAt": "2026-11-24T00:00:00Z", "pipeline_tag": "text-generation"}]
    derived = [{"id": "q/Foo-AWQ", "tags": ["awq", "base_model:quantized:org/Foo"], "createdAt": "2026-11-24T00:00:00Z"},
               {"id": "z/Unrelated", "tags": ["base_model:finetune:other/Model"], "createdAt": "2026-11-24T00:00:00Z"}]      # サーバーの絞り込みが効かず、関係のないモデルが混ざっても入れない

    async def http(url, *, params, headers, accept, max_bytes):
        flt = params.get("filter")
        rows = derived if isinstance(flt, str) and flt.startswith("base_model:") else new
        return 200, {}, json.dumps(rows).encode()

    monkeypatch.setattr(connectors, "_http", http)
    enable("hf_models")
    cfg = connectors.load()
    cfg["connectors"]["hf_models"]["settings"].update(watch_models=["org/Foo"], relations=["quantized"])
    connectors.save(cfg)
    res = run(connectors.run_one("hf_models"))
    assert res["ran"] and res["count"] == 2
    writer.flush("t")
    by = {d.title: d for d in docs_in_db()}
    assert "z/Unrelated" not in by
    assert by["a/new-model"].tier == 1 and by["q/Foo-AWQ"].tier == 2 and "派生版" in by["q/Foo-AWQ"].tier_reason


# ---- ⑧ GitHub ----

def test_github_discover_stars_delta_and_surge(monkeypatch):
    """GitHub の発見: 検索の語に直近の日数・スター数が入る。スター数は日ごとに Metric へ、前日との差（stars_delta_1d）も入り、急増なら L2 へ。鍵が無ければ認証のヘッダーを付けない。"""
    q = conn_impl.search_query("llm", days=7, min_stars=50, now=datetime(2026, 11, 25, tzinfo=UTC))
    assert q == "topic:llm created:>2026-11-18 stars:>=50"
    calls: list = []
    state = {"stars": 100}

    async def http(url, *, params, headers, accept, max_bytes):
        calls.append({"url": url, "headers": headers})
        return 200, {}, json.dumps({"items": [gh_row("o/rocket", state["stars"])]}).encode()

    monkeypatch.setattr(connectors, "_http", http)
    enable("github_discover")
    cfg = connectors.load()
    cfg["connectors"]["github_discover"]["settings"].update(topics=["llm"], watch_stars=False)
    connectors.save(cfg)
    run(connectors.run_one("github_discover"))
    writer.flush("t")
    assert "Authorization" not in (calls[0]["headers"] or {})
    # 昨日の点を作って、今日 +300
    with news_db.session() as db:
        for p in db.scalars(select(MetricPoint)):
            p.at = p.at - timedelta(days=1)
        db.commit()
    connectors.state("github_discover")["usage"] = {"day": connectors.today(), "items": 0, "bytes": 0, "requests": 0}
    state["stars"] = 400
    run(connectors.run_one("github_discover"))
    writer.flush("t")
    with news_db.session() as db:
        pts = docstore.series(db, "repo:o/rocket", "stars", 5)
        assert [p["value"] for p in pts] == [100.0, 400.0]
        delta = docstore.series(db, "repo:o/rocket", "stars_delta_1d", 5)
        assert delta and delta[-1]["value"] == 300.0 and delta[-1]["unit"] == "count/day"
        doc = db.scalar(select(Document).where(Document.title == "o/rocket"))
        assert doc.tier == 2 and "スターの急増" in doc.tier_reason
        # 同じ日の再取得は同じ点（重複しない）
        n = db.scalar(select(func.count(MetricPoint.id)))
    run(connectors.run_one("github_discover"))
    writer.flush("t")
    with news_db.session() as db:
        assert db.scalar(select(func.count(MetricPoint.id))) == n


def test_optional_key_is_sent_but_never_returned(admin_client, monkeypatch):
    """鍵（任意）: 入れると認証のヘッダーに付く。応答・診断・設定の API には値を出さない（has_key だけ）。暗号化して保存する。"""
    secret = "-".join(["ghp", "unit", "test", "value"])
    r = admin_client.put("/api/v1/connectors/github_discover/key", json={"value": secret}, headers=H)
    assert r.status_code == 200 and r.json() == {"has_key": True}
    assert secret not in admin_client.get("/api/v1/connectors").text
    from qnapnewshub.config import data_dir

    assert secret not in (data_dir() / "secrets.json").read_text()
    assert connectors.auth_headers("github_discover") == {"Authorization": f"Bearer {secret}"}
    assert admin_client.put("/api/v1/connectors/hn/key", json={"value": secret}, headers=H).status_code == 422      # 鍵を使わない connector
    assert admin_client.delete("/api/v1/connectors/github_discover/key", headers=H).json() == {"has_key": False}
    assert connectors.auth_headers("github_discover") == {}
    from qnapnewshub import diagnostics

    with news_db.session() as db:
        assert secret not in json.dumps(diagnostics.bundle(db), ensure_ascii=False)


# ---- ⑨ Sitemap ----

SITEMAP = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://news.example.test/posts/hello-world.html</loc><lastmod>2026-11-20</lastmod></url>
<url><loc>https://news.example.test/private/secret-page</loc><lastmod>2026-11-21</lastmod></url>
<url><loc>https://other.example.test/elsewhere</loc></url>
<url><loc>ftp://news.example.test/x</loc></url></urlset>"""


def test_sitemap_parse_rules():
    """sitemap の整形: 同じホストだけ・http/https だけ・robots.txt の Disallow を除く・題は URL から・DTD は断る・gzip は展開・件数の上限。"""
    kind, rows = conn_impl.parse_sitemap(SITEMAP)
    assert kind == "urlset" and len(rows) == 4
    docs, blocked = conn_impl.sitemap_docs(rows, site="Ex", host="news.example.test", allowed=lambda u: "/private/" not in u, max_urls=10)
    assert [d["url"] for d in docs] == ["https://news.example.test/posts/hello-world.html"] and blocked == 1
    assert docs[0]["title"] == "hello world" and docs[0]["tier"] == 0
    with pytest.raises(ValueError):
        conn_impl.parse_sitemap(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><urlset></urlset>')
    import gzip

    assert conn_impl.parse_sitemap(gzip.compress(SITEMAP))[0] == "urlset"
    many = [(f"https://a.test/p{i}", f"2026-11-{i % 28 + 1:02d}") for i in range(50)]
    assert len(conn_impl.sitemap_docs(many, site="a", host="a.test", allowed=lambda u: True, max_urls=7)[0]) == 7


def test_sitemap_connector_honors_robots_txt(monkeypatch):
    """connector 全体で robots.txt を守る: Disallow の URL は文書にならない。robots.txt が 5xx で読めないサイトは何も取らない（sitemap も取りに行かない）。"""
    robots = b"User-agent: *\nDisallow: /private/\n"

    async def b_http(url, *, max_bytes, accept):
        if url.endswith("/robots.txt"):
            return (200, {}, robots) if "news.example.test" in url else (503, {}, b"")
        raise AssertionError(url)

    fetched: list = []

    async def http(url, *, params, headers, accept, max_bytes):
        fetched.append(url)
        return 200, {}, SITEMAP

    monkeypatch.setattr(bodies, "_http", b_http)
    monkeypatch.setattr(connectors, "_http", http)
    enable("sitemap")
    cfg = connectors.load()
    cfg["connectors"]["sitemap"]["settings"]["sites"] = [{"name": "Ex", "url": "https://news.example.test/sitemap.xml"}, {"name": "Down", "url": "https://down.example.test/sitemap.xml"}]
    connectors.save(cfg)
    res = run(connectors.run_one("sitemap"))
    assert res["count"] == 1 and fetched == ["https://news.example.test/sitemap.xml"] and "robots.txt を読めない" in res["note"] and "除いた URL 1 件" in res["note"]
    writer.flush("t")
    assert [d.url for d in docs_in_db()] == ["https://news.example.test/posts/hello-world.html"]


# ---- ⑩ 変化の検出 ----

def test_change_detection_etag_hash():
    """変化の検出: 初回は基準だけ（変化にしない）・見える文字が同じなら（script・style・空白が違っても）変化なし・文字が変われば変化・304 は変化なしで本文を読まない。"""
    page = b"<html><head><style>a{}</style></head><body><h1>Hello</h1><script>var t=1</script> text </body></html>"
    st, out = conn_impl.detect_change(None, 200, {"etag": '"v1"'}, page, 1000.0)
    assert out == "baseline" and st["etag"] == '"v1"'
    st2, out2 = conn_impl.detect_change(st, 200, {"etag": '"v2"'}, page.replace(b"var t=1", b"var t=2").replace(b"<h1>", b"<h1>  "), 2000.0)
    assert out2 == "unchanged"
    st3, out3 = conn_impl.detect_change(st2, 200, {"etag": '"v3"'}, page.replace(b"Hello", b"Goodbye"), 3000.0)
    assert out3 == "changed" and st3["changed_at"] == 3000.0 and len(st3["changes"]) == 1 and st3["hash"] != st["hash"]
    st4, out4 = conn_impl.detect_change(st3, 304, {}, b"", 4000.0)
    assert out4 == "not_modified" and st4["hash"] == st3["hash"]


def test_change_connector_conditional_get_and_document(monkeypatch):
    """connector: 2 回目は If-None-Match を付けて 304 なら何も起きない。内容が変わると文書（L0）の版（hash）と時刻が更新される。robots.txt が禁じるページは見ない。"""
    async def b_http(url, *, max_bytes, accept):
        return (200, {}, b"User-agent: *\nDisallow: /nope\n")

    seen: list = []
    body = {"v": b"<body>one</body>", "etag": '"a"'}

    async def http(url, *, params, headers, accept, max_bytes):
        seen.append(dict(headers or {}))
        if headers and headers.get("If-None-Match") == body["etag"]:
            return 304, {}, b""
        return 200, {"etag": body["etag"]}, body["v"]

    monkeypatch.setattr(bodies, "_http", b_http)
    monkeypatch.setattr(connectors, "_http", http)
    monkeypatch.setattr(conn_impl, "CHANGE_MIN_GAP", 0)
    enable("change")
    cfg = connectors.load()
    cfg["connectors"]["change"]["settings"]["pages"] = [{"name": "Page", "url": "https://p.example.test/status"}, {"name": "Blocked", "url": "https://p.example.test/nope"}]
    connectors.save(cfg)
    run(connectors.run_one("change"))
    writer.flush("t")
    assert len(seen) == 1 and docs_in_db()[0].tier == 0 and docs_in_db()[0].published_at is None
    v1 = docs_in_db()[0].version
    run(connectors.run_one("change"))
    assert seen[-1].get("If-None-Match") == '"a"'
    body.update(v=b"<body>two</body>", etag='"b"')
    run(connectors.run_one("change"))
    writer.flush("t")
    d = docs_in_db()[0]
    assert d.version != v1 and d.published_at is not None and len(docs_in_db()) == 1


# ---- ⑪ HN の検索・L0 の検索 ----

def test_hn_search_tool_saves_l0_and_l0_is_searchable(monkeypatch):
    """HN の検索は問いに応じて 1 回: 結果を返し、save なら L0（URL・題・日付・ID だけ）の文書として保存する。L0 の文書も題で検索できる（3 文字未満は LIKE）。"""
    data = {"hits": [{"objectID": "123", "title": "Show HN: Rocm benchmark tool", "url": "https://x.example.test/a", "points": 55, "num_comments": 9, "created_at_i": 1763000000}]}
    calls: list = []
    monkeypatch.setattr(connectors, "_http", fake({"https://hn.algolia.com/api/v1/search": (200, {}, data)}, calls))
    connectors.set_enabled("hn_search", True, terms_ack=True)
    res = run(connectors.run_one("hn_search", query="rocm benchmark", days=30, save=True))
    assert res["ran"] and len(calls) == 1 and "numericFilters" in calls[0]["params"] and res["count"] == 1
    assert json.loads(res["note"])[0]["points"] == 55
    writer.flush("t")
    with news_db.session() as db:
        d = db.scalar(select(Document))
        assert d.tier == 0 and d.ident.startswith("url:") and "news.ycombinator.com" in d.ident
        assert [x.id for x in docstore.search(db, "Rocm bench")] == [d.id]
        assert [x.id for x in docstore.search(db, "HN")] == [d.id]
        assert docstore.search(db, "nothing here") == []
    run(connectors.run_one("hn_search", query="again", save=False))
    writer.flush("t")
    assert len(docs_in_db()) == 1


# ---- ⑫ API・まとめ書き・お休み ----

def test_api_is_admin_only_and_documents_listing(admin_client, anon_client):
    """connector の API は管理者だけ（未ログインは 401）。一覧は利用条件・予算・今日の使用を返す。設定は検査され、未確認は明示が要る。文書の一覧に L0 の URL も出る。"""
    from fastapi.testclient import TestClient

    from qnapnewshub.main import app

    with TestClient(app) as anon:
        assert anon.get("/api/v1/connectors").status_code == 401
        assert anon.put("/api/v1/connectors/hn", json={"enabled": False}, headers=H).status_code in (401, 403)
    r = admin_client.get("/api/v1/connectors")
    assert r.status_code == 200
    body = r.json()
    ids = [c["id"] for c in body["connectors"]]
    assert "github_discover" in ids and "sitemap" in ids
    gd = next(c for c in body["connectors"] if c["id"] == "github_discover")
    assert gd["terms"]["tos_url"] and gd["terms"]["checked_at"] and gd["usage"]["items"] == 0 and gd["enabled"] is False
    assert admin_client.put("/api/v1/connectors/hn_search", json={"enabled": True}, headers=H).status_code == 422
    assert admin_client.put("/api/v1/connectors/hn_search", json={"enabled": True, "terms_ack": True}, headers=H).json()["enabled"] is True
    assert admin_client.put("/api/v1/connectors/github_discover", json={"settings": {"topics": ["a b"]}}, headers=H).status_code == 422
    assert admin_client.put("/api/v1/connectors/github_discover", json={"daily_items": 10, "settings": {"topics": ["llm"]}}, headers=H).json()["daily_items"] == 10
    assert admin_client.put("/api/v1/connectors/zzz", json={}, headers=H).status_code == 404
    assert admin_client.post("/api/v1/connectors/hn/run", headers=H).status_code == 400
    with news_db.session() as db:
        docstore.upsert(db, {"kind": "other", "ids": {"url": "https://l0.example.test/page-one"}, "title": "page one", "tier": 0})
        db.commit()
    docs = admin_client.get("/api/v1/documents", params={"q": "page one"}).json()["documents"]
    assert docs and docs[0]["tier"] == 0 and docs[0]["tier_label"] == "L0 発見"
    did = docs[0]["id"]
    assert admin_client.post(f"/api/v1/documents/{did}/promote", json={"tier": 2, "reason": "確認"}, headers=H).json() == {"promoted": True, "tier": 2}
    assert admin_client.get(f"/api/v1/documents/{did}").json()["promotions"][0]["reason"] == "確認"


def test_results_go_through_write_coordinator_and_rest_mode_stops(monkeypatch):
    """結果は Write Coordinator のバッファに積まれ、まとめ書きまで DB に入らない。お休みモード（停止）中は connector を動かさず通信しない。"""
    calls: list = []
    monkeypatch.setattr(connectors, "_http", fake({"https://api.github.com/search/repositories": (200, {}, {"items": [gh_row("o/x", 90)]})}, calls))
    enable("github_discover")
    cfg = connectors.load()
    cfg["connectors"]["github_discover"]["settings"].update(topics=["llm"], watch_stars=False)
    connectors.save(cfg)
    run(connectors.run_one("github_discover"))
    assert docs_in_db() == [] and writer.get().counts()["connectors"] == 1 and writer.get().pending_connector_docs() == 1
    writer.flush("t")
    assert len(docs_in_db()) == 1 and writer.get().counts()["connectors"] == 0
    monkeypatch.setattr(rest, "blocks_fetch", lambda: True)
    connectors.state("github_discover")["usage"]["items"] = 0
    res = run(connectors.run_one("github_discover"))
    assert res["ran"] is False and "お休み" in res["reason"] and len(calls) == 1
    assert run(connectors.run_due()) == []
