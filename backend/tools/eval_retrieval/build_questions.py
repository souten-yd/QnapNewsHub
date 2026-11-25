"""評価の問いの集めを、評価用のハブの DB（コピー）から半自動で作る。本物の LLM は使わない（規則と雛形だけ）。

問いの文の自然さより、**正解の根拠が正しいこと**を優先する。正解は DB の構造から決める:
- fact（事実）: 物の release／price／benchmark の主張 → 正解は、その主張・根拠の文書・物。
- compare（比較）: competes の関係 A–B → 正解は、その関係・両方の物・関係の根拠の文書。
- change（変化）: replaces の関係、または日をまたぐ release の主張が複数ある物 → 正解は、その関係／主張・文書・物。
- multi（複数の文書）: 記事が 3 件以上のイベント → 正解は、イベントの主な物 2 つを両方含む記事（最大 5 件）とそのイベント。
- relation（関係）: ある物を object にした uses／implements／supports／part_of／cites の関係 → 正解は、subject の物・関係・根拠の文書・object の物。
- bridge（橋渡し。build_bridge で別に作る）: 関係（co_occurs を除く・向きなし）の最短経路が 2〜6 段の 2 物「A と B はどうつながっているか」→ 正解は、途中の物・経路の関係・その根拠の文書（経路上の全部）。
  通常の問いの正解は 1 段先までで作られ、深さを測れない。これは深さ・会合（retrieval.expand_meet）を測る物差し。端の物は名前が 1 つしか無いものだけ。
- short（3 文字未満の語。build_short で別に作る）: 題に 2 文字の漢字語（「認証」「量子」など）を持つ記事 → 「{語} について何が報じられたか」（語だけ）と「{題の別の語} の {語}」（語＋別の語）。正解は、題にその語（と別の語）を含む文書。FTS5 trigram が引けない語を測る。
- crosslingual（日本語の問い・英語の記事が正解）: 題がほぼ ASCII の記事（英語）に付いた日本語の主張 → 日本語の主張を含む問い（半分は物の名前つき、半分は名前なし）。正解は、その英語の記事・主張・物。
正解が 1 件も無い・記事の根拠が無いものは問いにしない。区分が public の物・文書だけ。
DB は読むだけ（mode=ro）。出力の JSON は利用者の実データ由来なので、リポジトリに入れない。
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

TYPES = ("fact", "compare", "change", "multi", "relation", "crosslingual", "bridge", "watch", "bench", "short")
BRIDGE_LENGTHS = (2, 3, 4, 5, 6)
TYPE_LABEL = {"fact": "事実", "compare": "比較", "change": "変化", "multi": "複数の文書", "relation": "関係", "crosslingual": "日本語の問い・英語の記事", "bridge": "橋渡し（2〜6 段）", "watch": "最近の変化（ウォッチ）", "bench": "条件つきの数値の比較", "short": "3 文字未満の語"}
GOLD_DOCS_MAX = 5
GOLD_CLAIMS_MAX = 4
_JA = re.compile(r"[぀-ヿ㐀-鿿]")
_NONASCII = re.compile(r"[^\x00-\x7f]")
REL_PHRASE = {      # 関係の型 → 「{O}」を主語にした問いの言い回し（subject が正解）
    "uses": "{o} を使っているものは何か",
    "implements": "{o} を実装しているものは何か",
    "supports": "{o} に対応しているものは何か",
    "part_of": "{o} の一部であるものは何か",
    "cites": "{o} を引用しているものは何か",
}
CLAIM_PHRASE = {
    "release": ["{x} は何を出したか", "{x} のリリースでは何が発表されたか"],
    "price": ["{x} の価格はいくらか", "{x} の料金について何が報じられたか"],
    "benchmark": ["{x} のベンチマークの結果はどうだったか", "{x} は性能比較でどう評価されたか"],
}


class Src:
    """読み取り専用の DB と、問いづくりで何度も使う対応表。"""

    def __init__(self, path: Path):
        self.c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.c.row_factory = sqlite3.Row
        q = lambda sql, *a: self.c.execute(sql, a).fetchall()  # noqa: E731
        self.ents = {r["id"]: dict(r) for r in q("SELECT id, name, type, article_count FROM entities WHERE sensitivity='public'")}
        name_count: dict[str, int] = {}
        for e in self.ents.values():
            name_count[e["name"].casefold()] = name_count.get(e["name"].casefold(), 0) + 1
        self.unique_name = {i for i, e in self.ents.items() if name_count[e["name"].casefold()] == 1 and len(e["name"]) >= 3}
        self.docs = {r["id"]: dict(r) for r in q("SELECT id, title, published_at FROM documents WHERE sensitivity='public'")}
        self.doc_of_article = {r[0]: r[1] for r in q("SELECT news_item_id, document_id FROM news_documents")}
        self.claim_ev: dict[int, list[int]] = {}
        self.rel_ev: dict[int, list[int]] = {}
        for r in q("SELECT claim_id, relationship_id, document_id FROM evidences WHERE document_id IS NOT NULL ORDER BY confidence DESC, id"):
            if r["document_id"] in self.docs:
                for table, key in ((self.claim_ev, r["claim_id"]), (self.rel_ev, r["relationship_id"])):
                    if key is not None and r["document_id"] not in table.setdefault(key, []):
                        table[key].append(r["document_id"])
        self.claims = [dict(r) for r in q("SELECT id, article_id, entity_id, kind, text FROM claims WHERE sensitivity='public' ORDER BY id")]
        self.rels = [dict(r) for r in q("SELECT id, subject_id, object_id, type, confidence FROM relationships WHERE sensitivity='public' ORDER BY id")]

    def claim_docs(self, c: dict) -> list[int]:
        docs = list(self.claim_ev.get(c["id"], []))
        d = self.doc_of_article.get(c["article_id"])
        if d in self.docs and d not in docs:
            docs.append(d)
        return docs

    def day(self, doc_id: int) -> str:
        return str(self.docs[doc_id]["published_at"] or "")[:10]


def _gold(docs=(), ents=(), claims=(), rels=(), events=()) -> dict:
    return {"docs": sorted(set(docs)), "entities": sorted(set(ents)), "claims": sorted(set(claims)), "relationships": sorted(set(rels)), "events": sorted(set(events))}


def make_fact(s: Src, rng: random.Random, n: int) -> list[dict]:
    by: dict[tuple[int, str], list[dict]] = {}
    for c in s.claims:
        if c["kind"] in CLAIM_PHRASE and c["entity_id"] in s.unique_name and s.claim_docs(c):
            by.setdefault((c["entity_id"], c["kind"]), []).append(c)
    keys = sorted(by)
    rng.shuffle(keys)
    out, used = [], set()
    for ent, kind in keys:
        if ent in used or len(out) >= n:
            continue
        used.add(ent)
        cs = by[(ent, kind)][:GOLD_CLAIMS_MAX]
        name = s.ents[ent]["name"]
        out.append({"type": "fact", "q": rng.choice(CLAIM_PHRASE[kind]).format(x=name), "keywords": [name],
                    "gold": _gold(docs=[d for c in cs for d in s.claim_docs(c)][:GOLD_DOCS_MAX * 2], ents=[ent], claims=[c["id"] for c in cs]), "anchor": {"entity": ent, "kind": kind}})
    return out


def make_compare(s: Src, rng: random.Random, n: int) -> list[dict]:
    rs = [r for r in s.rels if r["type"] == "competes" and r["subject_id"] in s.unique_name and r["object_id"] in s.unique_name and s.rel_ev.get(r["id"])]
    rng.shuffle(rs)
    out = []
    for r in rs[:n]:
        a, b = s.ents[r["subject_id"]]["name"], s.ents[r["object_id"]]["name"]
        out.append({"type": "compare", "q": rng.choice(["{a} と {b} の違いは何か", "{a} と {b} はどう比べられているか"]).format(a=a, b=b), "keywords": [a, b],
                    "gold": _gold(docs=s.rel_ev[r["id"]][:GOLD_DOCS_MAX], ents=[r["subject_id"], r["object_id"]], rels=[r["id"]]), "anchor": {"relationship": r["id"]}})
    return out


def make_change(s: Src, rng: random.Random, n: int) -> list[dict]:
    out = []
    rs = [r for r in s.rels if r["type"] == "replaces" and r["subject_id"] in s.unique_name and r["object_id"] in s.unique_name and s.rel_ev.get(r["id"])]
    rng.shuffle(rs)
    for r in rs[: n // 2]:
        a, b = s.ents[r["subject_id"]]["name"], s.ents[r["object_id"]]["name"]
        out.append({"type": "change", "q": f"{a} は {b} から何が変わったか", "keywords": [a, b],
                    "gold": _gold(docs=s.rel_ev[r["id"]][:GOLD_DOCS_MAX], ents=[r["subject_id"], r["object_id"]], rels=[r["id"]]), "anchor": {"relationship": r["id"]}})
    by: dict[int, list[dict]] = {}
    for c in s.claims:
        if c["kind"] == "release" and c["entity_id"] in s.unique_name and s.claim_docs(c):
            by.setdefault(c["entity_id"], []).append(c)
    cand = []
    for ent, cs in by.items():
        days = {s.day(s.claim_docs(c)[0]) for c in cs}
        if len(days) >= 2:
            cand.append((ent, cs))
    cand.sort(key=lambda x: x[0])
    rng.shuffle(cand)
    for ent, cs in cand:
        if len(out) >= n:
            break
        cs = sorted(cs, key=lambda c: s.day(s.claim_docs(c)[0]))
        pick, seen = [], set()
        for c in cs:      # 日ごとに 1 つずつ（日をまたいだ変化を正解にする）
            d = s.day(s.claim_docs(c)[0])
            if d not in seen:
                seen.add(d)
                pick.append(c)
        pick = pick[:GOLD_CLAIMS_MAX]
        name = s.ents[ent]["name"]
        out.append({"type": "change", "q": rng.choice(["{x} の対応は最近どう変わったか", "{x} の提供状況は以前からどう変わったか"]).format(x=name), "keywords": [name],
                    "gold": _gold(docs=[s.claim_docs(c)[0] for c in pick], ents=[ent], claims=[c["id"] for c in pick]), "anchor": {"entity": ent, "days": sorted(seen)}})
    return out[:n]


def make_multi(s: Src, rng: random.Random, n: int) -> list[dict]:
    evs = [dict(r) for r in s.c.execute("SELECT id, title, n_articles, entities FROM events WHERE n_articles >= 3 AND n_articles <= 200 ORDER BY id")]
    rng.shuffle(evs)
    out = []
    for ev in evs:
        if len(out) >= n:
            break
        try:
            top = [e for e in json.loads(ev["entities"] or "[]") if e["id"] in s.unique_name][:2]
        except (ValueError, KeyError, TypeError):
            continue
        if len(top) < 2:
            continue
        a, b = top[0]["id"], top[1]["id"]
        rows = s.c.execute(
            "SELECT nd.document_id AS d FROM event_articles ea JOIN news_documents nd ON nd.news_item_id = ea.article_id "
            "JOIN article_entities x ON x.article_id = ea.article_id AND x.entity_id = ? JOIN article_entities y ON y.article_id = ea.article_id AND y.entity_id = ? "
            "WHERE ea.event_id = ?", (a, b, ev["id"])).fetchall()
        docs = sorted({r["d"] for r in rows if r["d"] in s.docs}, key=lambda d: (s.docs[d]["published_at"] or "", d))
        if len(docs) < 3:
            continue
        na, nb = s.ents[a]["name"], s.ents[b]["name"]
        out.append({"type": "multi", "q": rng.choice(["{a} と {b} をめぐる出来事は、各記事でどう報じられているか", "{a} と {b} について、複数の記事を合わせて経緯を知りたい"]).format(a=na, b=nb),
                    "keywords": [na, nb], "gold": _gold(docs=docs[:GOLD_DOCS_MAX], ents=[a, b], events=[ev["id"]]), "anchor": {"event": ev["id"]}})
    return out


def make_relation(s: Src, rng: random.Random, n: int) -> list[dict]:
    by: dict[tuple[int, str], list[dict]] = {}
    for r in s.rels:
        if r["type"] in REL_PHRASE and r["object_id"] in s.unique_name and r["subject_id"] in s.ents and s.rel_ev.get(r["id"]):
            by.setdefault((r["object_id"], r["type"]), []).append(r)
    keys = sorted(by, key=lambda k: (-len(by[k]), k))      # 正解が複数ある組を先に
    head = [k for k in keys if len(by[k]) >= 2]
    tail = [k for k in keys if len(by[k]) < 2]
    rng.shuffle(tail)
    out, used = [], set()
    for key in head[: n // 2] + tail + head[n // 2:]:
        if len(out) >= n:
            break
        o, t = key
        if o in used:
            continue
        used.add(o)
        rs = by[key][:GOLD_CLAIMS_MAX + 1]
        name = s.ents[o]["name"]
        out.append({"type": "relation", "q": REL_PHRASE[t].format(o=name), "keywords": [name],
                    "gold": _gold(docs=[d for r in rs for d in s.rel_ev[r["id"]][:2]][:GOLD_DOCS_MAX], ents=[o] + [r["subject_id"] for r in rs], rels=[r["id"] for r in rs]),
                    "anchor": {"entity": o, "relation_type": t}})
    return out


def make_crosslingual(s: Src, rng: random.Random, n: int) -> list[dict]:
    cand = []
    for c in s.claims:
        if c["entity_id"] not in s.unique_name or not _JA.search(c["text"]) or not 6 <= len(c["text"]) <= 40 or not s.ents[c["entity_id"]]["name"].isascii():
            continue
        en = [d for d in s.claim_docs(c) if len(_NONASCII.findall(s.docs[d]["title"] or "")) < 0.1 * max(1, len(s.docs[d]["title"] or ""))]
        if en:
            cand.append((c, en))
    rng.shuffle(cand)
    out, used = [], set()
    for c, en in cand:
        if len(out) >= n:
            break
        if c["entity_id"] in used:
            continue
        used.add(c["entity_id"])
        name = s.ents[c["entity_id"]]["name"]
        text = c["text"].rstrip("。")
        named = len(out) % 2 == 0      # 偶数番は物の名前つき、奇数番は名前なし（日本語の文だけで英語の記事に届くか）
        q = f"{name} に関して「{text}」という報道は何か" if named else f"「{text}」という報道は何か"
        out.append({"type": "crosslingual", "q": q, "keywords": [name] if named else [text],
                    "gold": _gold(docs=en[:GOLD_DOCS_MAX], ents=[c["entity_id"]], claims=[c["id"]]), "anchor": {"claim": c["id"], "named": named}})
    return out


MAKERS = {"fact": make_fact, "compare": make_compare, "change": make_change, "multi": make_multi, "relation": make_relation, "crosslingual": make_crosslingual}


def make_bridge(s: Src, rng: random.Random, per_length: int) -> list[dict]:
    """関係の最短経路が長さ L（BRIDGE_LENGTHS）の 2 物を、L ごとに最大 per_length 問。共起の集計（co_occurs）は経路に使わない（つながりの根拠にならない）。"""
    adj: dict[int, dict[int, int]] = {}
    for r in s.rels:
        if r["type"] == "co_occurs" or r["subject_id"] == r["object_id"]:
            continue
        if r["subject_id"] in s.ents and r["object_id"] in s.ents:
            adj.setdefault(r["subject_id"], {}).setdefault(r["object_id"], r["id"])
            adj.setdefault(r["object_id"], {}).setdefault(r["subject_id"], r["id"])
    starts = sorted(i for i in adj if i in s.unique_name)
    rng.shuffle(starts)
    found: dict[int, list[tuple[int, int, list[int], list[int]]]] = {L: [] for L in BRIDGE_LENGTHS}
    used: dict[int, set[int]] = {L: set() for L in BRIDGE_LENGTHS}      # 長さごとに、同じ物を端に 2 度使わない
    for a in starts:
        if all(len(found[L]) >= per_length for L in BRIDGE_LENGTHS):
            break
        parent: dict[int, tuple[int, int]] = {a: (-1, -1)}
        dist = {a: 0}
        frontier = [a]
        while frontier and dist[frontier[0]] < max(BRIDGE_LENGTHS):
            nxt = []
            for u in frontier:
                for v, rid in sorted(adj[u].items()):
                    if v not in dist:
                        dist[v] = dist[u] + 1
                        parent[v] = (u, rid)
                        nxt.append(v)
            frontier = nxt
        for L in BRIDGE_LENGTHS:
            if len(found[L]) >= per_length or a in used[L]:
                continue
            cands = sorted(v for v, d in dist.items() if d == L and v in s.unique_name and v not in used[L] and v != a)
            if not cands:
                continue
            b = rng.choice(cands)
            nodes, rels, cur = [b], [], b
            while parent[cur][0] != -1:
                u, rid = parent[cur]
                rels.append(rid)
                nodes.append(u)
                cur = u
            docs = [d for rid in rels for d in s.rel_ev.get(rid, [])]
            if not docs:
                continue
            found[L].append((a, b, nodes, rels))
            used[L].update({a, b})
    out = []
    for L in BRIDGE_LENGTHS:
        for a, b, nodes, rels in found[L]:
            na, nb = s.ents[a]["name"], s.ents[b]["name"]
            docs = sorted({d for rid in rels for d in s.rel_ev.get(rid, [])})
            out.append({"type": "bridge", "length": L, "q": rng.choice(["{a} と {b} はどうつながっているか", "{a} と {b} の関係を、間の物をたどって知りたい"]).format(a=na, b=nb), "keywords": [na, nb],
                        "gold": _gold(docs=docs, ents=nodes, rels=rels), "anchor": {"a": a, "b": b, "path": nodes[::-1], "relationships": rels}})
    return out


WATCH_DAYS = 1      # 「最近」の窓（日）。runner の道具の呼び出し（since=1d）と同じ。評価のハブの主張は 2 日分しか無いので 1 日にした（長い DB なら増やす）


def make_watch(s: Src, rng: random.Random, n: int) -> list[dict]:
    """最近の変化（v0.7 の続き）: 物 E の主張のうち、窓（直近 WATCH_DAYS 日）の中の日付（stated_at。無ければ created_at）のもの＋窓の中に初めて出た関係が正解。窓の前の主張が 1 つ以上ある物だけ（「前から変わった」と言える）。"""
    cut = (datetime.now(timezone.utc) - timedelta(days=WATCH_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = s.c.execute("SELECT id, article_id, entity_id, kind, text, COALESCE(stated_at, created_at) AS at FROM claims WHERE sensitivity='public' AND entity_id IS NOT NULL").fetchall()
    by: dict[int, dict[str, list]] = {}
    for r in rows:
        if r["entity_id"] in s.unique_name:
            c = dict(r)
            if s.claim_docs(c):
                by.setdefault(r["entity_id"], {"new": [], "old": []})["new" if str(r["at"]) > cut else "old"].append(c)
    rels = {}
    for r in s.c.execute("SELECT id, subject_id, object_id, type FROM relationships WHERE sensitivity='public' AND type != 'co_occurs' AND first_at > ?", (cut,)):
        for e in (r["subject_id"], r["object_id"]):
            rels.setdefault(e, []).append(r["id"])
    cand = sorted(e for e, g in by.items() if g["new"] and g["old"] and e in s.unique_name)
    rng.shuffle(cand)
    out = []
    for e in cand[:n]:
        new = by[e]["new"][:GOLD_CLAIMS_MAX]
        name = s.ents[e]["name"]
        out.append({"type": "watch", "q": rng.choice(["{x} は直近で何が変わったか", "{x} について前回から新しく分かったことは何か"]).format(x=name), "keywords": [name],
                    "gold": _gold(docs=[s.claim_docs(c)[0] for c in new][:GOLD_DOCS_MAX], ents=[e], claims=[c["id"] for c in new], rels=rels.get(e, [])[:2]), "anchor": {"entity": e, "days": WATCH_DAYS}})
    return out


def make_bench(s: Src, rng: random.Random, n: int) -> list[dict]:
    """条件つきの数値の比較: 値のある benchmark の主張が 2 つ以上ある物 E の、その主張・根拠の文書が正解。「条件を揃えて比べたい」という問い。"""
    by: dict[int, list[dict]] = {}
    for c in s.claims:
        if c["kind"] == "benchmark" and c["entity_id"] in s.unique_name and s.claim_docs(c):
            v = s.c.execute("SELECT value FROM claims WHERE id=?", (c["id"],)).fetchone()[0]
            if v and re.search(r"\d", v):
                by.setdefault(c["entity_id"], []).append(c)
    cand = sorted(e for e, cs in by.items() if len(cs) >= 2)
    rng.shuffle(cand)
    out = []
    for e in cand[:n]:
        cs = by[e][:GOLD_CLAIMS_MAX]
        name = s.ents[e]["name"]
        out.append({"type": "bench", "q": rng.choice(["{x} の性能の数値を、測った条件ごとに比べるとどうなるか", "{x} のベンチマークは、GPU・エンジンなどの条件を揃えるとどう違うか"]).format(x=name), "keywords": [name],
                    "gold": _gold(docs=[d for c in cs for d in s.claim_docs(c)][:GOLD_DOCS_MAX * 2], ents=[e], claims=[c["id"] for c in cs]), "anchor": {"entity": e}})
    return out


TOOL_MAKERS = {"watch": make_watch, "bench": make_bench}


def build_tools(db_path: Path, per_type: int = 10, seed: int = 1) -> dict:
    """変化・比較の問い（TOOL_MAKERS）だけの問いの集め。通常の build には入れない（基準値の問いを変えない）。"""
    s = Src(db_path)
    out = []
    for t, mk in TOOL_MAKERS.items():
        for q in mk(s, random.Random(f"{seed}:{t}"), per_type):
            if q["gold"]["docs"] or q["gold"]["entities"]:
                q["id"] = f"t{len(out) + 1:03d}"
                out.append(q)
    return {"version": 1, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "seed": seed, "per_type": per_type,
            "counts": {t: sum(1 for q in out if q["type"] == t) for t in TOOL_MAKERS}, "questions": out}


def main_tools(db_path: Path, out: Path, per_type: int = 10, seed: int = 1) -> dict:
    data = build_tools(db_path, per_type, seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def build_bridge(db_path: Path, per_length: int = 20, seed: int = 1) -> dict:
    s = Src(db_path)
    qs = make_bridge(s, random.Random(f"{seed}:bridge"), per_length)
    for n, q in enumerate(qs, 1):
        q["id"] = f"b{n:03d}"
    return {"version": 1, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "seed": seed, "per_length": per_length,
            "counts": {f"L{L}": sum(1 for q in qs if q["length"] == L) for L in BRIDGE_LENGTHS}, "questions": qs}


def build(db_path: Path, per_type: int = 10, seed: int = 1) -> dict:
    s = Src(db_path)
    questions: list[dict] = []
    for t in MAKERS:
        rng = random.Random(f"{seed}:{t}")
        for q in MAKERS[t](s, rng, per_type):
            if not q["gold"]["docs"] and not q["gold"]["entities"]:
                continue
            questions.append(q)
    seen: set[str] = set()
    out = []
    for q in questions:
        if q["q"] in seen:
            continue
        seen.add(q["q"])
        q["id"] = f"q{len(out) + 1:03d}"
        out.append(q)
    return {"version": 1, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "seed": seed, "per_type": per_type,
            "counts": {t: sum(1 for q in out if q["type"] == t) for t in TYPES}, "questions": out}


def main(db_path: Path, out: Path, per_type: int = 10, seed: int = 1) -> dict:
    data = build(db_path, per_type, seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def main_bridge(db_path: Path, out: Path, per_length: int = 20, seed: int = 1) -> dict:
    data = build_bridge(db_path, per_length, seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


_KANJI2 = re.compile(r"(?<![㐀-鿿々])[一-鿿]{2}(?![㐀-鿿々])")
_ANCHOR = re.compile(r"[A-Za-z][A-Za-z0-9.+\-]{2,}|[ァ-ヿー]{3,}")
SHORT_DF = (3, 12)      # 2 文字の語の、題に出る記事の数の範囲（正解が少なすぎず多すぎない）


def make_short(s: Src, rng: random.Random, n: int) -> list[dict]:
    """3 文字未満の語の問い。n 問ずつ: ① 語だけ ② 別の語（題の ASCII の語・カタカナ 3 文字以上）＋語。正解は題が条件を満たす文書。"""
    titles = {i: d["title"] for i, d in s.docs.items()}
    df: dict[str, list[int]] = {}
    for i, t in titles.items():
        for w in set(_KANJI2.findall(t)):
            df.setdefault(w, []).append(i)
    words = sorted(w for w, ids in df.items() if SHORT_DF[0] <= len(ids) <= SHORT_DF[1] and w not in STOP_TEMPLATE)
    rng.shuffle(words)
    out: list[dict] = []
    for w in words[:n]:
        out.append({"type": "short", "q": f"{w} について何が報じられたか", "keywords": [w], "gold": _gold(docs=df[w]), "anchor": {"word": w, "form": "alone"}})
    both = 0
    for w in words[n:]:
        if both >= n:
            break
        ids = df[w]
        for i in ids:
            for a in _ANCHOR.findall(titles[i]):
                gold = [j for j in ids if a.casefold() in titles[j].casefold()]
                if 1 <= len(gold) <= 8 and len([j for j in titles if a.casefold() in titles[j].casefold()]) >= 3:
                    out.append({"type": "short", "q": f"{a} の {w}", "keywords": [a, w], "gold": _gold(docs=gold), "anchor": {"word": w, "form": "with_term", "term": a}})
                    both += 1
                    break
            else:
                continue
            break
    return out


STOP_TEMPLATE = frozenset(["関係", "報道", "条件", "記事", "性能", "数値", "発表", "複数", "経緯", "一部", "直近", "前回", "価格", "実装", "対応", "引用", "修正", "検索"])      # 雛形に使う語は内容の語にしない


def build_short(db_path: Path, per_form: int = 25, seed: int = 1) -> dict:
    qs = make_short(Src(db_path), random.Random(f"{seed}:short"), per_form)
    for n, q in enumerate(qs, 1):
        q["id"] = f"s{n:03d}"
    return {"version": 1, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "seed": seed, "per_form": per_form,
            "counts": {"short": len(qs)}, "questions": qs}


def main_short(db_path: Path, out: Path, per_form: int = 25, seed: int = 1) -> dict:
    data = build_short(db_path, per_form, seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data
