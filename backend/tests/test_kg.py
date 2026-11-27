"""目的: v0.5 知識グラフ（文書・根拠・関係・機密の区分の引き継ぎ・履歴と矛盾・知識カード・地図の neighbors・Obsidian の書き出し）の試験。外へ通信しない（LLM は llm._post の模擬）。
  ①ニュースの記事 → documents の対応づけが冪等（再実行・重複 URL・あとから増えた記事・claims の作り直し）②識別子の正規化 ③機密の区分の引き継ぎ（confidential の文書から作った主張・関係・物は confidential で、
  既定の読み取りに出ない・API トークンは機密を読めない・区分は緩めない）④co_occurs の基準（回数 3 以上・期待値の比 2 以上。どこにでも出る物との組は入れない）
  ⑤型つきの関係の検査（原文に引用があれば extracted、無ければ「推測」で確かさを低く、確定にしない・上限 5・型が違えば捨てる）⑥履歴: 変化と矛盾の印（上書きしない）
  ⑦利用者のメモは再分析・カードの作り直しで消えない ⑧neighbors の上限・段ごとのまとめ・絞り込み ⑨書き出しの中身と区分 ⑩カードの概要の LLM は public・allowed の物だけ・材料に無い数字を捨てる・変化のあった物だけ。
根拠: 利用者の指示（v0.5「統合ナレッジ基盤の土台として汎用に」2026-11-25）・ControlDeck の PLAN-integrated-knowledge 4・7 章（機密の区分を派生データへ引き継ぐ・根拠の無い関係は確定しない）・ARCHITECTURE 6 章。
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select, text

from qnapnewshub import db as news_db
from qnapnewshub import bench, extract, kg, kg_job, kg_view, llm, settings, sources, store, writer
from qnapnewshub.auth import AuthUser
from qnapnewshub.db import Claim, Document, Entity, EntityCard, EntityNote, Evidence, NewsDocument, NewsItem, Relationship
from tests.test_events import Srv, run
from tests.test_news_llm_prep import chat, keyed  # noqa: F401  (keyed は fixture)

H = {"X-Requested-With": "NewsHub"}
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731
_n = [0]


def art(hours: float, ents: list[tuple[str, str]], *, facts: list[dict] | None = None, relations: list[dict] | None = None, title: str | None = None, url: str | None = None,
        source: str = "src") -> int:
    """合成の記事を 1 件保存し、取り出し（物・facts・relations）も保存して、記事の id を返す。イベントにまとまらないよう、題は毎回ちがう語にする。"""
    _n[0] += 1
    k = _n[0]
    title = title or f"Zq{k} unique{k * 7} topic report number {k}"
    url = url or f"https://{source}{k}.test/a/{k}"
    store.stage_items([sources.make_item(kind="news", source=source, source_key=f"{source}{k}", title=title, url=url, summary=f"summary body {k}", image=None,
                                         published=NOW() - timedelta(hours=hours))])
    writer.flush("t")
    with news_db.session() as db:
        aid = db.scalar(select(NewsItem.id).where(NewsItem.url == url))
    writer.get().stage_extraction(aid, {"entities": [{"name": n, "type": t} for n, t in ents], "facts": facts or [], "relations": relations or [], "model": "m/x:free",
                                        "version": extract.PROMPT_VERSION})
    writer.flush("t")
    return aid


def ent_id(name: str) -> int:
    with news_db.session() as db:
        return db.scalar(select(Entity.id).where(Entity.key == extract.normalize_key(name)))


# ---- ① 文書の対応・② 識別子 ----

def test_news_documents_mapping_is_idempotent(client):
    """目的: 記事 → documents の対応づけは何度流しても同じ（冪等）。同じ URL（重複）の記事は同じ文書を指し、あとから増えた記事だけが足される。ニュースの文書は public・allowed。
    根拠: 利用者の指示「今のニュースの記事は documents に対応づける（既存の表を壊さず。移行は冪等）」。"""
    a = art(1, [("Alpha", "tech")])
    b = art(2, [("Alpha", "tech")])
    with news_db.session() as db:
        n_docs = db.scalar(select(func.count(Document.id)))
        n_map = db.scalar(select(func.count(NewsDocument.news_item_id)))
        assert n_docs == n_map == 2
        before = kg.sync_documents(db, full=True), kg.sync_documents(db, full=True), kg.sync_documents(db)
        db.commit()
        assert before == (0, 0, 0)
        assert db.scalar(select(func.count(Document.id))) == 2 and db.scalar(select(func.count(NewsDocument.news_item_id))) == 2
        doc = db.scalars(select(Document)).first()
        assert doc.kind == "news" and doc.sensitivity == "public" and doc.ai_use == "allowed" and doc.ident.startswith("url:")
        # 記事の canon_url を同じにした（重複）→ 文書は 1 つのまま、対応は 2 つ
        db.execute(text("UPDATE news_items SET canon_url = 'https://dup.test/x'"))
        db.execute(text("DELETE FROM news_documents"))
        db.execute(text("DELETE FROM documents"))
        db.commit()
        assert kg.sync_documents(db, full=True) == 2
        db.commit()
        assert db.scalar(select(func.count(Document.id))) == 1 and db.scalar(select(func.count(NewsDocument.news_item_id))) == 2
    c = art(3, [("Alpha", "tech")])      # あとから増えた記事は、流さなくても対応づく（まとめ書きの中で）
    with news_db.session() as db:
        assert c in kg.docs_of_articles(db, [a, b, c])
        assert db.scalar(select(Claim.id)) is None


def test_claims_table_rebuild_allows_null_article_and_keeps_rows():
    """目的: 古い形の claims（article_id が NOT NULL）を、行を保ったまま NULL を許す形へ 1 回だけ作り直す。2 回目は何もしない。根拠: ニュース以外の文書の主張は記事が無い（v0.5）。既存の DB を壊さない決まり。"""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE claims (id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL, entity_id INTEGER, kind VARCHAR(16), text TEXT, value VARCHAR(120), "
                          "source_article_id INTEGER, model VARCHAR(200), prompt_version VARCHAR(16), created_at DATETIME, ai_extracted BOOLEAN DEFAULT 1, "
                          "document_id INTEGER, sensitivity VARCHAR(12) NOT NULL DEFAULT 'public', ai_use VARCHAR(12) NOT NULL DEFAULT 'allowed')"))
        conn.execute(text("INSERT INTO claims (id, article_id, entity_id, kind, text, value, model, prompt_version, created_at) VALUES (7, 1, 2, 'price', 't', '10', 'm', 'ex1', '2026-01-01 00:00:00')"))
        news_db._rebuild_claims(conn)  # noqa: SLF001
        info = {r[1]: r for r in conn.execute(text("PRAGMA table_info(claims)")).all()}
        assert info["article_id"][3] == 0
        assert conn.execute(text("SELECT id, article_id, value, sensitivity FROM claims")).all() == [(7, 1, "10", "public")]
        conn.execute(text("INSERT INTO claims (id, article_id, kind, text, value, model, prompt_version, created_at) VALUES (8, NULL, 'x', '', '', '', '', '2026-01-01 00:00:00')"))
        news_db._rebuild_claims(conn)  # noqa: SLF001
        assert conn.execute(text("SELECT count(*) FROM claims")).scalar() == 2


def test_normalize_ident_forms():
    """目的: 識別子（URL・DOI・arXiv・リポジトリ＋SHA・特許・規格）を 1 つの形にそろえる。判別できなければ ValueError。根拠: 「正規化した識別子」の指示。"""
    assert kg.normalize_ident("https://doi.org/10.1234/ABC.5") == kg.normalize_ident("doi:10.1234/abc.5") == "doi:10.1234/abc.5"
    assert kg.normalize_ident("https://arxiv.org/abs/2401.12345v3") == kg.normalize_ident("arXiv:2401.12345") == "arxiv:2401.12345"
    assert kg.normalize_ident("https://github.com/Owner/Repo.git", sha="ABCDEF1") == "repo:github.com/owner/repo@abcdef1"
    assert kg.normalize_ident("JP 2023-123456 A") == "patent:JP2023123456A"
    assert kg.normalize_ident("ISO 9001:2015") == "standard:ISO 9001:2015"
    assert kg.normalize_ident("HTTPS://Example.COM/a?utm_source=x#frag") == kg.normalize_ident("https://example.com/a")
    with pytest.raises(ValueError):
        kg.normalize_ident("ただの文章")
    with pytest.raises(ValueError):
        kg.normalize_ident("https://github.com/o/r", sha="not-a-sha")


# ---- ③ 機密の区分の引き継ぎ ----

def test_confidential_document_derivatives_are_confidential(client):
    """目的: confidential の文書から作った主張・関係・物は confidential になり、既定の読み取り（public）に出ない。管理者だけが confidential=true で読める。API トークン（管理者でない）は読めない。
    文書の区分は厳しい方へしか動かない（緩められない）。根拠: ControlDeck の PLAN-integrated-knowledge 7 章「機密の区分を派生データにも引き継ぐ」。"""
    r = client.post("/api/v1/kg/documents", json={"kind": "patent", "ident": "JP 2026-000111 A", "title": "自分のアイデア"}, headers=H)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["sensitivity"] == "confidential" and doc["ai_use"] == "local_only" and doc["ident"] == "patent:JP2026000111A"
    again = client.post("/api/v1/kg/documents", json={"kind": "patent", "ident": "patent:JP2026000111A", "sensitivity": "public", "ai_use": "allowed"}, headers=H).json()
    assert again["id"] == doc["id"] and again["sensitivity"] == "confidential"      # 緩めない
    c = client.post("/api/v1/kg/claims", json={"document_id": doc["id"], "entity": "秘密の方式", "kind": "claim", "text": "新しい冷却方式", "value": "v1",
                                               "evidences": [{"quote": "冷却方式", "page": 3}, {"quote": "別の根拠", "start": 10, "end": 20}]}, headers=H)
    assert c.status_code == 200 and c.json()["sensitivity"] == "confidential"
    rel = client.post("/api/v1/kg/relationships", json={"document_id": doc["id"], "subject": "秘密の方式", "object": "Peltier", "type": "uses",
                                                       "evidences": [{"quote": "Peltier を使う", "page": 4}]}, headers=H)
    assert rel.status_code == 200, rel.text
    assert rel.json()["sensitivity"] == "confidential" and rel.json()["status"] == "confirmed" and rel.json()["evidence_count"] == 1
    secret = ent_id("秘密の方式")
    with news_db.session() as db:
        e = db.get(Entity, secret)
        assert e.sensitivity == "confidential" and e.ai_use == "local_only"
        assert db.scalar(select(func.count(Evidence.id)).where(Evidence.claim_id == c.json()["id"])) == 2
        assert db.get(Claim, c.json()["id"]).article_id is None
    assert client.get(f"/api/v1/kg/entities/{secret}/card").status_code == 404
    assert client.get(f"/api/v1/kg/entities/{secret}/card?confidential=true").json()["claims"][0]["evidences"][0]["quote"] == "冷却方式"
    pub = client.post("/api/v1/graph/neighbors", json={"ids": [secret]}).json()
    assert pub["seeds"] == [] and pub["levels"] == []
    full = client.post("/api/v1/graph/neighbors", json={"ids": [secret], "confidential": True}).json()
    assert len(full["levels"][0]["edges"]) == 1 and full["levels"][0]["edges"][0]["sensitivity"] == "confidential"
    assert client.get("/api/v1/kg/documents").json()["total"] == 0 and client.get("/api/v1/kg/documents?confidential=true").json()["total"] == 1
    with pytest.raises(Exception):
        from fastapi import HTTPException
        from qnapnewshub.kg_router import scopes_for

        try:
            scopes_for(AuthUser(id=1, username="t", via="token", scope="read"), True)
        except HTTPException as exc:
            assert exc.status_code == 403
            raise
    assert kg.inherit([("public", "allowed"), ("confidential", "local_only")]) == ("confidential", "local_only")
    assert kg.inherit([("public", "allowed"), ("public", "prohibited")]) == ("public", "prohibited")
    assert kg.inherit([]) == ("confidential", "local_only")      # 根拠が無ければ fail-closed


def test_relationship_sensitivity_follows_all_evidence_documents(client):
    """目的: 関係の根拠に機密の文書が 1 つでも混ざれば、その関係は confidential（根拠のうち最も厳しい区分）。根拠: 機密の派生データを公開の範囲に出さない（PLAN-integrated-knowledge 7 章）。"""
    pub = art(1, [("Foo", "tech"), ("Bar", "tech")], relations=[{"subject": "Foo", "type": "uses", "object": "Bar", "quote": "", "grounded": False}])
    d = client.post("/api/v1/kg/documents", json={"kind": "note", "ident": "https://notes.test/private"}, headers=H).json()
    client.post("/api/v1/kg/relationships", json={"document_id": d["id"], "subject": "Foo", "object": "Bar", "type": "uses", "evidences": [{"quote": "Foo uses Bar"}]}, headers=H)
    with news_db.session() as db:
        r = db.scalar(select(Relationship).where(Relationship.type == "uses"))
        assert r.sensitivity == "confidential" and r.evidence_count == 2 and r.status == "confirmed"
        assert db.get(Entity, ent_id("Foo")).sensitivity == "public"      # 公開の記事に出た物は public のまま
    assert pub


# ---- ④ co_occurs ----

def test_cooccur_threshold_count_and_ratio(client):
    """目的: 一緒に出る（co_occurs）は、回数 3 以上かつ期待値の比 2 以上の組だけ。2 回の組・どこにでも出る物との組（比が 1 前後）は入れない。集計だけで作り、型は co_occurs・向き無し・statistical。
    根拠: 利用者の指示「偶然より多い組だけ: 期待値の比 2 以上・回数 3 以上」。"""
    k = 0
    for i in range(30):      # どこにでも出る物 Fil
        art(60 * k + 1, [("Fil", "org")]); k += 1
    for i in range(3):
        art(60 * k + 1, [("Aaa", "tech"), ("Bbb", "tech")]); k += 1
    for i in range(3):
        art(60 * k + 1, [("Aaa", "tech"), ("Fil", "org")]); k += 1      # Fil と一緒でも、Fil が多すぎて比は低い
    for i in range(2):
        art(60 * k + 1, [("Ccc", "tech"), ("Ddd", "tech")]); k += 1      # 2 回
    with news_db.session() as db:
        out = kg.rebuild_cooccur(db)
        db.commit()
        assert out["kept"] == 1
        rel = db.scalar(select(Relationship).where(Relationship.type == "co_occurs"))
        names = {db.get(Entity, rel.subject_id).name, db.get(Entity, rel.object_id).name}
        assert names == {"Aaa", "Bbb"} and rel.subject_id < rel.object_id and not rel.directed
        assert rel.stat_n == 3 and rel.stat_ratio >= 2 and rel.status == "statistical" and rel.sensitivity == "public" and rel.first_at <= rel.last_at
        kg.rebuild_cooccur(db)      # 作り直しても同じ（置き換え）
        db.commit()
        assert db.scalar(select(func.count(Relationship.id))) == 1


def test_cooccur_counts_event_once(client):
    """目的: 同じイベントの記事は 1 単位として数える（同じ出来事を何社が報じても 1 回）。根拠: ARCHITECTURE 4 章「同じ出来事を 20 のサイトが報じても代表の 1 回」。"""
    from tests.test_events import seed

    seed(["gpt62"])      # 4 記事・同じ 2 つの物（OpenAI・GPT-6.2）・1 イベント
    with news_db.session() as db:
        out = kg.rebuild_cooccur(db)
        assert out["kept"] == 0 and out["units"] == 1


# ---- ⑤ 型つきの関係 ----

def test_relations_grounded_vs_inferred(client):
    """目的: relations は原文（LLM に送った題と抜粋）に引用があるものだけ extracted（確かさ 0.7）。引用が無い・原文に無い関係は「推測」（inferred）で確かさ 0.35 以下、確定にしない。
    型が違う・同じ物どうし・6 件目以降は捨てる。根拠: 利用者の指示「根拠の無い関係は推測として確かさを低く、確定にしない」・LLM の答えを信用しない決まり。"""
    source = "OpenAI released GPT-6.2 which replaces GPT-6.1 in the API."
    raw = {"entities": [{"name": "OpenAI", "type": "org"}, {"name": "GPT-6.2", "type": "model"}, {"name": "GPT-6.1", "type": "model"}],
           "relations": [{"subject": "GPT-6.2", "type": "replaces", "object": "GPT-6.1", "quote": "replaces GPT-6.1"},
                         {"subject": "GPT-6.2", "type": "uses", "object": "GPT-6.1", "quote": "powered by moon rocks"},
                         {"subject": "OpenAI", "type": "bogus", "object": "GPT-6.2", "quote": "released GPT-6.2"},
                         {"subject": "OpenAI", "type": "supports", "object": "OpenAI", "quote": ""},
                         {"subject": "OpenAI", "type": "co_occurs", "object": "GPT-6.2", "quote": "OpenAI released"}]}
    got = extract.clean(raw, source)
    assert [(r["type"], r["grounded"]) for r in got["relations"]] == [("replaces", True), ("uses", False)]
    assert got["relations"][1]["quote"] == ""
    many = {"entities": [{"name": "A1", "type": "tech"}], "relations": [{"subject": f"S{i}", "type": "uses", "object": f"O{i}", "quote": ""} for i in range(9)]}
    assert len(extract.clean(many, "")["relations"]) == extract.MAX_RELATIONS
    aid = art(1, [("OpenAI", "org"), ("GPT-6.2", "model"), ("GPT-6.1", "model")], relations=got["relations"])
    with news_db.session() as db:
        rows = {r.type: r for r in db.scalars(select(Relationship))}
        assert set(rows) == {"replaces", "uses"}
        assert rows["replaces"].status == "extracted" and rows["replaces"].confidence == 0.7 and rows["replaces"].directed
        assert rows["uses"].status == "inferred" and rows["uses"].confidence <= kg.INFERRED_CAP
        ev = db.scalar(select(Evidence).where(Evidence.relationship_id == rows["replaces"].id))
        assert ev.quote == "replaces GPT-6.1" and len(ev.quote_hash) == 64 and ev.document_id == db.scalar(select(NewsDocument.document_id).where(NewsDocument.news_item_id == aid))
        assert rows["replaces"].activity_model == "m/x:free" and rows["replaces"].activity_version == extract.PROMPT_VERSION
    # 別の記事でも同じ関係が出れば、根拠が 2 つになり確かさが上がる。取り出し直しで前の根拠は置き換わる（二重にならない）
    art(2, [("OpenAI", "org"), ("GPT-6.2", "model"), ("GPT-6.1", "model")], relations=[got["relations"][0]])
    writer.get().stage_extraction(aid, {"entities": [{"name": "OpenAI", "type": "org"}], "facts": [], "relations": [], "model": "m", "version": "x"})
    writer.flush("t")
    with news_db.session() as db:
        rep = db.scalar(select(Relationship).where(Relationship.type == "replaces"))
        assert rep.evidence_count == 1 and rep.status == "extracted"
        assert db.scalar(select(Relationship).where(Relationship.type == "uses")) is None      # 根拠が 0 になった推測は消える


def test_prompt_asks_relations_in_same_call_and_fits_output_budget():
    """目的: relations は下ごしらえと同じ 1 回の呼び出しに足す（呼び出しは増やさない・件数 20 のまま）。典型的な出力は 1 件あたり出力の上限（TOKENS_PER_ITEM）に収まる。最悪（全部最大）は超えうるが、その場合は
    JSON が切れて既存の再試行（欠けた記事だけ送り直し）が受ける。根拠: 利用者の指示「件数 20 のまま出力の上限に収まるか見積もって docs に」・research-free-llm-batch.md の実測（30 件で切れた）。"""
    from qnapnewshub import prep

    items = [{"id": i, "title": f"t{i}", "source": "s", "summary": "x"} for i in range(1, 21)]
    prompt = prep.build_prompt(items)
    assert '"relations": [...]' in prompt and "relations:" in prompt and prep.BATCH == 20

    def tokens(s: str) -> float:      # 粗い見積もり: ASCII は 3 文字 1 トークン（JSON の記号が多いので細かめ）、日本語は 1 文字 1 トークン（安全側）
        return sum(1 / 3 if ch.isascii() else 1.0 for ch in s)

    def one(n_rel: int) -> str:
        return json.dumps({"ja": "新しいモデルが公開され、速度が向上したと報じられた。" * 3, "img": "a small server on a desk with soft light and cables", "topics": ["GPU", "ドライバー", "量子化"],
                           "entities": [{"name": f"Name{i}", "type": "model"} for i in range(8)],
                           "facts": [{"about": "Name1", "kind": "release", "text": "新しい版が公開された。" * 2, "value": "1.5"} for _ in range(5)],
                           "relations": [{"subject": "Name1", "type": "replaces", "object": "Name2", "quote": "Name1 replaces Name2 in the lineup"} for _ in range(n_rel)]}, ensure_ascii=False)

    typical, worst = tokens(one(2)), tokens(one(5))
    assert typical <= prep.TOKENS_PER_ITEM and typical * 20 <= 12000
    assert worst > 0      # 最悪の見積もりは docs/DESIGN.md 13 章へ（実測ではなく見積もり）
    print("tokens per item typical/worst", round(typical), round(worst))


# ---- ⑥ 履歴と矛盾 ----

def test_history_marks_change_and_contradiction(client):
    """目的: 同じ物・同じ種類の主張の値が時間を置いて変われば「変化」、近い時期（3 日以内）に別の文書が別の値を述べれば「矛盾」の印。どちらも元の主張は消さない・上書きしない。値の無い主張は比べない。
    根拠: 利用者の指示「値が変わったら変化、同時期に食い違えば矛盾として印。上書きしない」・ARCHITECTURE 3 章（時系列の変化として残す）。"""
    f = lambda v, t="価格": {"about": "Gizmo", "kind": "price", "text": f"{t}は {v} 円", "value": v}  # noqa: E731
    art(24 * 40, [("Gizmo", "product")], facts=[f("1000")])
    art(24 * 30, [("Gizmo", "product")], facts=[f("1000")])
    art(24 * 20, [("Gizmo", "product")], facts=[f("1200")])      # 変化
    art(24 * 6, [("Gizmo", "product")], facts=[f("1200")])
    art(24 * 2, [("Gizmo", "product")], facts=[f("1500", "A 社の報道")])
    art(24 * 2 - 5, [("Gizmo", "product")], facts=[f("1300", "B 社の報道")])      # 同時期に別の値 → 矛盾
    art(24 * 1, [("Gizmo", "product")], facts=[{"about": "Gizmo", "kind": "release", "text": "発売", "value": ""}])
    with news_db.session() as db:
        h = kg.history(db, ent_id("Gizmo"))
        assert h["changes"] == 1 and h["contradictions"] == 2
        marks = [(e["value"], e["mark"]) for e in h["series"][0]["entries"]]
        assert marks == [("1000", "first"), ("1000", "same"), ("1200", "change"), ("1200", "same"), ("1500", "contradiction"), ("1300", "contradiction")]
        assert h["series"][0]["entries"][2]["from"] == "1000"
        assert [g["kind"] for g in h["series"]] == ["price"]      # value の無い release は入らない
        assert db.scalar(select(func.count(Claim.id)).where(Claim.kind == "price")) == 6      # 元の主張は 1 つも消えない
    # 同じ文書が値を言い換えても矛盾にしない
    e = [{"t": datetime(2026, 1, 1), "value": "v1.0", "doc_id": 1, "claim_id": 1}, {"t": datetime(2026, 1, 1, 1), "value": "1.0", "doc_id": 2, "claim_id": 2}]
    assert [x["mark"] for x in kg.mark_series(e)] == ["first", "same"]


def test_history_groups_by_metric_and_compares_numbers_with_conditions(client):
    """目的: 「変化と矛盾」は種類（benchmark など）ではなく「何を測ったか」（単位の次元）で束ね、数値は単位をそろえて比べ、測定条件（量子化・機材）が両方に書かれて違えば矛盾でなく cond_diff にする。
    読むたびに計算するので、保存済みの主張（DB は書き換えない）も新しい規則でそのまま判定し直される。
    根拠: 2026-11-27 利用者の画面（Inkling 2 Mini の「変化と矛盾」で 26.4B・16 GB・122 tok/s・0.59 ドル・1.9x が全部「性能」で並んで互いに矛盾になった）・AGENTS.md（辞書の足し継ぎをしない）。"""
    f = lambda text, v: {"about": "Inkx", "kind": "benchmark", "text": text, "value": v}  # noqa: E731
    art(24 * 5, [("Inkx", "model")], facts=[f("パラメータ数: 26.4B", "26.4B"), f("メモリ使用量: 16 GB", "16 GB"), f("M5 Max 上で 122 tok/s を達成", "122 tok/s"), f("1 タスク 0.59 ドル", "0.59 ドル")])
    art(24 * 5 - 3, [("Inkx", "model")], facts=[f("26.4B のモデル", "26B"), f("メモリ 16.0GB で動く", "16.0GB")])
    with news_db.session() as db:
        h = kg.history(db, ent_id("Inkx"))
        by = {g["dim"]: g for g in h["series"]}
        assert {"params", "memory", "speed:llm.tps_unspecified", "money:usd"} <= set(by)      # 指標ごとに別の列
        assert h["contradictions"] == 0                                         # 26.4B と 26B・16 GB と 16.0GB は丸めの違い（同じ）
    art(24 * 5 - 4, [("Inkx", "model")], facts=[f("メモリ 20 GB が必要", "20 GB")])
    with news_db.session() as db:
        h = kg.history(db, ent_id("Inkx"))
        mem = [g for g in h["series"] if g["dim"] == "memory"][0]
        assert mem["metric"].startswith("メモリ")
        assert {e["value"] for e in mem["entries"] if e["mark"] == "contradiction"} >= {"20 GB", "16 GB"}      # 同じ指標で 16 と 20 は矛盾
    # 条件の違い: 量子化が両方に書かれて違えば cond_diff（矛盾でも変化でもない）。片方だけに書かれていれば比べる
    art(24 * 3, [("Inkq", "model")], facts=[{"about": "Inkq", "kind": "benchmark", "text": "Q4_K_M で 122 tok/s", "value": "122 tok/s"}])
    art(24 * 3 - 2, [("Inkq", "model")], facts=[{"about": "Inkq", "kind": "benchmark", "text": "Q8_0 で 80 tok/s", "value": "80 tok/s"}])
    art(24 * 3 - 4, [("Inkq", "model")], facts=[{"about": "Inkq", "kind": "benchmark", "text": "decode 60 tok/s", "value": "60 tok/s"}])
    with news_db.session() as db:
        h = kg.history(db, ent_id("Inkq"))
        marks = {e["value"]: e["mark"] for g in h["series"] for e in g["entries"]}
        assert marks["122 tok/s"] == "cond_diff" and marks["80 tok/s"] == "cond_diff" and h["cond_diffs"] >= 2
        assert next(e for g in h["series"] for e in g["entries"] if e["value"] == "122 tok/s")["cond"]["quant"]      # 画面が条件を出せる


def test_quantity_units_and_tolerance():
    """目的: 値を (次元, 基準単位の数) にそろえる。GB と MB・B と M、速さの種類。±3% の違いは同じ、v1.0 と 1.0 は矛盾にしない。
    根拠: 2026-11-27 の画面（種類だけで束ねて文字列で比べていた不具合）。"""
    q = bench.quantity
    assert q("26.4B")[:2] == ("params", 26.4) and q("500M")[:2] == ("params", 0.5)
    assert q("16 GB")[:2] == ("memory", 16.0) and q("2048 MB")[:2] == ("memory", 2.048)
    assert q("122 tok/s")[0].startswith("speed:") and q("0.59 ドル")[0] == "money:usd" and q("1.9x")[0] == "ratio" and q("32855")[0] == "number"
    assert q("26.4B, 3.4B") is None
    t = datetime(2026, 1, 1)
    e = lambda i, v, **k: {"t": t + timedelta(hours=i), "value": v, "doc_id": i, "claim_id": i, **k}  # noqa: E731
    near = [e(1, "16 GB", dim="memory", num=16.0, tol=0.03), e(2, "16.2GB", dim="memory", num=16.2, tol=0.03)]
    assert [x["mark"] for x in kg.mark_series(near)] == ["first", "same"]
    far = [e(1, "16 GB", dim="memory", num=16.0, tol=0.03), e(2, "20 GB", dim="memory", num=20.0, tol=0.03)]
    assert [x["mark"] for x in kg.mark_series(far)] == ["contradiction", "contradiction"]
    assert [x["mark"] for x in kg.mark_series([e(1, "v1.0"), e(2, "1.0")])] == ["first", "same"]


# ---- ⑦ メモ ----

def test_user_note_survives_reanalysis_and_card_rebuild(client, monkeypatch, keyed):
    """目的: 利用者のメモは AI の部分と別の表で、再分析（取り出し直し）・カードの概要の作り直しで上書きされない。保存はすぐ（PUT で即時）。長すぎれば 422。
    根拠: 利用者の指示「利用者のメモは AI の部分と分けて保存し、再分析で上書きしない・すぐ保存」・ARCHITECTURE 3 章 Note。"""
    aid = art(1, [("Nota", "tech")], facts=[{"about": "Nota", "kind": "claim", "text": "Nota は軽い", "value": ""}])
    nid = ent_id("Nota")
    r = client.put(f"/api/v1/kg/entities/{nid}/note", json={"body": "自分の考察: 後で試す"}, headers=H)
    assert r.status_code == 200 and r.json()["body"] == "自分の考察: 後で試す"
    with news_db.session() as db:
        assert db.get(EntityNote, nid).body == "自分の考察: 後で試す"      # すぐ DB にある（まとめ書きを待たない）
    writer.get().stage_extraction(aid, {"entities": [{"name": "Nota", "type": "tech"}], "facts": [], "relations": [], "model": "m", "version": "x"})
    writer.flush("t")
    with news_db.session() as db:
        db.add(EntityCard(entity_id=nid, summary="古い概要", model="m", sig="old"))
        db.commit()
    kg_job._save([({"id": nid, "sig": "new", "tries": 0}, "新しい概要です", "m2")])  # noqa: SLF001
    card = client.get(f"/api/v1/kg/entities/{nid}/card").json()
    assert card["note"]["body"] == "自分の考察: 後で試す" and card["overview"]["text"] == "新しい概要です" and card["overview"]["ai"] is True
    assert client.put(f"/api/v1/kg/entities/{nid}/note", json={"body": "x" * (kg_view.NOTE_MAX + 1)}, headers=H).status_code == 422
    assert client.put("/api/v1/kg/entities/999999/note", json={"body": "x"}, headers=H).status_code == 404


# ---- ⑧ neighbors ----

def _graph(db, n_hub_edges: int = 300):
    """ハブ 1 つ（id h）に n_hub_edges 本・その先の物に 2 段目の辺を持つ合成のグラフ。"""
    hub = Entity(key="hub", name="Hub", type="tech")
    db.add(hub)
    db.flush()
    leaves = []
    for i in range(n_hub_edges):
        e = Entity(key=f"leaf{i}", name=f"Leaf{i}", type="model" if i % 2 else "org", article_count=i)
        db.add(e)
        leaves.append(e)
    db.flush()
    for i, e in enumerate(leaves):
        db.add(Relationship(subject_id=hub.id, object_id=e.id, type="uses" if i % 3 else "supports", directed=True, status="extracted", confidence=round(0.99 - i * 0.003, 3),
                            evidence_count=1, first_at=datetime(2026, 1, 1), last_at=datetime(2026, 1, 1) + timedelta(days=i % 60)))
    for i in range(10):
        far = Entity(key=f"far{i}", name=f"Far{i}", type="tech")
        db.add(far)
        db.flush()
        db.add(Relationship(subject_id=leaves[0].id, object_id=far.id, type="part_of", directed=True, status="extracted", confidence=0.5, evidence_count=1))
    db.commit()
    return hub.id, [e.id for e in leaves]


def test_neighbors_limit_levels_and_filters(client):
    """目的: neighbors は物の id の一覧から段ごとにまとめて返し（段 1 は 1 回のまとめ取り）、辺の合計を limit で切り（truncated）、確かさの高い順に残す。辺の型・確かさ・期間・物の種類で絞れる。
    深さ 2 で、新しく出た物の先も別の段として返す。id の一覧・深さ・上限の範囲外は 422。根拠: 利用者の指示「POST /api/v1/graph/neighbors（物の id の一覧・辺の型・深さ・上限）。API は段ごとにまとめて返す」。"""
    with news_db.session() as db:
        hub, leaves = _graph(db)
    r = client.post("/api/v1/graph/neighbors", json={"ids": [hub], "limit": 50}).json()
    assert len(r["levels"]) == 1 and len(r["levels"][0]["edges"]) == 50 and r["truncated"] is True
    conf = [e["confidence"] for e in r["levels"][0]["edges"]]
    assert conf == sorted(conf, reverse=True) and len(r["levels"][0]["nodes"]) == 50 and r["seeds"][0]["id"] == hub
    only = client.post("/api/v1/graph/neighbors", json={"ids": [hub], "types": ["supports"], "limit": 500}).json()
    assert {e["type"] for e in only["levels"][0]["edges"]} == {"supports"} and only["truncated"] is False
    hi = client.post("/api/v1/graph/neighbors", json={"ids": [hub], "min_confidence": 0.9, "limit": 500}).json()
    assert 0 < len(hi["levels"][0]["edges"]) < 50 and all(e["confidence"] >= 0.9 for e in hi["levels"][0]["edges"])
    kinds = client.post("/api/v1/graph/neighbors", json={"ids": [hub], "kinds": ["model"], "limit": 500}).json()
    assert kinds["levels"][0]["nodes"] and all(n["type"] == "model" for n in kinds["levels"][0]["nodes"])
    since = client.post("/api/v1/graph/neighbors", json={"ids": [hub], "since": "2026-02-15T00:00:00Z", "limit": 500}).json()
    assert 0 < len(since["levels"][0]["edges"]) < 300
    two = client.post("/api/v1/graph/neighbors", json={"ids": [leaves[0]], "depth": 2, "limit": 500}).json()
    assert [lv["depth"] for lv in two["levels"]] == [1, 2]
    assert {n["name"] for n in two["levels"][0]["nodes"]} >= {"Hub"} | {f"Far{i}" for i in range(10)}
    assert len(two["levels"][1]["edges"]) >= 299      # ハブから他の葉へ（段 2）
    assert client.post("/api/v1/graph/neighbors", json={"ids": [], "limit": 5}).status_code == 422
    assert client.post("/api/v1/graph/neighbors", json={"ids": [hub], "depth": 9}).status_code == 422
    assert client.post("/api/v1/graph/neighbors", json={"ids": [hub], "limit": 100000}).status_code == 422
    assert client.post("/api/v1/graph/neighbors", json={"ids": list(range(1, 80))}).status_code == 422
    assert client.get("/api/v1/graph/seeds").json()["items"][0]["name"].startswith(("Leaf", "Hub"))


# ---- ⑨ 書き出し ----

def test_export_zip_contents_and_scopes(client):
    """目的: 書き出しは範囲を指定したときだけ、物ごとの .md（front matter・[[関係する物]]・根拠の URL・メモ）を zip で返す。既定は public だけ。機密の物は含めず、public の物から機密の物へのリンクも出さない。
    confidential は管理者が指定したときだけ。根拠: 利用者の指示「Obsidian の書き出し: 範囲を指定して物ごとの .md を zip で。機密の区分の範囲を選べる（既定は public だけ）」。"""
    src = "OpenAI released GPT-6.2 which replaces GPT-6.1 in the API."
    got = extract.clean({"entities": [{"name": "GPT-6.2", "type": "model"}, {"name": "GPT-6.1", "type": "model"}],
                         "relations": [{"subject": "GPT-6.2", "type": "replaces", "object": "GPT-6.1", "quote": "replaces GPT-6.1"}]}, src)
    for _ in range(3):
        art(5, [("GPT-6.2", "model"), ("GPT-6.1", "model")], relations=got["relations"],
            facts=[{"about": "GPT-6.2", "kind": "release", "text": "6.2 を公開", "value": "6.2"}], url=None)
    nid = ent_id("GPT-6.2")
    client.put(f"/api/v1/kg/entities/{nid}/note", json={"body": "メモ: 要確認"}, headers=H)
    d = client.post("/api/v1/kg/documents", json={"kind": "research", "ident": "https://private.test/r1", "title": "社内"}, headers=H).json()
    client.post("/api/v1/kg/relationships", json={"document_id": d["id"], "subject": "GPT-6.2", "object": "SecretProj", "type": "uses", "evidences": [{"quote": "q"}]}, headers=H)
    r = client.post("/api/v1/kg/export", json={"min_articles": 0, "limit": 100}, headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "model/GPT-6.2.md" in names and "model/GPT-6.1.md" in names and not any("SecretProj" in n for n in names)
    md = z.read("model/GPT-6.2.md").decode()
    assert md.startswith("---\n") and 'name: "GPT-6.2"' in md and "sensitivity: public" in md and "[[GPT-6.1]]" in md and "SecretProj" not in md
    assert "https://" in md and "メモ: 要確認" in md and "確かさ 0.9" in md and "6.2 を公開" in md
    sel = client.post("/api/v1/kg/export", json={"ids": [nid], "min_articles": 0}, headers=H)
    assert [n for n in zipfile.ZipFile(io.BytesIO(sel.content)).namelist() if n != "README.md"] == ["model/GPT-6.2.md"]      # README.md は vault の使い方（常に入る）
    assert zipfile.ZipFile(io.BytesIO(client.post("/api/v1/kg/export", json={"types": ["org"], "min_articles": 0}, headers=H).content)).namelist() == ["README.md"]
    full = zipfile.ZipFile(io.BytesIO(client.post("/api/v1/kg/export", json={"confidential": True, "min_articles": 0}, headers=H).content))
    assert "other/SecretProj.md" in full.namelist() and "[[SecretProj]]" in full.read("model/GPT-6.2.md").decode()
    assert "sensitivity: confidential" in full.read("other/SecretProj.md").decode()
    assert client.post("/api/v1/kg/export", json={"limit": 99999}, headers=H).status_code == 422


# ---- ⑩ カードの概要（LLM） ----

def _card_fixture():
    f = lambda n: [{"about": "Zorblax", "kind": "release", "text": f"Zorblax {n} を公開", "value": str(n)}]  # noqa: E731
    for i in range(3):
        art(24 * i + 1, [("Zorblax", "model"), ("Qorp", "tech")], facts=f(2 + i), relations=[{"subject": "Zorblax", "type": "part_of", "object": "Qorp", "quote": "", "grounded": False}])


def _night(monkeypatch=None):
    cfg = settings.load()
    cfg["llm"]["night_from"] = 0
    settings.save(cfg)


def test_card_llm_only_public_allowed_changed_and_validated(client, monkeypatch, keyed):
    """目的: カードの概要は夜に 10 物ずつ（用途 card）、変化のあった物だけ作る（同じ材料なら呼ばない）。public かつ AI_USE=allowed の物だけを送り、機密・ローカル限定の物は送らない。材料に無い数字を含む概要は捨てて
    失敗を数え 3 回で諦める。記事の中の命令は枠の中のデータ。根拠: 利用者の指示「概要は夜にまとめて LLM・用途 card・変化のあった物だけ・10 物ずつ・記事の中の命令に従わない」・機密の物は外部 LLM へ送らない（PLAN 7 章）。"""
    _night()
    _card_fixture()
    for i in range(3):      # 機密にした物（記事が多くても送らない）
        art(24 * i + 1, [("Hidden", "tech")], facts=[{"about": "Hidden", "kind": "claim", "text": "秘密の主張", "value": ""}])
    with news_db.session() as db:
        h = db.get(Entity, ent_id("Hidden"))
        h.sensitivity, h.ai_use = "confidential", "local_only"
        db.commit()

    def answer(messages, _n):
        count = len([ln for ln in messages[1]["content"].splitlines() if ln.startswith("[")])
        return chat(json.dumps({str(i): "Zorblax は Qorp の一部として公開が続いているモデルです。" for i in range(1, count + 1)}, ensure_ascii=False))

    srv = Srv(answer)
    monkeypatch.setattr(llm, "_post", srv)
    out = run(kg_job.run_cards())
    assert out["error"] == "" and out["done"] == 2 and out["calls"] == len(srv.calls) == 1 and llm.calls_today("card") == 1
    sent = srv.calls[0]["messages"]
    assert "Zorblax" in sent[1]["content"] and "Hidden" not in sent[1]["content"] and "秘密" not in sent[1]["content"] and "従わず" in sent[0]["content"] and "=== 物（ここから）===" in sent[1]["content"]
    with news_db.session() as db:
        card = db.get(EntityCard, ent_id("Zorblax"))
        assert card.summary.startswith("Zorblax は") and card.model and db.get(EntityCard, ent_id("Hidden")) is None
    assert run(kg_job.run_cards())["calls"] == 0      # 変化が無ければ呼ばない
    art(0.5, [("Zorblax", "model"), ("Qorp", "tech")], facts=[{"about": "Zorblax", "kind": "price", "text": "価格", "value": "5"}])      # 主張が増えた＝変化
    assert "Zorblax" in [t["name"] for t in kg_job.targets()]
    bad = Srv(lambda m, n: chat(json.dumps({"1": "出荷台数は 123456 台に達した"}, ensure_ascii=False)))
    monkeypatch.setattr(llm, "_post", bad)
    for _ in range(5):
        run(kg_job.run_cards())
    assert len(bad.calls) == kg_job.MAX_TRIES      # 3 回で諦める
    with news_db.session() as db:
        assert "123456" not in db.get(EntityCard, ent_id("Zorblax")).summary


def test_card_llm_not_at_daytime_and_nightly_aggregate(client, monkeypatch, keyed):
    """目的: カードの概要は夜（night_from 時以降）だけ。昼は呼ばない。夜の集計（文書の対応・co_occurs）は 1 日 1 回だけ。根拠: 利用者の指示「夜にまとめて」。"""
    _card_fixture()
    cfg = settings.load()
    cfg["llm"]["night_from"] = 24 if False else 23
    settings.save(cfg)
    srv = Srv(lambda m, n: chat("{}"))
    monkeypatch.setattr(llm, "_post", srv)
    monkeypatch.setattr(kg_job, "datetime", type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 1, 12, 0))}))
    assert run(kg_job.run())["cards"]["calls"] == 0 and not srv.calls
    monkeypatch.setattr(kg_job, "datetime", type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 1, 23, 30))}))
    first = run(kg_job.run())
    assert first["aggregate"] is not None and run(kg_job.run())["aggregate"] is None


def test_card_endpoint_shape(client):
    """目的: 知識カードの API は概要・要素・年表・主張と根拠・関係・変化と矛盾・メモを返し、主張には根拠（文書）が付く。根拠: 利用者の指示 4（知識カード）。"""
    aid = art(1, [("Cardy", "tech"), ("Other", "tech")], facts=[{"about": "Cardy", "kind": "release", "text": "公開", "value": "1"}],
              relations=[{"subject": "Cardy", "type": "uses", "object": "Other", "quote": "", "grounded": False}])
    c = client.get(f"/api/v1/kg/entities/{ent_id('Cardy')}/card").json()
    assert set(c) >= {"overview", "elements", "timeline", "claims", "relations", "history", "note"} and c["overview"] is None
    assert c["claims"][0]["evidences"][0]["document"]["url"].startswith("https://") and c["claims"][0]["ai_extracted"] is True
    assert c["relations"][0]["type"] == "uses" and c["relations"][0]["status"] == "inferred" and c["relations"][0]["other"]["name"] == "Other"
    assert c["timeline"][0]["id"] == aid and client.get("/api/v1/kg/entities/99999/card").status_code == 404
    assert client.get("/api/v1/kg/stats").json()["documents"] == 1
