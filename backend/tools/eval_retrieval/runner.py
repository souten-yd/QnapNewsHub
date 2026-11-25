"""5 つの測る道を、同じ問いで、既存の API（ASGI を直接）だけで流して点数にする。LLM は呼ばない・外へ通信しない。

道（ranking は参照 `doc:` `ent:` `clm:` `rel:` `evt:` の並び）:
  ① fulltext … GET /news/items?q=<キーワード>（今の全文検索。問いに付けたキーワードで引く＝LLM が打つ検索語の代わり）
  ② seeds    … POST /retrieve/seeds（問いの文そのまま。文書・物・主張・イベントを 1 件ずつ交互に並べる）
  ③ neighbors … ②の上位 3 つの物から POST /graph/neighbors（深さ 1。seed_scores・q を渡して辺の score をもらう）→ 辺（rel）・相手の物・辺の根拠の文書（POST /retrieve/evidence）。
                ②の並びへ score で差し込む（fuse_scored。両方から来た参照は点数を足す）
  ④ ppr      … ②の上位 3 つの物から POST /graph/ppr → 物・上位の物の主張と根拠の文書（/retrieve/evidence）。②と RRF で合わせる
  ③' meet    … ②の上位 3 つの物から POST /graph/expand（上位の種から別々に展開し、2 つ以上の種から届いた物・辺へ加点＝会合。深さは自動〔普通の問いは 1・物を結ぶ問いだけ最大 5〕か --meet-depth で固定）。③と同じに差し込む
  ⑥ tools    … ⑤に、上位の物の GET /watch/changes（since=WATCH_DAYS 日）と GET /metrics/compare を足す（LLM が「最近の変化」「条件つきの数値」を道具で引く形）。主張・根拠の文書・関係・イベントの参照を足し、文脈にその text を足す
  ⑤ context  … ③④を合わせ、上位の物の POST /things/context（LLM に渡す文脈）。文脈から取れる主張・関係・文書の参照も足す
④⑤の合わせ方 RRF は 1/(10+順位)。時間は、その道に必要な呼び出しを順に流した合計（キャッシュは呼び出しごとに捨てる）。
文脈の密度: 渡した文脈（①〜④は上位 10 件を文にしたもの、⑤は物の文脈＋上位 5 文書の題）に入っていた正解の参照の数 ÷ 見積りのトークン数 × 1000。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import metrics

PATHS = ("fulltext", "seeds", "neighbors", "meet", "ppr", "context", "tools")
PATH_LABEL = {"fulltext": "① 全文だけ", "seeds": "② 種", "neighbors": "③ 種＋周り 1 段", "meet": "③' 種＋会合の展開", "ppr": "④ 種＋局所 PPR", "context": "⑤ 物の文脈", "tools": "⑥ ⑤＋変化・比較の道具"}
H = {"X-Requested-With": "NewsHub"}
SEED_LIMIT = 10
EXPAND_FROM = 3       # 種の物のうち、周りへ広げる数
EXPAND_MAX = 30
CONTEXT_ENTITIES = 6
TOOL_ENTITIES = 2      # 道具を引く物の数。種（②）の上位の物だけ（LLM が lookup で選んだ物に道具を引く形）
WATCH_DAYS = 1      # build_questions.WATCH_DAYS と同じ
CONTEXT_TOP = 10
RRF_K = 10
RANK_DAMP = 0.85      # 種の点数の、同じ種類の中の順位ごとの割引（周りの点数との比較用）


def gold_refs(q: dict) -> set[str]:
    g = q["gold"]
    return ({f"doc:{i}" for i in g.get("docs", [])} | {f"ent:{i}" for i in g.get("entities", [])} | {f"clm:{i}" for i in g.get("claims", [])}
            | {f"rel:{i}" for i in g.get("relationships", [])} | {f"evt:{i}" for i in g.get("events", [])})


def rrf(*lists: list[str]) -> list[str]:
    score: dict[str, float] = {}
    first: dict[str, int] = {}
    for lst in lists:
        for rank, r in enumerate(metrics.dedupe(lst), start=1):
            score[r] = score.get(r, 0.0) + 1.0 / (RRF_K + rank)
            first.setdefault(r, len(first))
    return sorted(score, key=lambda r: (-score[r], first[r]))


def interleave(*lists: list[str]) -> list[str]:
    out, i = [], 0
    while any(i < len(x) for x in lists):
        out += [x[i] for x in lists if i < len(x)]
        i += 1
    return metrics.dedupe(out)


def fuse_scored(base: list[str], base_scores: dict[str, float], extra: dict[str, float]) -> list[str]:
    """種の並び（base。種類を交互に並べた順）を崩さずに、周り（extra: 参照 → 点数）を点数で差し込む。
    種の有効な点数は、並びの先頭から見た最小値（並びが点数の降順になるよう丸める）。周りは種より必ず低い点数なので、元の種の下にだけ入り、
    元の種より低い点数の種（並びの後ろ）は追い抜ける。同点は種が先。種に既にある参照は周りから足さない。"""
    eff, cur = [], 1.0
    for r in base:
        cur = min(cur, base_scores.get(r, 0.0))
        eff.append((r, cur))
    seen = set(base)
    rest = sorted(((r, sc) for r, sc in extra.items() if r not in seen), key=lambda t: -t[1])
    out, j = [], 0
    for r, sc in eff:
        while j < len(rest) and rest[j][1] > sc:
            out.append(rest[j][0])
            j += 1
        out.append(r)
    out += [r for r, _ in rest[j:]]
    return out


class Lookup:
    """DB（読むだけ）の対応表: 記事 → 文書・URL → 文書・主張の本文 → 主張・関係の（物, 相手, 型）→ 関係。"""

    def __init__(self, db_path: Path):
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.doc_of_article = dict(c.execute("SELECT news_item_id, document_id FROM news_documents"))
        self.doc_of_url = {u: i for i, u in c.execute("SELECT id, url FROM documents WHERE url != ''")}
        self.claim_of = {(e, t[:120]): i for i, e, t in c.execute("SELECT id, entity_id, text FROM claims")}
        self.rel_of: dict[tuple[int, int, str], int] = {}
        for i, s, o, t in c.execute("SELECT id, subject_id, object_id, type FROM relationships"):
            self.rel_of[(s, o, t)] = i
            self.rel_of[(o, s, t)] = i
        self.rel_ids = {i for (i,) in c.execute("SELECT id FROM relationships")}
        c.close()


class Run:
    def __init__(self, client, lookup: Lookup, meet_depth: int | None = None):
        self.c = client
        self.lk = lookup
        self.meet_depth = meet_depth      # None＝自動（サーバーが問いで決める）
        self.meet_info: dict = {}
        self.text: dict[str, str] = {}      # 参照 → 文脈に出す 1 行

    def call(self, method: str, url: str, **kw):
        """1 回の呼び出し。キャッシュを捨てて時間を測る。(JSON, ミリ秒)。"""
        from qnapnewshub import rcache

        rcache.reset()
        t = time.perf_counter()
        r = self.c.request(method, "/api/v1" + url, headers=H, **kw)
        ms = (time.perf_counter() - t) * 1000
        if r.status_code != 200:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:200]}")
        return r.json(), ms

    # ---- ① ----
    def fulltext(self, q: dict) -> tuple[list[str], float]:
        r, ms = self.call("GET", "/news/items", params={"view": "latest", "q": " ".join(q["keywords"]), "limit": 20})
        refs = []
        for it in r["items"]:
            d = self.lk.doc_of_article.get(it["id"])
            if d is not None:
                ref = f"doc:{d}"
                refs.append(ref)
                self.text[ref] = f"{it['title']}\n{(it.get('summary') or '')[:200]}"
        return metrics.dedupe(refs), ms

    # ---- ② ----
    def seeds(self, q: dict) -> tuple[dict, float]:
        r, ms = self.call("POST", "/retrieve/seeds", json={"q": q["q"], "limit": SEED_LIMIT})
        docs, ents, evs, clms = [], [], [], []
        scores: dict[str, float] = {}
        kind_n: dict[str, int] = {}
        for it in r["items"]:
            if it["kind"] == "document" and it.get("id") is not None:
                ref = f"doc:{it['id']}"
                docs.append(ref)
                self.text[ref] = f"{it['title']}\n{(it.get('position') or {}).get('snippet', '')[:200]}"
            elif it["kind"] == "entity":
                ref = f"ent:{it['id']}"
                ents.append(ref)
                self.text[ref] = f"{it['name']} ({it['type']})"
            elif it["kind"] == "event":
                ref = f"evt:{it['id']}"
                evs.append(ref)
                self.text[ref] = f"{it['title']} ({it.get('n_articles', 0)} 件)"
            elif it["kind"] == "claim":
                ref = f"clm:{it['id']}"
                clms.append(ref)
                self.text[ref] = f"{(it.get('entity') or {}).get('name', '')}: {it['text']}"
            else:
                continue
            kind_n[it["kind"]] = kind_n.get(it["kind"], 0) + 1
            scores[ref] = it["score"] * RANK_DAMP ** (kind_n[it["kind"]] - 1)      # 種類ごとの点数は最上位が 1 に規格化されている（質が違っても 1 に近い）ので、種類内の順位で割り引いて周りと比べる
        return {"list": interleave(docs, ents, clms, evs), "scores": scores, "ent_ids": [int(e.split(":")[1]) for e in ents],
                "ent_scores": [it["score"] for it in r["items"] if it["kind"] == "entity"]}, ms

    def _evidence_docs(self, body: dict) -> tuple[dict, float]:
        return self.call("POST", "/retrieve/evidence", json=body)

    # ---- ③ ----
    def neighbors(self, ent_ids: list[int], ent_scores: list[float], question: str) -> tuple[dict[str, float], float]:
        """周りの参照 → 点数（サーバーが辺・物に付ける score。根拠の文書は辺の点数の 0.9 倍）。点数の大きい順に EXPAND_MAX 件。"""
        ids = ent_ids[:EXPAND_FROM]
        if not ids:
            return {}, 0.0
        r, ms = self.call("POST", "/graph/neighbors", json={"ids": ids, "depth": 1, "limit": 40, "order": "weight", "hub_k": 10, "seed_scores": ent_scores[:EXPAND_FROM], "q": question})
        names = {n["id"]: n["name"] for lv in r["levels"] for n in lv["nodes"]} | {s["id"]: s["name"] for s in r["seeds"]}
        edges = sorted((e for lv in r["levels"] for e in lv["edges"]), key=lambda e: -(e.get("score") or 0))[:EXPAND_MAX]
        rel_ids = [e["id"] for e in edges if e["id"] in self.lk.rel_ids]
        ev, ms2 = self._evidence_docs({"relationships": rel_ids[:20], "per_item": 2}) if rel_ids else ({"relationships": {}}, 0.0)
        out: dict[str, float] = {}

        def put(ref: str, sc: float) -> None:
            if sc > out.get(ref, -1.0):
                out[ref] = sc

        for e in edges:
            other = e["target"] if e["source"] in ids else e["source"]
            sc = e.get("score") or 0.0
            if e["id"] in self.lk.rel_ids:
                ref = f"rel:{e['id']}"
                put(ref, sc)
                self.text[ref] = f"{names.get(e['source'], e['source'])} -{e['type']}-> {names.get(e['target'], e['target'])}"
            put(f"ent:{other}", sc)
            self.text.setdefault(f"ent:{other}", names.get(other, str(other)))
            for evd in (ev["relationships"].get(str(e["id"])) or {}).get("evidences", []):
                ref = f"doc:{evd['document']['id']}"
                put(ref, sc * 0.6)
                self.text.setdefault(ref, evd["document"]["title"])
        top = sorted(out.items(), key=lambda t: -t[1])[:EXPAND_MAX]
        return dict(top), ms + ms2

    # ---- ③' ----
    def meet(self, ent_ids: list[int], ent_scores: list[float], question: str) -> tuple[dict[str, float], float]:
        """/graph/expand（会合の展開）の周りの参照 → 点数。物は物の点数・辺は辺の点数（両端の物にも辺の点数）・根拠の文書は辺の点数の 0.6 倍。"""
        ids = ent_ids[:EXPAND_FROM]
        if not ids:
            return {}, 0.0
        body: dict = {"ids": ids, "seed_scores": ent_scores[:EXPAND_FROM], "q": question}
        if self.meet_depth:
            body["depth"] = self.meet_depth
        r, ms = self.call("POST", "/graph/expand", json=body)
        self.meet_info = {"depth": r["depth"], "kind": r["plan"]["kind"], "meet": r["meet"], "n_nodes": len(r["nodes"]), "n_edges": len(r["edges"])}
        names = {n["id"]: n["name"] for n in r["nodes"]}
        edges = sorted(r["edges"], key=lambda e: -e["score"])[:EXPAND_MAX]
        rel_ids = [e["id"] for e in edges if e["id"] in self.lk.rel_ids]
        ev, ms2 = self._evidence_docs({"relationships": rel_ids[:20], "per_item": 2}) if rel_ids else ({"relationships": {}}, 0.0)
        out: dict[str, float] = {}

        def put(ref: str, sc: float) -> None:
            if sc > out.get(ref, -1.0):
                out[ref] = sc

        for e in edges:
            sc = e["score"]
            if e["id"] in self.lk.rel_ids:
                ref = f"rel:{e['id']}"
                put(ref, sc)
                self.text[ref] = f"{names.get(e['source'], e['source'])} -{e['type']}-> {names.get(e['target'], e['target'])}"
            for end in [x for x in (e["source"], e["target"]) if x not in ids] or [e["target"]]:      # 種でない端（どちらも種なら ③ と同じく目的語）
                put(f"ent:{end}", sc)
                self.text.setdefault(f"ent:{end}", names.get(end, str(end)))
            for evd in (ev["relationships"].get(str(e["id"])) or {}).get("evidences", []):
                ref = f"doc:{evd['document']['id']}"
                put(ref, sc * 0.6)
                self.text.setdefault(ref, evd["document"]["title"])
        for n in r["nodes"]:      # 辺の後に（同点では辺の相手が先。③ と同じ並び）。種が他の種から届いた会合の点（加点つき）もここで入る
            put(f"ent:{n['id']}", n["score"])
            self.text.setdefault(f"ent:{n['id']}", n["name"])
        return dict(sorted(out.items(), key=lambda t: -t[1])[:EXPAND_MAX]), ms + ms2

    # ---- ④ ----
    def ppr(self, ent_ids: list[int], scores: list[float]) -> tuple[list[str], float]:
        ids = ent_ids[:EXPAND_FROM]
        if not ids:
            return [], 0.0
        r, ms = self.call("POST", "/graph/ppr", json={"seeds": ids, "weights": [max(s, 0.01) for s in scores[:EXPAND_FROM]], "top": 20})
        top = [it for it in r["items"] if not it.get("is_seed")]
        for it in top:
            self.text.setdefault(f"ent:{it['id']}", f"{it['name']} ({it['type']})")
        ev, ms2 = self._evidence_docs({"entities": [it["id"] for it in top[:5]], "per_item": 2}) if top else ({"entities": {}}, 0.0)
        out: list[str] = []
        for i, it in enumerate(top):
            out.append(f"ent:{it['id']}")
            for cl in (ev["entities"].get(str(it["id"])) or {}).get("claims", []):
                cref = f"clm:{cl['claim_id']}"
                out.append(cref)
                self.text.setdefault(cref, cl["text"])
                for evd in cl["evidences"]:
                    ref = f"doc:{evd['document']['id']}"
                    out.append(ref)
                    self.text.setdefault(ref, evd["document"]["title"])
        return metrics.dedupe(out)[:EXPAND_MAX], ms + ms2

    # ---- ⑤ ----
    def context(self, fused: list[str]) -> tuple[list[str], str, float]:
        ids = [int(r.split(":")[1]) for r in fused if r.startswith("ent:")][:CONTEXT_ENTITIES]
        if not ids:
            return [], "", 0.0
        body = {"ids": ids, "max_relations": 8, "max_facts": 3}
        txt, ms = self._text_call(body)
        js, _ = self.call("POST", "/things/context", json={**body, "format": "json"})
        refs: list[str] = []
        for it in js["items"]:
            e = it["id"]
            refs.append(f"ent:{e}")
            for f in it.get("facts", []):
                c = self.lk.claim_of.get((e, f["text"][:120]))
                if c:
                    refs.append(f"clm:{c}")
                for u in f.get("src", []):
                    if u in self.lk.doc_of_url:
                        refs.append(f"doc:{self.lk.doc_of_url[u]}")
            for grp in it.get("relations", {}).values():
                for row in grp:
                    r_id = self.lk.rel_of.get((e, row["id"], row["rel"]))
                    refs.append(f"ent:{row['id']}")
                    if r_id:
                        refs.append(f"rel:{r_id}")
            for u in it.get("sources", []):
                if u in self.lk.doc_of_url:
                    refs.append(f"doc:{self.lk.doc_of_url[u]}")
        return metrics.dedupe(refs), txt, ms

    def _text_call(self, body: dict) -> tuple[str, float]:
        from qnapnewshub import rcache

        rcache.reset()
        t = time.perf_counter()
        r = self.c.post("/api/v1/things/context", headers=H, json={**body, "format": "text"})
        ms = (time.perf_counter() - t) * 1000
        if r.status_code != 200:
            raise RuntimeError(f"things/context -> {r.status_code}")
        return r.text, ms

    # ---- ⑥ ----
    def tools(self, ent_ids: list[int]) -> tuple[list[str], str, float]:
        """上位 TOOL_ENTITIES 個の物について、変化の要約（直近 WATCH_DAYS 日）と条件つきの数値の比較を引く。参照 clm・doc・rel・evt・ent と、LLM に渡す text。"""
        ids = ent_ids[:TOOL_ENTITIES]
        if not ids:
            return [], "", 0.0
        spec = ",".join(map(str, ids))
        ch, ms1 = self.call("GET", "/watch/changes", params={"thing": spec, "since": f"{WATCH_DAYS}d"})
        cp, ms2 = self.call("GET", "/metrics/compare", params={"thing": spec})
        refs: list[str] = []
        for it in ch["items"]:
            e = it["thing"]["id"]
            refs.append(f"ent:{e}")
            for c in it["claims"]:
                refs.append(f"clm:{c['id']}")
                if c.get("doc"):
                    refs.append(f"doc:{c['doc']}")
            refs += [f"rel:{r['id']}" for r in it["relations"]] + [f"evt:{x['id']}" for x in it["events"]]
        for t in cp["things"]:
            for g in t["groups"]:
                for p in g["points"]:
                    if p.get("claim_id"):
                        refs.append(f"clm:{p['claim_id']}")
                    if p.get("doc"):
                        refs.append(f"doc:{p['doc']}")
        from qnapnewshub import bench, watch

        text = watch.text(ch) + "\n" + bench.compare_text(cp)
        return metrics.dedupe(refs), text, ms1 + ms2

    def render(self, refs: list[str]) -> str:
        return "\n".join(self.text.get(r, r) for r in refs)


def run_question(run: Run, q: dict) -> dict[str, dict]:
    """1 つの問いを 5 つの道で流して、道ごとの点数（metrics.score_one ＋ ms）を返す。"""
    gold = gold_refs(q)
    out: dict[str, dict] = {}

    def done(path: str, ranked: list[str], ms: float, ctx_refs: set[str] | None = None, ctx_text: str | None = None):
        top = metrics.dedupe(ranked)[:CONTEXT_TOP]
        text = run.render(top) if ctx_text is None else ctx_text
        s = metrics.score_one(ranked, gold, ctx_refs, metrics.est_tokens(text))
        s["ms"] = ms
        s["n"] = len(metrics.dedupe(ranked))
        s["ranked"] = metrics.dedupe(ranked)[:20]
        out[path] = s

    ft, ms_ft = run.fulltext(q)
    done("fulltext", ft, ms_ft)
    sd, ms_sd = run.seeds(q)
    done("seeds", sd["list"], ms_sd)
    nb_scores, ms_nb = run.neighbors(sd["ent_ids"], sd["ent_scores"], q["q"])
    nb = list(nb_scores)
    both = {r: min(0.99, sd["scores"][r] + sc) for r, sc in nb_scores.items() if r in sd["scores"]}      # 種と周りの両方から来た参照は、2 つの点数を足して（上限 0.99）押し上げる
    l3 = fuse_scored([r for r in sd["list"] if r not in both], sd["scores"], {**nb_scores, **both})
    done("neighbors", l3, ms_sd + ms_nb)
    mt_scores, ms_mt = run.meet(sd["ent_ids"], sd["ent_scores"], q["q"])
    both_m = {r: min(0.99, sd["scores"][r] + sc) for r, sc in mt_scores.items() if r in sd["scores"]}
    lm = fuse_scored([r for r in sd["list"] if r not in both_m], sd["scores"], {**mt_scores, **both_m})
    done("meet", lm, ms_sd + ms_mt)
    out["meet"]["info"] = dict(run.meet_info)
    pp, ms_pp = run.ppr(sd["ent_ids"], sd["ent_scores"])
    l4 = rrf(sd["list"], pp)
    done("ppr", l4, ms_sd + ms_pp)
    fused = rrf(sd["list"], nb, pp)
    cref, ctext, ms_cx = run.context(fused)
    l5 = rrf(sd["list"], nb, pp, cref)
    docs5 = [r for r in fused if r.startswith("doc:")][:5]
    ctx_text = ctext + "\n" + run.render(docs5)
    done("context", l5, ms_sd + ms_nb + ms_pp + ms_cx, ctx_refs=set(cref) | set(docs5), ctx_text=ctx_text)
    tref, ttext, ms_tl = run.tools(sd["ent_ids"])
    l6 = metrics.dedupe(tref[:5] + l5)      # 道具が返した先頭の参照（変化・数値が新しい順・条件の組の順）を先に、残りは⑤の並び
    done("tools", l6, ms_sd + ms_nb + ms_pp + ms_cx + ms_tl, ctx_refs=set(cref) | set(docs5) | set(tref), ctx_text=ctx_text + "\n" + ttext)
    return out


def run_all(client, db_path: Path, questions: list[dict], warmup: int = 3, progress=None, meet_depth: int | None = None) -> list[dict]:
    lk = Lookup(db_path)
    for q in questions[:warmup]:      # 温めてから測る（初回の接続・ページの読み込みを外す）
        run_question(Run(client, lk, meet_depth), q)
    rows = []
    for i, q in enumerate(questions):
        res = run_question(Run(client, lk, meet_depth), q)
        rows.append({"id": q["id"], "type": q["type"], "q": q["q"], "gold_n": len(gold_refs(q)), "paths": res})
        if progress:
            progress(i + 1, len(questions))
    return rows
