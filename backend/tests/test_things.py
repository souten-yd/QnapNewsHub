"""目的: Things（旧「物」）を LLM の別の知識体（Obsidian のような脳）として使うための機能の試験。外へ通信しない（LLM は llm._post の模擬）。
  ①カードの帯（定義・状態・確かさ・根拠の数と最新の日時）：確かさは根拠の文書の数だけで決まる ②関係の型ごとの分け方と向き（part_of の先は上位・元は下位、uses の向き、replaces の向き、仲間、矛盾）
  ③カードの LLM（定義・状態・未解決の問い・反対の見方）：材料に無い数字・英字の名前・問いでない文・不正な状態を捨てる。古い文字列だけの返事も受ける ④リンクされていない言及の候補（題・抜粋に名前があり
  article_entities に無い記事。英数字の途中は除く・確定で article_entities と記事の数が増える・却下は二度と出ない）⑤まとまりの計算（Louvain: 2 つの塊が分かれる・同じ入力は同じ結果・機密の物は入らない）
  ⑥全体の地図（上限・次数順・孤立した点は別の配列・まとまりの代表・区分）⑦context の短さ・区分（トークンは機密を読めない・無い物と読めない物を区別しない）・まとめて引く・text 形式
  ⑧書き出し（frontmatter の項目・Connections・Open Questions・MOC・README・既定は public だけ）⑨MOC（まとまり・チャンネル）。
根拠: 利用者の指示（2026-11-25「Obsidian のように脳として機能し、LLM の別知識体として使いたい」「知識の地図は、初めは全体を表示して検索したワードを抽出」「もうちょっと分厚く」）・
ARCHITECTURE 6 章（Things の脳）・区分の強制（AGENTS.md・HANDOFF 6 章: 派生データは kg.inherit・読む API は scopes_for）。
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from qnapnewshub import auth, kg_job, kg_view, llm, settings, things, writer
from qnapnewshub import db as news_db
from qnapnewshub.db import ArticleEntity, Community, Entity, EntityCard, EntityCommunity, MentionCandidate, NewsItem, Relationship
from tests.test_events import Srv, run
from tests.test_kg import H, NOW, art, ent_id
from tests.test_news_llm_prep import chat, keyed  # noqa: F401  (keyed は fixture)


def _rel(a: str, t: str, b: str) -> dict:
    return {"subject": a, "type": t, "object": b, "quote": "", "grounded": False}


def _night():
    cfg = settings.load()
    cfg["llm"]["night_from"] = 0
    settings.save(cfg)


def _link(a: str, t: str, b: str, n: int = 1):
    for i in range(n):
        art(i + 1, [(a, "tech"), (b, "tech")], relations=[_rel(a, t, b)])


# ---- ① 帯 ----

def test_brief_confidence_depends_only_on_document_count(client):
    """目的: 確かさは LLM の言い分ではなく根拠の文書（記事）の数だけ（1 件 0.5・2 件 0.75・3 件 0.88・上限 5 件で 0.97）。状態は概要を作る前は「不明」、根拠の数と最新の根拠の日時が出る。
    根拠: 利用者の指示（カードの先頭の帯: 確かさ・根拠の数と新しさ。誇張しない）。"""
    assert [things.confidence_of(n) for n in (0, 1, 2, 3, 9)] == [0.0, 0.5, 0.75, 0.88, 0.97]
    art(30, [("Briefy", "tech")])
    art(2, [("Briefy", "tech")])
    c = client.get(f"/api/v1/things/{ent_id('Briefy')}/card").json()
    b = c["brief"]
    assert b["confidence"] == 0.75 and b["evidence_count"] == 2 and b["state"] == {"key": "unknown", "label": "不明"} and b["definition"] == ""
    assert b["latest_evidence_at"].startswith(str(datetime.now(timezone.utc).year)) and b["checked_at"] == ""
    assert client.get(f"/api/v1/kg/entities/{ent_id('Briefy')}/card").json()["brief"] == b      # 古い道（/kg）でも同じ


# ---- ② 関係の分け方 ----

def test_relation_groups_split_by_type_and_direction(client):
    """目的: 関係を ExcaliBrain のように、上位（part_of の先）・下位（part_of の元）・仲間（competes・co_occurs）・使う／使われる（uses・implements の向き）・置き換える／置き換えられる・矛盾に分ける。
    向きはその物から見る。根拠: 利用者の指示（関係を型ごとに・上位下位仲間）。"""
    for other, t, flip in [("Parent", "part_of", False), ("Child", "part_of", True), ("Rival", "competes", False), ("Lib", "uses", False), ("User", "uses", True),
                           ("Old", "replaces", False), ("New", "replaces", True), ("Foe", "contradicts", False), ("Spec", "implements", False)]:
        a, b = ("Hub", other) if not flip else (other, "Hub")
        for i in range(2):
            art(i + 1, [(a, "tech"), (b, "tech")], relations=[_rel(a, t, b)])
    groups = {g["key"]: [r["other"]["name"] for r in g["items"]] for g in client.get(f"/api/v1/things/{ent_id('Hub')}/card").json()["groups"]}
    assert groups["upper"] == ["Parent"] and groups["lower"] == ["Child"] and groups["peers"] == ["Rival"]
    assert sorted(groups["uses"]) == ["Lib", "Spec"] and groups["used_by"] == ["User"]
    assert groups["replaces"] == ["Old"] and groups["replaced_by"] == ["New"] and groups["contradicts"] == ["Foe"]
    assert [g["key"] for g in client.get(f"/api/v1/things/{ent_id('Hub')}/card").json()["groups"]][:3] == ["upper", "lower", "peers"]      # 節の並びは固定


# ---- ③ カードの LLM ----

def test_card_llm_definition_state_questions_are_grounded(client, monkeypatch, keyed):
    """目的: 夜のカードの LLM に定義・状態・未解決の問い・反対の見方も作らせる。材料に無い事実を書かせない検査: 材料に無い数字・英字の名前を含む項目は捨てる・問いは「〜か？」の形だけ・
    状態は決まった 4 つ以外は「不明」・古い文字列だけの返事も概要として受ける。根拠: 利用者の指示（根拠が無いことは問いとして書く・材料に無い事実を書かない検査）。"""
    _night()
    for i in range(3):
        art(24 * i + 1, [("Zorblax", "model"), ("Qorp", "org")], facts=[{"about": "Zorblax", "kind": "release", "text": f"Zorblax {2 + i} を公開", "value": str(2 + i)}],
            relations=[_rel("Zorblax", "part_of", "Qorp")])
    good = {"definition": "Qorp の一部として公開されているモデル。", "state": "official", "summary": "Zorblax は Qorp の一部として公開が続いているモデルです。",
            "questions": ["Zorblax の性能は他と比べてどうか？", "Hallucinated の名前を含む問いか？", "公開は 99999 回目か？", "断定してしまった文です。"],
            "opposing": ["Qorp の別のモデルが競合するとの見方もある。", "Fabrix という会社が否定した。"]}
    srv = Srv(lambda m, n: chat(json.dumps({"1": good, "2": "Qorp は Zorblax を含む組織です。"}, ensure_ascii=False)))
    monkeypatch.setattr(llm, "_post", srv)
    assert run(kg_job.run_cards())["done"] >= 1
    with news_db.session() as db:
        cd = db.get(EntityCard, ent_id("Zorblax"))
        assert cd.definition.startswith("Qorp の一部") and cd.state == "official" and cd.summary.startswith("Zorblax は")
        assert json.loads(cd.questions) == ["Zorblax の性能は他と比べてどうか？"]      # 材料に無い名前・数字・問いでない文は捨てた
        assert json.loads(cd.opposing) == ["Qorp の別のモデルが競合するとの見方もある。"]
    sent = srv.calls[0]["messages"][1]["content"]
    assert "未解決" in sent or "questions" in sent and "根拠が無いことは" in sent
    c = kg_job.valid_item({"summary": "要約です。", "state": "weird", "questions": "not a list"}, "要約です")
    assert c["state"] == "unknown" and c["questions"] == [] and kg_job.valid_item("古い返事の概要です。", "古い返事の概要です")["summary"] == "古い返事の概要です。"
    card = client.get(f"/api/v1/things/{ent_id('Zorblax')}/card").json()
    assert card["brief"]["definition"].startswith("Qorp") and card["brief"]["state"]["label"] == "正式公開" and card["questions"] and card["opposing"] and card["brief"]["checked_at"]


# ---- ④ つなぐ候補 ----

def test_unlinked_mentions_candidates_confirm_dismiss(client):
    """目的: 名前・別名が題・抜粋にあるのに article_entities に無い記事を「つなぐ候補」にする。英数字の途中（Qwen が Qwenzo の中）は除く・すでにつながった記事は出さない。確定で article_entities と記事の数が増え、
    却下した候補は夜に作り直しても二度と出ない。確定・却下は管理者だけ。根拠: 利用者の指示（リンクされていない言及。押すと確定・夜にまとめて計算）。"""
    for i in range(3):
        art(i + 1, [("Mentionee", "tech")])
    miss = art(5, [("Other", "tech")], title="Mentionee gets a new release today")
    sub = art(6, [("Other", "tech")], title="Mentioneeville is unrelated")
    dis = art(7, [("Other", "tech")], title="Another note about mentionee internals")
    with news_db.session() as db:
        out = things.compute_mentions(db)
        db.commit()
    assert out["candidates"] == 2
    eid = ent_id("Mentionee")
    c = client.get(f"/api/v1/things/{eid}/card").json()
    assert sorted(m["article_id"] for m in c["mentions"]) == sorted([miss, dis]) and sub not in [m["article_id"] for m in c["mentions"]]
    assert c["mentions"][0]["matched"].lower() == "mentionee"
    tok = {"Authorization": "Bearer " + auth.create_token(1, "t", scope="read")["token"]}
    assert client.post(f"/api/v1/things/{eid}/mentions/{miss}/confirm", headers=tok).status_code in (401, 403)      # トークンは確定できない（管理者だけ）
    assert client.post(f"/api/v1/things/{eid}/mentions/{miss}/confirm", headers=H).json() == {"status": "confirmed"}
    assert client.post(f"/api/v1/things/{eid}/mentions/{dis}/dismiss", headers=H).json() == {"status": "dismissed"}
    assert client.post(f"/api/v1/things/{eid}/mentions/{dis}/dismiss", headers=H).status_code == 404      # 二度は決められない
    with news_db.session() as db:
        assert db.scalar(select(ArticleEntity.article_id).where(ArticleEntity.entity_id == eid, ArticleEntity.article_id == miss)) == miss
        assert db.get(Entity, eid).article_count == 4
        things.compute_mentions(db)
        db.commit()
        left = {(m.article_id, m.status) for m in db.scalars(select(MentionCandidate).where(MentionCandidate.entity_id == eid))}
    assert (miss, "confirmed") in left and (dis, "dismissed") in left and not any(s == "candidate" for _, s in left)      # 作り直しても却下は戻らない


# ---- ⑤ まとまり ----

def test_communities_split_two_clusters_deterministic_and_public_only(client):
    """目的: 関係から主題のまとまりを夜に計算する（Louvain）。つながりの薄い 2 つの塊は別のまとまりになり、同じ入力は同じ結果（乱数なし）。小さすぎる塊は作らない。
    機密の物はまとまりに入れない。根拠: 利用者の指示（まとまりを依存を足さずに夜に計算）・区分の強制。"""
    for grp in ("A", "B"):
        names = [f"{grp}{i}" for i in range(5)]
        for i in range(5):
            for j in range(i + 1, 5):
                _link(names[i], "uses", names[j], 1)
    _link("A0", "uses", "B0", 1)      # 塊をつなぐ細い橋
    _link("Lone1", "uses", "Lone2", 1)      # 小さすぎる（2 点）
    with news_db.session() as db:
        e = db.get(Entity, ent_id("B4"))
        e.sensitivity, e.ai_use = "confidential", "local_only"
        db.commit()
    first = _communities()
    again = _communities()
    assert first == again
    groups = list(first.values())
    big = [g for g in groups if len(g) >= 4]
    assert len(big) == 2 and {n[0] for n in big[0]} != {n[0] for n in big[1]} and all(len({n[0] for n in g}) == 1 for g in big)
    assert all("Lone1" not in g for g in groups) and not any("B4" in g for g in groups)
    assert things.louvain([(1, 2, 1.0), (2, 3, 1.0), (1, 3, 1.0), (4, 5, 1.0), (5, 6, 1.0), (4, 6, 1.0)]) == things.louvain(
        [(4, 6, 1.0), (1, 3, 1.0), (5, 6, 1.0), (2, 3, 1.0), (4, 5, 1.0), (1, 2, 1.0)])


def _communities() -> dict[int, list[str]]:
    with news_db.session() as db:
        things.compute_communities(db)
        db.commit()
        names = {e.id: e.name for e in db.scalars(select(Entity))}
        out: dict[int, list[str]] = {}
        for eid, cid in db.execute(select(EntityCommunity.entity_id, EntityCommunity.community_id)):
            out.setdefault(cid, []).append(names[eid])
        return {k: sorted(v) for k, v in sorted(out.items())}


# ---- ⑥ 全体の地図 ----

def test_overview_limit_representatives_isolated_and_scope(client):
    """目的: 全体の地図の API は、次数の多い主要な点を上限まで返し、まとまりの代表を付け、孤立した点は別の配列で返す。上限を超えれば truncated。機密の物・線は既定で出ない。
    根拠: 利用者の指示（最初に全体: サーバーが主要な点と代表を返す。上限 1,500・孤立した点の表示の切り替え）・区分の強制。"""
    for i in range(1, 7):
        _link("Core", "uses", f"Leaf{i}", 1)
    art(1, [("Alone", "tech")])
    _link("Secret", "uses", "Core", 1)
    with news_db.session() as db:
        e = db.get(Entity, ent_id("Secret"))
        e.sensitivity, e.ai_use = "confidential", "local_only"
        db.execute(Relationship.__table__.update().where(Relationship.subject_id == e.id).values(sensitivity="confidential"))
        things.compute_communities(db)
        db.commit()
    o = client.get("/api/v1/things/map/overview?limit=10").json()
    names = [n["name"] for n in o["nodes"]]
    assert names[0] == "Core" and "Secret" not in names and "Alone" not in names and "Alone" in [n["name"] for n in o["isolated"]]
    assert o["nodes"][0]["degree"] == 6 and all(len(set(e) & {"source", "target", "type"}) == 3 for e in o["edges"]) and len(o["edges"]) == 6
    assert o["communities"] and o["communities"][0]["top"][0]["name"] == "Core" and o["nodes"][0]["community"] == o["communities"][0]["id"]
    small = client.get("/api/v1/things/map/overview?limit=10").json()
    assert small["totals"]["with_relations"] == 7 and small["truncated"] is False
    assert client.get("/api/v1/things/map/overview?limit=10&isolated=0").json()["isolated"] == []
    capped = client.get("/api/v1/things/map/overview?limit=10").json()
    assert capped["limit"] == 10 and client.get("/api/v1/things/map/overview?limit=5").status_code == 422
    full = client.get("/api/v1/things/map/overview?limit=10&confidential=true").json()
    assert "Secret" in [n["name"] for n in full["nodes"]]
    cm = client.get(f"/api/v1/things/map/community/{o['communities'][0]['id']}").json()
    assert cm["name"] == "Core" and len(cm["nodes"]) == 7 and client.get("/api/v1/things/map/community/9999").status_code == 404


def test_overview_truncated_when_more_nodes_than_limit(client):
    """目的: 関係のある点が上限より多いと truncated を立てる（上限は 10 点以上）。根拠: 上限 1,500 の設計（切ったことを黙らない）。"""
    for i in range(12):
        _link(f"X{i}", "uses", f"Y{i}", 1)
    o = client.get("/api/v1/things/map/overview?limit=10").json()
    assert o["truncated"] is True and len(o["nodes"]) == 10 and o["totals"]["with_relations"] == 24


# ---- ⑦ context ----

def test_context_is_short_scoped_and_batchable(client):
    """目的: LLM の脳の API は、1 行の定義・frontmatter のような項目・型つきの関係（相手の定義つき）・主な事実と根拠の URL・未解決の問いを短く返す。空の項目は出さない。関係は上限の中で節ごとに配る。
    機密の物は、管理者でない読み取りトークンには 404（無い物と区別しない）。まとめて引く POST は読めない id を missing に。text 形式は JSON より短い。
    根拠: 利用者の指示（context のトークン節約・区分の強制は既存の read_user・link の範囲）。"""
    for i in range(3):
        art(i + 1, [("Brain", "model"), ("Part", "tech"), ("Peer", "model"), ("Dep", "tech")], facts=[{"about": "Brain", "kind": "benchmark", "text": "R9700 で 25 tok/s", "value": "25"}],
            relations=[_rel("Brain", "part_of", "Part"), _rel("Brain", "competes", "Peer"), _rel("Brain", "uses", "Dep")])
    with news_db.session() as db:
        db.add(EntityCard(entity_id=ent_id("Part"), summary="", definition="Brain の部品となる技術。", state="official", questions="[]", opposing="[]"))
        db.add(EntityCard(entity_id=ent_id("Brain"), summary="Brain は話題のモデル。", definition="推論に使うモデル。", state="experimental", questions=json.dumps(["長い文脈で劣化するか？"], ensure_ascii=False), opposing="[]"))
        db.commit()
    bid = ent_id("Brain")
    c = client.get(f"/api/v1/things/{bid}/context").json()
    assert c["definition"] == "推論に使うモデル。" and c["status"] == "experimental" and c["type"] == "model" and c["confidence"] == 0.88 and c["updated"]
    assert c["sources"][0].startswith("https://") and c["open_questions"] == ["長い文脈で劣化するか？"] and "opposing" not in c and "aliases" not in c
    assert {k: [r["name"] for r in v] for k, v in c["relations"].items()} == {"upper": ["Part"], "peers": ["Peer"], "uses": ["Dep"]}
    assert c["relations"]["upper"][0]["def"] == "Brain の部品となる技術。" and c["facts"][0] == {"text": "R9700 で 25 tok/s", "value": "25", "src": [c["sources"][0]]} or c["facts"][0]["value"] == "25"
    one = client.get(f"/api/v1/things/{bid}/context?max_relations=1").json()
    assert sum(len(v) for v in one["relations"].values()) == 1
    text = client.get(f"/api/v1/things/{bid}/context?format=text")
    assert text.headers["content-type"].startswith("text/plain") and text.text.startswith("---\nid: ") and "## 上位" in text.text and "## Open Questions" in text.text
    assert len(text.text) < len(json.dumps(c, ensure_ascii=False)) and len(text.text) < 1500
    batch = client.post("/api/v1/things/context", json={"ids": [bid, 987654]}, headers=H).json()
    assert [i["name"] for i in batch["items"]] == ["Brain"] and batch["missing"] == [987654]
    assert client.post("/api/v1/things/context", json={"ids": list(range(1, 30))}, headers=H).status_code == 422
    assert client.get(f"/api/v1/things/lookup?q=Brai").json()["items"][0]["definition"] == "推論に使うモデル。"
    # 機密
    with news_db.session() as db:
        e = db.get(Entity, ent_id("Peer"))
        e.sensitivity, e.ai_use = "confidential", "local_only"
        db.commit()
    pid = ent_id("Peer")
    assert client.get(f"/api/v1/things/{pid}/context").status_code == 404 and "Peer" not in json.dumps(client.get(f"/api/v1/things/{bid}/context").json())
    assert client.get(f"/api/v1/things/{pid}/context?confidential=true").status_code == 200
    token = auth.create_token(1, "t", scope="read")["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/v1/things/{bid}/context", headers=bearer).status_code == 200
    assert client.get(f"/api/v1/things/{pid}/context?confidential=true", headers=bearer).status_code == 403      # 範囲の外は黙って落とさず 403
    assert client.post("/api/v1/things/context", json={"ids": [pid]}, headers=bearer).json() == {"items": [], "missing": [pid]}


# ---- ⑧ 書き出し ----

def test_export_has_frontmatter_connections_questions_moc_readme(client):
    """目的: Obsidian の書き出しが、frontmatter（type・status・aliases・confidence・updated・sources）・`## Connections`（型ごと）・`## Open Questions`・MOC のファイル・vault の使い方の README を含む。
    既定は public だけ。根拠: 利用者の指示（Obsidian の書き出しを厚く）。"""
    for grp in ("P", "Q"):
        for i in range(3):
            for j in range(i + 1, 3):
                _link(f"{grp}{i}", "uses", f"{grp}{j}", 3)
    with news_db.session() as db:
        db.add(EntityCard(entity_id=ent_id("P0"), summary="", definition="P0 の定義。", state="deprecated", questions=json.dumps(["P0 は置き換えられるか？"], ensure_ascii=False), opposing="[]"))
        things.compute_communities(db)
        db.commit()
    r = client.post("/api/v1/kg/export", json={"min_articles": 0, "limit": 100}, headers=H)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    md = z.read("tech/P0.md").decode()
    for key in ("type: tech", "status: deprecated", "aliases: ", "confidence: ", "updated: ", "sources: [\"https://"):
        assert key in md
    assert "## Connections" in md and "### 使う" in md and "[[P1]]" in md and "## Open Questions" in md and "P0 は置き換えられるか？" in md and "> P0 の定義。" in md
    assert "README.md" in names and "規約" in z.read("README.md").decode() and "Open Questions" in z.read("README.md").decode()
    moc = [n for n in names if n.startswith("_MOC/")]
    assert len(moc) >= 1 and "[[P0]]" in z.read(next(n for n in moc if "P" in n.split("/")[1] or True)).decode() + "".join(z.read(n).decode() for n in moc)
    assert "sensitivity: public" in md and not any("confidential" in z.read(n).decode().split("---")[1] for n in names if n.endswith(".md") and n.startswith("tech/"))


# ---- ⑨ MOC ----

def test_moc_community_and_channel(client):
    """目的: 主題の入口（MOC）は、まとまりごと・チャンネルごとに、主要な Things・最近の出来事・未解決の問いを返す。根拠: 利用者の指示（まとまりごと・チャンネルごとの入口ページ）。"""
    for i in range(3):
        for j in range(i + 1, 3):
            _link(f"M{i}", "uses", f"M{j}", 3)
    with news_db.session() as db:
        db.add(EntityCard(entity_id=ent_id("M0"), summary="", definition="M0 の定義。", state="official", questions=json.dumps(["M0 の将来は？"], ensure_ascii=False), opposing="[]"))
        things.compute_communities(db)
        db.commit()
    idx = client.get("/api/v1/things/moc").json()
    assert idx["communities"] and idx["channels"]
    cid = idx["communities"][0]["id"]
    m = client.get(f"/api/v1/things/moc/community/{cid}").json()
    assert m["kind"] == "community" and {t["name"] for t in m["things"]} == {"M0", "M1", "M2"} and m["questions"] == [{"entity_id": ent_id("M0"), "entity": "M0", "question": "M0 の将来は？"}]
    assert "events" in m and client.get("/api/v1/things/moc/community/9999").status_code == 404
    ch = idx["channels"][0]["id"]
    mc = client.get(f"/api/v1/things/moc/channel/{ch}").json()
    assert mc["kind"] == "channel" and set(mc) >= {"things", "events", "questions", "title"} and client.get("/api/v1/things/moc/channel/nope").status_code == 404


def test_things_alias_routes_and_nightly_aggregate_computes(client):
    """目的: /things は /entities の別名（同じ中身）。夜の集計（kg_job.aggregate）が、まとまりとつなぐ候補も作る。根拠: 利用者の指示（名前は画面だけ変える・API の道は互換・夜にまとめて計算）。"""
    for i in range(3):
        art(i + 1, [("Aliasy", "tech"), ("Buddy", "tech")], relations=[_rel("Aliasy", "uses", "Buddy")])
        art(i + 1, [("Third", "tech"), ("Buddy", "tech")], relations=[_rel("Third", "uses", "Buddy")])
    a = client.get("/api/v1/things?q=Aliasy").json()
    assert a == client.get("/api/v1/entities?q=Aliasy").json() and a["items"][0]["name"] == "Aliasy"
    assert client.get(f"/api/v1/things/{a['items'][0]['id']}").json() == client.get(f"/api/v1/entities/{a['items'][0]['id']}").json()
    out = kg_job.aggregate()
    assert out["communities"] >= 1 and "mention_candidates" in out
    with news_db.session() as db:
        assert db.scalar(select(func.count(Community.id))) >= 1
