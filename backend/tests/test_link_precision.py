"""目的: 「つながりの精度」（LLM が引く検索の結果の精度と密度）の試験。外へ通信しない。
  ①つなぐ候補（リンクされていない言及）の雑音を減らす: 一般語（文書頻度が高く結びつきが少ない）・種類が合わない物・短い語・より長い名前の一部・ハイフン／CamelCase の続き・大文字小文字・提供元の定型句・取得元の名前を外す
  ②名寄せ: 規則（会社の語尾・先頭の The・v 接頭辞・末尾の .0）で確かな組は自動で寄せ、種類違い・括弧違い・日英の表記は候補止まり。寄せたら記事・主張・関係・別名・集計が付け替わり、冪等で、元に戻せる（戻した組は再び寄せない）。
  寄せた後に同じ表記が取り出されたら勝った物へ向く。機密の物は寄せない。③まとまりの名前（番号でなく、代表＋特徴の語）。
根拠: 利用者の指示（2026-11-25「つながりの精度を上げる。つなぐ候補の雑音・別名の寄せ・まとまりの名前・全体の地図の名前の出し方」）・
評価のハブの実データ（候補 1,091 件のうち無作為 30 件で誤りが 14 件。Metal・Windows のような一般語と、Claude Code の Claude のような長い名前の一部）・ARCHITECTURE 6 章（つながりの精度）。
"""
from __future__ import annotations

from sqlalchemy import func, select

from qnapnewshub import auth, entity_merge, things
from qnapnewshub import db as news_db
from qnapnewshub.db import (AnaEntity, ArticleEntity, Claim, Entity, EntityAlias, EntityMerge, Evidence, MergeCandidate, Relationship)
from tests.test_kg import H, art, ent_id


def _rel(a: str, t: str, b: str) -> dict:
    return {"subject": a, "type": t, "object": b, "quote": "", "grounded": False}


def _mentions(name: str) -> list[int]:
    with news_db.session() as db:
        things.compute_mentions(db)
        db.commit()
    with news_db.session() as db:
        from qnapnewshub.db import MentionCandidate

        return sorted(m.article_id for m in db.scalars(select(MentionCandidate).where(MentionCandidate.entity_id == ent_id(name), MentionCandidate.status == "candidate")))


# ---- ① つなぐ候補の雑音 ----

def test_mentions_drop_longer_names_continuations_case_and_boilerplate(client):
    """目的: 名前が出ていても「独立した言及」だけを候補にする。Claude Code の Claude（より長い既知の名前の一部）・Qwen3.8-Flash のようなハイフン続き・EventBridge のような CamelCase の続き・
    小文字の一般語（metal）・提供元の定型句（sponsored by）は外し、ふつうの言及は残す。根拠: 実データの無作為 30 件の誤り 14 件の大半（Apple Watch の Apple・Android Auto の Android・Claude Code の Claude）。"""
    for i in range(3):
        art(i + 1, [("Claude", "model"), ("Claude Code", "software"), ("Metal", "software"), ("Qwen3.8", "model"), ("Mocha", "software"), ("Anthropic", "org")])
    ok = art(5, [("Anthropic", "org")], title="Claude gets a long context window")
    longer = art(6, [("Anthropic", "org")], title="Claude Code ships a new release")
    hyphen = art(7, [("Anthropic", "org")], title="Qwen3.8-Flash is out")
    plain = art(8, [("Anthropic", "org")], title="Qwen3.8 benchmarks arrive")
    lower = art(9, [("Anthropic", "org")], title="Why a metal case matters for laptops")
    proper = art(10, [("Anthropic", "org")], title="Metal 4 brings new shader tools")
    sponsor = art(11, [("Anthropic", "org")], title="Weekly roundup 9to5 is sponsored by Mocha today")
    ctxless = art(12, [("Gardening", "tech")], title="Metal 4 trims the lawn edges")      # 一般の語と同じ綴りの名前は、同じ記事に出る物が合わなければ候補にしない
    assert _mentions("Claude") == [ok]
    assert _mentions("Qwen3.8") == [plain] and hyphen not in _mentions("Qwen3.8") and longer not in _mentions("Claude")
    assert _mentions("Metal") == [proper] and lower not in _mentions("Metal") and ctxless
    assert _mentions("Mocha") == [] and sponsor


def test_mentions_skip_generic_words_short_names_and_unsuitable_types(client):
    """目的: 文書頻度が高く、結びついた記事に比べて文中に出すぎる名前（一般語）・3 文字以下の英字・種類が合わない物（other・place・policy）は、候補を作らない。
    根拠: 実データの「Metal」「Windows」のような一般語（候補の雑音の主因）・利用者の指示（多くの記事に出る語・短い語・一般名詞を外す。種類が文脈に合うものだけ）。"""
    for i in range(3):
        art(i + 1, [("Lumen", "software"), ("Mac", "product"), ("Bridge", "other"), ("Paris", "place")])
    generic = [art(10 + i, [("Other", "tech")], title=f"Lumen update number {i} arrives") for i in range(25)]
    mac = art(40, [("Other", "tech")], title="Mac sales rise")
    bridge = art(41, [("Other", "tech")], title="Bridge opens to traffic")
    paris = art(42, [("Other", "tech")], title="Paris hosts a summit")
    assert generic and mac and bridge and paris
    assert _mentions("Lumen") == [] and _mentions("Mac") == [] and _mentions("Bridge") == [] and _mentions("Paris") == []
    # 結びついた記事に見合う数なら、一般語とは見なさない（同じ名前の別の物では、候補が出る）
    for i in range(3):
        art(60 + i, [("Quasar", "software")])
    q = art(70, [("Other", "tech")], title="Quasar adds a plugin system")
    assert _mentions("Quasar") == [q]


def test_mentions_skip_source_name_and_title_tail(client):
    """目的: 取得元の名前（記事を出した媒体）と題の末尾の「 | 出どころ」は、記事の中身の言及ではないので候補にしない。根拠: 実データ（Phoronix・9to5Mac・GamingOnLinux など媒体名の候補）。"""
    for i in range(3):
        art(i + 1, [("Fooblog", "org")])
    own = art(5, [("Other", "tech")], title="Big news today at Fooblog about phones", source="Fooblog")
    tail = art(6, [("Other", "tech")], title="Some report on chips - Fooblog Blog")
    real = art(7, [("Other", "tech")], title="Fooblog is acquired by a new owner", source="elsewhere")
    assert own and tail
    assert _mentions("Fooblog") == [real]


# ---- ② 名寄せ ----

def test_rule_key_unifies_corp_suffix_version_and_keeps_parens_apart():
    """目的: 規則の鍵は、会社の語尾・先頭の The・版の v 接頭辞・末尾の .0・長音符を除いて同じにする。括弧の中が違う物（Ear (3) と Ear (note)）は別物のまま。版の本体（7.4 と 7.5）は同じにしない。
    根拠: 実データの別表記（Sony と Sony Corporation・ROCm 7 と ROCm 7.0・Linux v7.4 と Linux 7.4・The New York Times）。"""
    k = entity_merge.rule_key
    assert k("Sony") == k("Sony Corporation") == k("SONY Corp.")
    assert k("ROCm 7") == k("ROCm 7.0") and k("ROCm 10.2") == k("ROCm 10.2.0") and k("ROCm 10.2") != k("ROCm 10.3")
    assert k("Linux v7.4") == k("Linux 7.4") != k("Linux 7.5")
    assert k("The New York Times") == k("New York Times")
    assert k("サーバー") == k("サーバ") and k("ソフトバンク株式会社") == k("ソフトバンク")
    assert k("Ear (3)") != k("Ear (note)") and k("Ear (3)", paren=True) == k("Ear (note)", paren=True) == k("Ear")
    assert k("a") == "" and k("") == ""


def _setup_pair() -> tuple[int, int, int]:
    """Sony（記事 3）と Sony Corporation（記事 2）。同じ関係・主張・別名を持つ。"""
    for i in range(3):
        art(i + 1, [("Sony", "org"), ("PS5", "product")], relations=[_rel("Sony", "uses", "PS5")] if i == 0 else [], facts=[{"about": "Sony", "kind": "claim", "text": "Sony announces a console", "value": ""}] if i == 1 else [])
    for i in range(2):
        art(10 + i, [("Sony Corporation", "org"), ("PS5", "product"), ("Bravia", "product")], relations=[_rel("Sony Corporation", "uses", "PS5"), _rel("Sony Corporation", "develops", "Bravia")] if i == 0 else [],
            facts=[{"about": "Sony Corporation", "kind": "claim", "text": "Corporation reports earnings", "value": ""}] if i == 1 else [])
    return ent_id("Sony"), ent_id("Sony Corporation"), ent_id("PS5")


def _state(db) -> dict:
    return {"ents": sorted(db.scalars(select(Entity.key))), "ae": sorted(db.execute(select(ArticleEntity.article_id, ArticleEntity.entity_id)).all()),
            "claims": sorted(db.execute(select(Claim.id, Claim.entity_id)).all()),
            "rels": sorted(db.execute(select(Relationship.id, Relationship.subject_id, Relationship.object_id, Relationship.type, Relationship.evidence_count)).all()),
            "ev": sorted(db.execute(select(Evidence.id, Evidence.relationship_id)).all()), "al": sorted(db.execute(select(EntityAlias.entity_id, EntityAlias.alias)).all()),
            "counts": sorted(db.execute(select(Entity.id, Entity.article_count)).all()), "ana": sorted(db.execute(select(AnaEntity.grain, AnaEntity.t, AnaEntity.entity_id, AnaEntity.n)).all())}


def test_auto_merge_moves_everything_is_idempotent_and_undoable(client):
    """目的: 規則で確かな組（Sony と Sony Corporation）は夜に自動で寄せる。記事の数・主張・関係（同じ主語・目的語・型は 1 本）・別名・集計が勝った物へ移り、2 回目は何もしない（冪等）。
    元に戻すと、物・記事の結び付き・主張・関係・根拠・別名・集計が寄せる前と完全に同じになる。戻した組は夜に再び寄せない。根拠: 利用者の指示（確かなものは自動で寄せ、関係・主張・記事の結び付きを付け替える。冪等・元に戻せる記録）。"""
    sony, corp, ps5 = _setup_pair()
    with news_db.session() as db:
        before = _state(db)
        out = entity_merge.compute_merges(db)
        db.commit()
    assert out["merged"] == 1
    with news_db.session() as db:
        assert db.get(Entity, corp) is None and db.get(Entity, sony).article_count == 5 and db.get(Entity, sony).sensitivity == "public"
        assert "Sony Corporation" in set(db.scalars(select(EntityAlias.alias).where(EntityAlias.entity_id == sony)))
        assert set(db.scalars(select(Claim.entity_id).where(Claim.entity_id.in_([sony, corp])))) == {sony}
        rels = db.execute(select(Relationship.subject_id, Relationship.object_id, Relationship.type, Relationship.evidence_count)).all()
        assert (sony, ps5, "uses", 2) in rels and len([r for r in rels if r[2] == "uses"]) == 1      # 同じ関係は 1 本・根拠は 2 つ
        assert any(r[0] == sony and r[2] == "develops" for r in rels)
        rec = db.scalar(select(EntityMerge))
        assert rec.mode == "auto" and rec.loser_id == corp and rec.undone_at is None
        assert entity_merge.compute_merges(db)["merged"] == 0      # 冪等
        assert entity_merge.merge(db, sony, corp) is None      # 寄せ済み
        db.commit()
        mid = rec.id
    with news_db.session() as db:
        assert entity_merge.undo(db, mid) == {"winner_id": sony, "loser_id": corp}
        assert entity_merge.undo(db, mid) is None      # 二度は戻せない
        db.commit()
    with news_db.session() as db:
        after = _state(db)
        assert {k: v for k, v in after.items() if k != "ana"} == {k: v for k, v in before.items() if k != "ana"}
        assert after["ana"] == before["ana"]
        entity_merge.compute_merges(db)      # 戻した組は再び寄せない
        db.commit()
        assert db.get(Entity, corp) is not None and db.scalar(select(MergeCandidate.status).where(MergeCandidate.lo_id == min(sony, corp), MergeCandidate.hi_id == max(sony, corp))) == "dismissed"


def test_extraction_after_merge_goes_to_the_winner(client):
    """目的: 寄せた後に、負けた側の表記（Sony Corporation）で新しい記事から取り出されても、新しい物を作らず勝った物（Sony）へ結ぶ。関係・主張の相手の名前も同じ。
    根拠: 寄せても次の取り出しで割れ直すと、名寄せが意味を失う（密度が戻ってしまう）。"""
    sony, corp, _ = _setup_pair()
    with news_db.session() as db:
        entity_merge.compute_merges(db)
        db.commit()
    a = art(20, [("Sony Corporation", "org"), ("Bravia", "product")], relations=[_rel("Sony Corporation", "uses", "Bravia")],
            facts=[{"about": "Sony Corporation", "kind": "claim", "text": "Sony Corporation shows a TV", "value": ""}])
    with news_db.session() as db:
        assert db.scalar(select(func.count(Entity.id)).where(Entity.name == "Sony Corporation")) == 0
        assert db.scalar(select(ArticleEntity.entity_id).where(ArticleEntity.article_id == a, ArticleEntity.entity_id == sony)) == sony
        assert db.get(Entity, sony).article_count == 6
        assert db.scalar(select(Claim.entity_id).where(Claim.article_id == a)) == sony
        assert db.scalar(select(func.count(Relationship.id)).where(Relationship.subject_id == sony, Relationship.type == "uses")) == 2


def test_uncertain_pairs_are_candidates_and_admin_decides(client):
    """目的: 種類が違う（VRChat と VRChat Inc.）・括弧を除くと同じ（Qwen3.8 と Qwen3.8 (preview)）組は自動で寄せず候補にする。確定は管理者だけ（トークンは不可）、却下は作り直しても戻らない、
    確定すると寄せた記録が残って画面から元に戻せる。根拠: 利用者の指示（迷うものは候補として画面に。管理者が確定）。"""
    for i in range(3):
        art(i + 1, [("VRChat", "software"), ("Qwen3.8", "model")])
    art(10, [("VRChat Inc.", "org"), ("Qwen3.8 (preview)", "model")])
    with news_db.session() as db:
        out = entity_merge.compute_merges(db)
        db.commit()
    assert out == {"merged": 0, "candidates": 2}
    tok = {"Authorization": "Bearer " + auth.create_token(1, "t", scope="read")["token"]}
    assert client.get("/api/v1/things/merge-candidates", headers=tok).status_code in (401, 403)
    got = client.get("/api/v1/things/merge-candidates").json()
    by = {(c["keep"]["name"], c["drop"]["name"]): c for c in got["items"]}
    v, q = by[("VRChat", "VRChat Inc.")], by[("Qwen3.8", "Qwen3.8 (preview)")]
    assert v["reason"] == "rule_type" and q["reason"] == "rule_paren" and got["total"] == 2
    assert client.post(f"/api/v1/things/merge-candidates/{v['id']}/confirm", headers=tok).status_code in (401, 403)
    done = client.post(f"/api/v1/things/merge-candidates/{v['id']}/confirm", headers=H).json()
    assert done["status"] == "merged" and ent_id("VRChat Inc.") is None
    assert client.post(f"/api/v1/things/merge-candidates/{v['id']}/confirm", headers=H).status_code == 404
    assert client.post(f"/api/v1/things/merge-candidates/{q['id']}/dismiss", headers=H).json() == {"status": "dismissed"}
    with news_db.session() as db:
        assert entity_merge.compute_merges(db)["candidates"] == 0      # 却下・確定済みは戻らない
        db.commit()
    hist = client.get("/api/v1/things/merge-candidates").json()["merges"]
    assert hist[0]["mode"] == "confirmed" and hist[0]["can_undo"]
    assert client.post(f"/api/v1/things/merges/{hist[0]['id']}/undo", headers=H).status_code == 200 and ent_id("VRChat Inc.") is not None
    assert client.post(f"/api/v1/things/merges/{hist[0]['id']}/undo", headers=H).status_code == 404


def test_cross_script_pair_is_candidate_only_and_confidential_is_never_merged(client):
    """目的: 日本語と英語の表記（同じ出来事の記事に別々に出る・同じ記事には一緒に出ない）は候補止まりで、自動では寄せない。機密の物は候補にも寄せにも入れない。
    根拠: 利用者の指示（日本語と英語の表記。手がかりは同じ記事で同時に出る・リンク先が同じ。確かなものだけ自動）・AGENTS.md の区分の決まり（機密は触らない）。"""
    from qnapnewshub import events

    a = art(1, [("Anthropic", "org")], title="Anthropic unveils new model for developers worldwide this week")
    b = art(2, [("アンソロピック", "org")], title="アンソロピックが開発者向けの新モデルを世界で発表した今週")
    c = art(3, [("Secret Corp", "org")])
    d = art(4, [("Secret Corporation", "org")])
    with news_db.session() as db:
        from qnapnewshub.db import Event, EventArticle

        ev = Event(rep_id=a, title="x", n_articles=2, n_sources=2, first_at=news_db.utcnow(), last_at=news_db.utcnow())
        db.add(ev)
        db.flush()
        db.add_all([EventArticle(article_id=a, event_id=ev.id), EventArticle(article_id=b, event_id=ev.id)])
        for name in ("Secret Corp", "Secret Corporation"):
            e = db.scalar(select(Entity).where(Entity.name == name))
            e.sensitivity, e.ai_use = "confidential", "local_only"
        db.commit()
        out = entity_merge.compute_merges(db)
        db.commit()
    assert events and c and d
    assert out["merged"] == 0 and out["candidates"] == 1
    items = client.get("/api/v1/things/merge-candidates").json()["items"]
    assert items[0]["reason"] == "cross_script" and {items[0]["keep"]["name"], items[0]["drop"]["name"]} == {"Anthropic", "アンソロピック"}
    with news_db.session() as db:
        assert entity_merge.merge(db, ent_id("Secret Corp"), ent_id("Secret Corporation")) is None


# ---- ③ まとまりの名前 ----

def test_community_names_use_representative_and_characteristic_words_not_numbers():
    """目的: まとまりの名前は「代表・特徴の語」（番号ではない）。代表の名前に入る語・小文字だけの一般語は特徴にしない。同じ名前が重ならない。同じ入力は同じ結果。
    根拠: 利用者の指示（「Things 3」のような番号をやめ、中で関係の多い Things の名前と特徴の語 2〜3 で名前を付ける）。"""
    groups = [["llama.cpp", "GGUF Format", "Vulkan Backend", "open source", "ROCm"], ["Nvidia", "CUDA Toolkit", "Jensen Huang", "Blackwell GPU"],
              ["Nvidia", "CUDA Toolkit", "Jensen Huang", "Blackwell GPU"], ["Alone"]]
    names = things.community_names(groups)
    assert names[0].startswith("llama.cpp・") and "open" not in names[0] and "source" not in names[0] and len(names[0].split("・")) >= 3
    assert len(set(names)) == len(names) and names[1] != names[2] and names == things.community_names(groups)
    assert not any(n.lower().startswith("things") or n.split("・")[-1].isdigit() for n in names)
    assert names[3] == "Alone"
