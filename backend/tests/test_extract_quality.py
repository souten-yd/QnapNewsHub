"""目的: 「Things の取り出しの精度」（2026-11-25・feat/extract-quality）の試験。外へ通信しない（LLM は llm._post を差し替える）。
  ①機械の除外（thing_filter）: 取得元・媒体名・一般語・断片・版だけの名前を物にしない。実在の物（OpenAI・Claude Code・題に出る GitHub）は残す
  ②寄せ（entity_merge.compute_variants）: org/repo と repo・版つきと本体・大きさ違いを本体へ。版は別名＋release の主張。model の版・本体の無いものは候補止まり。元に戻すと付けた主張も消える
  ③関係の整理（extract.relation_ok・kg.sweep_relations）: made_by・develops の型・型ごとの主語と目的語の種類・型 other は原文の引用が要る・プラットフォームを相手にしない
  ④根拠の段階（exact・paraphrase・none）: 関係と主張の確かさと主張の並び・日付の既定が記事の公開日
  ⑤gleaning（prep.run_gleaning）: 物が 0〜1 個の記事だけ・既出の物を示す・前の取り出しを消さず足す・1 日の上限・済みの印
  ⑥夜の掃除（retire_sweep・sweep_relations）。
根拠: 評価の置き場「2026-11-25 Things の抽出とカードの調査と測定」（50 記事の測定: 物の適合 79%・関係の 71% が誤り・根拠の一致 33%・名寄せの残り org/repo 19 組・版つき 79 組）。
誤りの例（XDA・AI coding agent・browser・Mac tool・financial firm・MCP supports Claude・Claude uses Anthropic・RAC-BL1GYN part_of サンワサプライ・Scrapling cites GitHub・sgl-project/sglang）をそのまま入れた。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from qnapnewshub import db as news_db
from qnapnewshub import entity_merge, extract, kg, kg_view, llm, prep, thing_filter, writer
from qnapnewshub.db import ArticleEntity, Claim, Entity, EntityAlias, EntityMerge, EntityRetirement, Evidence, NewsItem, Relationship
from tests.test_events import Srv, run  # noqa: F401
from tests.test_kg import art, ent_id
from tests.test_news_llm_prep import Server, chat, keyed  # noqa: F401  (keyed は fixture)

SRC = "XDA"


# ---- ① 機械の除外 ----

@pytest.mark.parametrize("name,kind", [("XDA", "org"), ("Android Authority", "org"), ("Towards Data Science", "org"), ("Infoseek", "org"), ("The Register", "org"),
                                       ("AI coding agent", "other"), ("browser", "other"), ("Mac tool", "other"), ("financial firm", "other"), ("algorithm", "other"),
                                       ("LLM", "tech"), ("取締役会", "other"), ("マネジメントコンソール", "other"), ("不安定な爆薬", "other"), ("リーガルAI", "other"),
                                       ("ランサムウェア", "other"), ("v0.5.22", "other"), ("3.13", "other"), ("A", "other")])
def test_measured_errors_are_rejected(name, kind):
    """目的: 50 記事の測定で誤りだった物（取得元・一般語・断片・版だけ）を、記事の文脈なしでも落とす。"""
    assert thing_filter.reject(name, kind, context=False)


@pytest.mark.parametrize("name,kind", [("OpenAI", "org"), ("Claude Code", "software"), ("Qwen 3.5 27B", "model"), ("Firefox 161.0", "software"), ("Jensen Huang", "person"),
                                       ("GitHub Actions", "software"), ("RTX 5050", "product"), ("熊本市", "place"), ("JSConf JP 2026", "other"), ("MCP", "tech"), ("Python 3.13", "software"),
                                       ("sgl-project/sglang", "software"), ("Mac Studio", "product"), ("Cloud Run", "product"), ("サンワサプライ", "org")])
def test_real_things_are_kept(name, kind):
    """目的: 実在の物（固有名）を落とさない。一般語の判定が広がりすぎない歯止め。"""
    assert thing_filter.reject(name, kind, context=False) == ""


def test_source_and_platform_depend_on_the_article():
    """目的: 出どころと同名の物・プラットフォームは、その記事の題に出るときだけ残す（題の末尾の「 - 出どころ」は題の内容でない）。実在の物（OpenAI）が出どころでも、題に出れば残す。"""
    assert thing_filter.reject("XDA Developers", "org", source=SRC, title="Something happened - XDA Developers") == "media"
    assert thing_filter.reject("Phoronix", "org", source="Phoronix", title="Mesa 26 lands") == "media"
    assert thing_filter.reject("OpenAI", "org", source="OpenAI News", title="Weekly roundup - OpenAI News") == ""      # 組織は媒体らしい語が無ければ残す
    assert thing_filter.reject("Foo Watch", "org", source="Foo Watch", title="Weekly roundup") == "source"
    assert thing_filter.reject("OpenAI", "org", source="OpenAI News", title="OpenAI ships a new model - OpenAI News") == ""
    assert thing_filter.reject("GitHub", "org", source="Hacker News", title="Scrapling is a fast scraper") == "platform"
    assert thing_filter.reject("GitHub", "org", source="Hacker News", title="GitHub changes pricing") == ""
    assert thing_filter.source_cores("forum.level1techs.com") >= {"level1techs"}


def test_registration_skips_excluded_names_and_their_facts_and_relations(client):
    """目的: 物の登録のとき、除外する名前は物にならず、その物についての主張・関係も作らない（別名で再び物が作られない）。"""
    facts = [{"about": "XDA", "kind": "claim", "text": "XDA が報じた", "value": ""}, {"about": "Foo Engine", "kind": "claim", "text": "新しい版が出た", "value": ""}]
    rels = [{"subject": "Foo Engine", "type": "cites", "object": "XDA", "quote": "", "grounded": False}, {"subject": "Foo Engine", "type": "uses", "object": "GitHub", "quote": "", "grounded": False}]
    aid = art(1, [("XDA", "org"), ("AI coding agent", "other"), ("Foo Engine", "software"), ("GitHub", "org")], facts=facts, relations=rels, title="Foo Engine gets a rewrite", source=SRC)
    with news_db.session() as db:
        assert sorted(db.scalars(select(Entity.name))) == ["Foo Engine"]
        assert [c.text for c in db.scalars(select(Claim))] == ["新しい版が出た"]
        assert db.scalar(select(func.count(Relationship.id))) == 0
        assert db.scalar(select(func.count(NewsItem.id))) == 1 and aid


# ---- ③ 関係の整理 ----

@pytest.mark.parametrize("t,s,o,ok", [("made_by", "product", "org", True), ("made_by", "org", "product", False), ("develops", "org", "software", True), ("develops", "product", "org", False),
                                      ("uses", "model", "org", False), ("uses", "software", "tech", True), ("supports", "tech", "software", True), ("supports", "software", "org", False),
                                      ("part_of", "product", "org", False), ("part_of", "tech", "software", True), ("part_of", "org", "org", True), ("part_of", "person", "org", False),
                                      ("cites", "software", "place", False), ("uses", "model", "other", True), ("other", "org", "place", True)])
def test_relation_domain_range(t, s, o, ok):
    """目的: 型ごとの主語・目的語の種類（定義域・値域）。測定の誤り: Claude uses Anthropic（モデルが組織を使う）・RAC-BL1GYN part_of サンワサプライ（製造元は made_by）。種類が不明（other）の端は通す。"""
    assert extract.relation_ok(t, s, o) is ok


def test_registration_drops_domain_violations_and_keeps_made_by(client):
    """目的: 型に合わない関係は登録のとき捨て、made_by・develops は入る。platform の相手・型 other の推測は入らない。"""
    r = lambda s, t, o: {"subject": s, "type": t, "object": o, "quote": "", "grounded": False}  # noqa: E731
    art(1, [("Claude", "model"), ("Anthropic", "org"), ("RAC-BL1GYN", "product"), ("Sanwa", "org"), ("Scrapling", "software"), ("GitHub", "org"), ("Metal", "tech"), ("llama.cpp", "software")],
        relations=[r("Claude", "uses", "Anthropic"), r("Claude", "made_by", "Anthropic"), r("RAC-BL1GYN", "part_of", "Sanwa"), r("Sanwa", "develops", "RAC-BL1GYN"),
                   r("Scrapling", "cites", "GitHub")], title="Claude news about Scrapling, Metal and llama.cpp from Anthropic, RAC-BL1GYN and Sanwa")
    with news_db.session() as db:
        got = {(db.get(Entity, x.subject_id).name, x.type, db.get(Entity, x.object_id).name) for x in db.scalars(select(Relationship))}
    assert got == {("Claude", "made_by", "Anthropic"), ("Sanwa", "develops", "RAC-BL1GYN")}


def test_other_relation_is_never_made():
    """目的: 型 other（内容の空な関係。測定で正しく具体的なものは 0 件）は作らない。引用が原文に一致していても落とす。型が合う関係は残る。"""
    src = "Del Casher used a wah-wah pedal on stage"
    rows = [{"subject": "Del Casher", "type": "other", "object": "wah-wah pedal", "quote": "used a wah-wah pedal"}, {"subject": "Del Casher", "type": "uses", "object": "wah-wah pedal", "quote": "used a wah-wah pedal"}]
    got = extract.clean_relations(rows, src)
    assert [(r["type"], r["tier"]) for r in got] == [("uses", "exact")]


# ---- ④ 根拠の段階 ----

def test_quote_tier_and_confidence_order():
    """目的: 引用が原文にそのまま＝exact・言い換え（文字の重なりが大きい）または値が原文にある＝paraphrase・無し＝none。確かさは 一致 > 言い換え > 無し。"""
    src = "Mistral AI announced Magistral Small 1.2, a 24B reasoning model, on Tuesday."
    assert extract.quote_tier("announced Magistral Small 1.2", src) == "exact"
    assert extract.quote_tier("announced Magistral Small 1.3 a 24B", src) == "paraphrase"
    assert extract.quote_tier("", src, value="24B") == "paraphrase"
    assert extract.quote_tier("totally different words here", src) == "none"
    assert kg.TIER_CONF["exact"] > kg.TIER_CONF["paraphrase"] > kg.TIER_CONF["none"] and kg.TIER_CONF["none"] > 0
    assert kg.INFERRED_CAP > kg.TIER_CONF["paraphrase"]


def test_relation_status_and_confidence_follow_the_tier(client):
    """目的: exact の関係だけが extracted（0.7）。言い換え・無しは推測（inferred）で、確かさは 0.3・0.15。"""
    mk = lambda o, t, g: {"subject": "Hub", "type": "uses", "object": o, "quote": "q" if g else "", "grounded": g == "exact", "tier": t}  # noqa: E731
    art(1, [("Hub", "tech"), ("Aaa", "tech"), ("Bbb", "tech"), ("Ccc", "tech")], relations=[mk("Aaa", "exact", "exact"), mk("Bbb", "paraphrase", ""), mk("Ccc", "none", "")])
    with news_db.session() as db:
        got = {db.get(Entity, r.object_id).name: (r.status, r.confidence) for r in db.scalars(select(Relationship))}
    assert got == {"Aaa": ("extracted", 0.7), "Bbb": ("inferred", 0.3), "Ccc": ("inferred", 0.15)}


def test_claims_get_tier_stated_at_and_are_sorted_by_tier(client):
    """目的: 主張の根拠に段階（引用が原文に一致・言い換え・無し）を付け、日付の既定を記事の公開日にし、カードの主張は 一致 > 言い換え > 無し の順に並べる。"""
    f = lambda text, tier, quote="": {"about": "Gadget", "kind": "claim", "text": text, "value": "", "quote": quote, "tier": tier}  # noqa: E731
    aid = art(3, [("Gadget", "product")], facts=[f("none", "none"), f("exact", "exact", "原文の引用です"), f("para", "paraphrase")])
    with news_db.session() as db:
        pub = db.get(NewsItem, aid).published_at
        claims = {c.text: c for c in db.scalars(select(Claim))}
        assert all(c.stated_at is not None and c.stated_at.replace(tzinfo=None) == pub.replace(tzinfo=None) for c in claims.values())
        tiers = {db.get(Claim, ev.claim_id).text: (ev.tier, ev.grounded, ev.confidence) for ev in db.scalars(select(Evidence).where(Evidence.claim_id.isnot(None)))}
        assert tiers == {"exact": ("exact", True, 0.7), "para": ("paraphrase", False, 0.3), "none": ("none", False, 0.15)}
        got = kg_view._claims_with_evidence(db, ent_id("Gadget"), ["public"])
        assert [c["text"] for c in got] == ["exact", "para", "none"] and got[0]["stated_at"].startswith(str(pub.year))


# ---- ② 寄せ ----

def _night() -> dict:
    with news_db.session() as db:
        out = entity_merge.compute_merges(db)
        db.commit()
    return out


def test_repo_and_version_variants_merge_into_the_base_with_alias_and_release_claim(client):
    """目的: org/repo と repo・版つきと本体・大きさ違いを本体へ自動で寄せる（測定: org/repo 19 組・版つき 79 組）。版は別名（負けた名前）と release の主張（値＝版）として残る。冪等。"""
    for i in range(3):
        art(i + 1, [("sglang", "software"), ("Firefox", "software"), ("Qwen 3.5", "model")])
    art(10, [("sgl-project/sglang", "software"), ("sglang v0.5.16", "software"), ("Firefox 161.0", "software"), ("Qwen 3.5 27B", "model")])
    out = _night()
    assert out["merged"] == 4
    with news_db.session() as db:
        names = set(db.scalars(select(Entity.name)))
        assert names == {"sglang", "Firefox", "Qwen 3.5"}
        sg = db.scalar(select(Entity).where(Entity.name == "sglang"))
        assert {"sgl-project/sglang", "sglang v0.5.16"} <= set(db.scalars(select(EntityAlias.alias).where(EntityAlias.entity_id == sg.id)))
        assert sg.article_count == 4
        rel = {(c.value, c.kind, c.ai_extracted) for c in db.scalars(select(Claim).where(Claim.model == "rule:variant"))}
        assert {("0.5.16", "release", False), ("161.0", "release", False), ("27B", "release", False)} == rel
        assert entity_merge.compute_merges(db)["merged"] == 0
        db.commit()


def test_model_version_and_missing_base_are_candidates_only(client):
    """目的: model の版（Claude 3.5 と Claude）は別の物なので自動で寄せない。本体が無い版どうしは候補。迷うものを勝手に寄せない。"""
    for i in range(3):
        art(i + 1, [("Claude", "model")])
    art(10, [("Claude 3.5", "model"), ("Foo 1.0", "software"), ("Foo 1.1", "software")])
    out = _night()
    assert out["merged"] == 0 and out["candidates"] >= 2
    with news_db.session() as db:
        assert db.scalar(select(func.count(Entity.id)).where(Entity.name.in_(["Claude", "Claude 3.5", "Foo 1.0", "Foo 1.1"]))) == 4


def test_variant_merge_is_undoable_and_removes_the_release_claim(client):
    """目的: 版の寄せは entity_merges に残り、戻すと物・別名・記事の結び付きが元に戻り、付けた release の主張も消える。戻した組は夜に再び寄せない。"""
    for i in range(2):
        art(i + 1, [("Firefox", "software")])
    art(10, [("Firefox 161.0", "software")], facts=[{"about": "Firefox 161.0", "kind": "release", "text": "公開", "value": "161.0"}])
    assert _night()["merged"] == 1
    with news_db.session() as db:
        rec = db.scalar(select(EntityMerge))
        assert rec.reason == "variant" and rec.mode == "auto"
        assert db.scalar(select(func.count(Claim.id)).where(Claim.model == "rule:variant")) == 1
        assert entity_merge.undo(db, rec.id) is not None
        db.commit()
    with news_db.session() as db:
        assert db.scalar(select(func.count(Claim.id)).where(Claim.model == "rule:variant")) == 0
        assert db.scalar(select(func.count(Claim.id)).where(Claim.text == "公開")) == 1
        assert {e.name: e.article_count for e in db.scalars(select(Entity))} == {"Firefox": 2, "Firefox 161.0": 1}
    assert _night()["merged"] == 0


# ---- ⑥ 夜の掃除 ----

def test_nightly_sweep_retires_media_and_drops_bad_relations(client):
    """目的: 既にある物のうち文脈なしで落とせるもの（媒体名・一般語）を外し（記録は entity_retirements）、型に合わない関係・空疎な関係を捨てる。人のメモのある物は外さない。"""
    with news_db.session() as db:
        now = news_db.utcnow()
        e = {n: Entity(key=extract.normalize_key(n), name=n, type=t, first_seen=now, last_seen=now) for n, t in
             [("Phoronix", "org"), ("browser", "other"), ("Mesa", "software"), ("Intel", "org"), ("Notey browser", "other")]}
        db.add_all(e.values())
        db.flush()
        e["Notey browser"].name = "tool"      # メモ付き
        from qnapnewshub.db import EntityNote

        db.add(EntityNote(entity_id=e["Notey browser"].id, body="大事", updated_by="u"))
        db.add_all([Relationship(subject_id=e["Mesa"].id, object_id=e["Intel"].id, type="uses", status="extracted"), Relationship(subject_id=e["Mesa"].id, object_id=e["Intel"].id, type="other", status="inferred")])
        db.commit()
    from qnapnewshub import kg_job

    out = kg_job.aggregate()
    assert out["retired"] == 2 and out["relations_dropped"] == 2      # Phoronix・browser／uses（software が org を使う）・other（推測）
    with news_db.session() as db:
        assert sorted(db.scalars(select(Entity.name))) == ["Intel", "Mesa", "tool"]
        assert sorted(db.scalars(select(EntityRetirement.reason))) == ["generic", "media"]
        assert thing_filter.summary(db)["retired"] == 2


# ---- ⑤ gleaning ----

def _mark_extracted(ids: list[int]) -> None:
    with news_db.session() as db:
        for i in ids:
            db.get(NewsItem, i).extract_v = 1
        db.commit()


def test_gleaning_asks_only_thin_articles_adds_without_deleting_and_marks_done(monkeypatch, keyed):
    """目的: 物が 0〜1 個の記事だけ「漏れている物は」と問い直す。既出の物の名前を示し、返事の追加の物・事実・関係を、前の取り出しを消さずに足す。済みの印（extract_v=2）で 2 度は問わない。
    物が 2 個以上の記事は問わない。根拠: 測定の取りこぼし約 17%（物ゼロの記事が 12%）・GraphRAG の gleaning（全記事でなく限定する）。"""
    thin0 = art(1, [], facts=[], title="Foo Engine 2.0 ships with a new renderer")
    thin1 = art(2, [("Alpha Lib", "software")], facts=[{"about": "Alpha Lib", "kind": "claim", "text": "既存の主張", "value": ""}], title="Beta Tool and Alpha Lib interop")
    rich = art(3, [("Aa Bb", "tech"), ("Cc Dd", "tech")], title="Rich article about Aa Bb and Cc Dd")
    _mark_extracted([thin0, thin1, rich])

    def reply(messages, n):
        body = messages[1]["content"]
        assert "既出: Alpha Lib" in body and "既出: なし" in body and "Rich article" not in body
        return chat(json.dumps({"1": {"entities": [{"name": "Foo Engine", "type": "software"}, {"name": "browser", "type": "other"}],
                                      "facts": [{"about": "Foo Engine", "kind": "release", "text": "2.0 が公開された", "value": "2.0", "quote": "Foo Engine 2.0"}],
                                      "relations": [{"subject": "Foo Engine", "type": "uses", "object": "Alpha Lib", "quote": "Foo Engine 2.0 ships"}]},
                                "2": {"entities": [{"name": "Beta Tool", "type": "product"}], "facts": [], "relations": []}}, ensure_ascii=False))
    server = Server(reply)
    monkeypatch.setattr(llm, "_post", server)
    out = run(prep.run_gleaning())
    assert out == {"done": 2, "calls": 1, "error": ""} and llm.calls_today("prep") == 1 and llm.extra_today("glean") == 1
    writer.flush("t")
    with news_db.session() as db:
        names = lambda aid: sorted(db.scalars(select(Entity.name).join(ArticleEntity, ArticleEntity.entity_id == Entity.id).where(ArticleEntity.article_id == aid)))  # noqa: E731
        assert names(thin0) == ["Alpha Lib", "Foo Engine"] and names(thin1) == ["Alpha Lib", "Beta Tool"]      # browser は一般語で捨てる・既出は残る
        assert db.scalar(select(func.count(Claim.id)).where(Claim.text == "既存の主張")) == 1      # 前の取り出しを消さない
        assert db.scalar(select(func.count(Claim.id)).where(Claim.text == "2.0 が公開された")) == 1
        got = [(db.get(Entity, r.subject_id).name, r.type, db.get(Entity, r.object_id).name, r.status) for r in db.scalars(select(Relationship))]
        assert got == [("Foo Engine", "uses", "Alpha Lib", "extracted")]      # 引用が原文（題）にそのまま出る＝exact
        assert {db.get(NewsItem, i).extract_v for i in (thin0, thin1)} == {prep.GLEAN_DONE} and db.get(NewsItem, rich).extract_v == 1
    assert run(prep.run_gleaning())["calls"] == 0      # 済んだ記事は問わない（対象が無い）


def test_gleaning_stops_at_the_daily_cap_and_when_disabled(monkeypatch, keyed):
    """目的: 1 日の上限（GLEAN_DAILY_MAX 回）・設定が切のときは呼び出し 0 回。割り当て（prep）を使い切っても止まる（エラーを返す）。"""
    ids = [art(i + 1, [], title=f"Thin article number {i} about Zork{i}") for i in range(3)]
    _mark_extracted(ids)
    server = Server(lambda messages, n: chat(json.dumps({"1": {"entities": [], "facts": [], "relations": []}})))
    monkeypatch.setattr(llm, "_post", server)
    for _ in range(prep.GLEAN_DAILY_MAX):
        llm.count_extra("glean")
    assert run(prep.run_gleaning()) == {"done": 0, "calls": 0, "error": ""} and server.calls == []
    from qnapnewshub import settings

    cfg = settings.load()
    cfg["llm"]["extract_enabled"] = False
    settings.save(cfg)
    assert run(prep.run_gleaning())["calls"] == 0


def test_prompt_version_and_rules_text():
    """目的: プロンプトの版を上げた（ex4）。関係は「原文が明示しているものだけ・引用を必ず」・取得元と一般語を入れない例・題の数を fact に、が指示に入っている。型 made_by・develops が型の一覧にある。"""
    assert extract.PROMPT_VERSION == "ex4" and {"made_by", "develops"} <= set(extract.REL_TYPES) <= set(kg.REL_TYPES)
    text = prep.build_prompt([{"title": "T", "source": "S", "summary": "x", "id": 1}])
    assert "原文が明示している関係だけ" in text and "quote は必ず付け" in text and "出どころ・媒体の名前" in text and "題に出る数・価格・日付は必ず fact" in text


def test_sweep_retires_source_named_media_but_keeps_real_things(client):
    """目的: 出どころの名前が物になったもの（結び付いた記事がすべてその出どころ・題に名前が出ない・媒体らしい語を含む組織）は夜に外す。
    実在の物（他の出どころの記事にも出る・人・ソフト・発表する側の組織）は外さない。根拠: 測定で取得元と同名の物が 119。"""
    for i in range(2):
        art(i + 1, [("Foo Watch", "org"), ("Acme Corp", "org"), ("Bar Lib", "software"), ("Shared Weekly News", "org")], source="FooWatch" if i == 0 else "AcmeCorp", title=f"A story {i} on chips")
    # 同名の出どころ（ドメインのような名前）の記事だけに出る物
    with news_db.session() as db:
        db.execute(NewsItem.__table__.update().values(source="Foo Watch"), execution_options={})
        db.commit()
    from qnapnewshub import kg_job

    assert kg_job.aggregate()["retired"] >= 1
    with news_db.session() as db:
        names = set(db.scalars(select(Entity.name)))
    assert "Foo Watch" not in names and {"Acme Corp", "Bar Lib"} <= names


@pytest.mark.parametrize("name", ["24日", "2日", "第 4回", "第 1〜3回", "12月1日～2027年1月30日", "30.7 weeks", "2026-11-23"])
def test_dates_and_quantities_are_fragments(name):
    """目的: 日付・回数・量（測定の標本の「24日」「第 4回」「30.7 weeks」「12月1日～2027年1月30日」）は物にしない。"""
    assert thing_filter.reject(name, "other", context=False) == "generic"


# ---- ex4: 一般名詞・カテゴリ・説明的な句 ----

@pytest.mark.parametrize("name", ["AI Subscription", "Gifts for Dads", "tiny house", "theoretical physicist", "144Hz AMOLED display", "handheld game system", "Steam library",
                                  "10,000mAh power bank", "screened porch", "AI fan videos", "FYP", "UI designer"])
def test_generic_phrases_from_real_llm_run_are_rejected(name):
    """目的: 本物の LLM（ex3）が実記事 20 件から出した一般名詞・カテゴリ・役職・説明的な句・仕様の句を、文脈なしで外す。根拠: 2026-11-25 の実取り出しで Things に混ざった誤り。"""
    assert thing_filter.reject(name, "other", context=False)


@pytest.mark.parametrize("name,kind", [("Flipper", "product"), ("Amazon Inspector", "product"), ("Nothing Phone (4a) Pro", "product"), ("Sony ULT Field 3", "product"),
                                       ("Busy Bar", "product"), ("UGREEN", "org"), ("Steam Deck", "product"), ("Best Buy", "org"), ("bell hooks", "person")])
def test_real_names_survive_generic_phrase_rules(name, kind):
    """目的: 新しい規則（小文字の句・説明的な語尾・仕様・役職）が実在の固有名を巻き込まない歯止め。正しい例（実取り出しで正しかった物）を固定する。"""
    assert thing_filter.reject(name, kind, context=False) == ""


def test_title_paraphrase_is_rejected_only_when_mark_is_weak():
    """目的: 題を言い換えただけの句（名前が題の大半で固有名の印が弱い）は外し、数字・記号のある固有名や短い名前は題と同じでも残す。"""
    assert thing_filter.reject("How to cook a rice", "other", title="How to cook a rice - Blog") == "title"
    assert thing_filter.reject("Why small teams ship faster", "other", title="Why small teams ship faster") == "title"
    assert thing_filter.reject("small teams ship faster", "other", title="Why small teams ship faster") == "lower"
    assert thing_filter.reject("Small teams Ship faster", "other", title="Small teams Ship faster?") == "title"
    assert thing_filter.reject("Busy Bar", "product", title="Busy Bar") == ""
    assert thing_filter.reject("Nothing Phone (4a) Pro", "product", title="Nothing Phone (4a) Pro") == ""


def test_spec_phrase_fact_moves_to_main_thing_and_generic_things_are_swept(client):
    """目的: 仕様の句（144Hz AMOLED display）は物にならず、その主張は記事の主な物の数値の事実として残る。説明的な句の物は夜の掃除で外れ、実在の物は残る（理由は entity_retirements）。"""
    facts = [{"about": "144Hz AMOLED display", "kind": "other", "text": "144Hz の画面を持つ", "value": "144Hz"}]
    art(1, [("Flipper Zero", "product"), ("144Hz AMOLED display", "other"), ("tiny house", "other")], facts=facts, title="Flipper Zero gets a screen")
    with news_db.session() as db:
        assert sorted(db.scalars(select(Entity.name))) == ["Flipper Zero"]
        claim = db.scalars(select(Claim)).one()
        assert claim.text == "144Hz の画面を持つ" and claim.entity_id == db.scalar(select(Entity.id))
        db.add(Entity(name="Steam library", key="steamlibrary", type="other", sensitivity="public"))
        db.commit()
    from qnapnewshub import kg_job

    kg_job.aggregate()
    with news_db.session() as db:
        assert sorted(db.scalars(select(Entity.name))) == ["Flipper Zero"]
        assert "phrase" in set(db.scalars(select(EntityRetirement.reason)))
