"""5 つの測る道を、同じ問いで、既存の API（ASGI を直接）だけで流して点数にする。LLM は呼ばない・外へ通信しない。

道（ranking は参照 `doc:` `ent:` `clm:` `rel:` `evt:` の並び）:
  ① fulltext … GET /news/items?q=<キーワード>（今の全文検索。問いに付けたキーワードで引く＝LLM が打つ検索語の代わり）
  ② seeds    … POST /retrieve/seeds（問いの文そのまま。文書・物・イベントを 1 件ずつ交互に並べる）
  ③ neighbors … ②の上位 3 つの物から POST /graph/neighbors（深さ 1）→ 辺（rel）・相手の物・辺の根拠の文書（POST /retrieve/evidence）。②と RRF で合わせる
  ④ ppr      … ②の上位 3 つの物から POST /graph/ppr → 物・上位の物の主張と根拠の文書（/retrieve/evidence）。②と RRF で合わせる
  ⑤ context  … ③④を合わせ、上位の物の POST /things/context（LLM に渡す文脈）。文脈から取れる主張・関係・文書の参照も足す
合わせ方 RRF は 1/(10+順位)。時間は、その道に必要な呼び出しを順に流した合計（キャッシュは呼び出しごとに捨てる）。
文脈の密度: 渡した文脈（①〜④は上位 10 件を文にしたもの、⑤は物の文脈＋上位 5 文書の題）に入っていた正解の参照の数 ÷ 見積りのトークン数 × 1000。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import metrics

PATHS = ("fulltext", "seeds", "neighbors", "ppr", "context")
PATH_LABEL = {"fulltext": "① 全文だけ", "seeds": "② 種", "neighbors": "③ 種＋周り 1 段", "ppr": "④ 種＋局所 PPR", "context": "⑤ 物の文脈"}
H = {"X-Requested-With": "NewsHub"}
SEED_LIMIT = 10
EXPAND_FROM = 3       # 種の物のうち、周りへ広げる数
EXPAND_MAX = 30
CONTEXT_ENTITIES = 6
CONTEXT_TOP = 10
RRF_K = 10


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
    def __init__(self, client, lookup: Lookup):
        self.c = client
        self.lk = lookup
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
        docs, ents, evs = [], [], []
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
        return {"list": interleave(docs, ents, evs), "ent_ids": [int(e.split(":")[1]) for e in ents], "ent_scores": [it["score"] for it in r["items"] if it["kind"] == "entity"]}, ms

    def _evidence_docs(self, body: dict) -> tuple[dict, float]:
        return self.call("POST", "/retrieve/evidence", json=body)

    # ---- ③ ----
    def neighbors(self, ent_ids: list[int]) -> tuple[list[str], float]:
        ids = ent_ids[:EXPAND_FROM]
        if not ids:
            return [], 0.0
        r, ms = self.call("POST", "/graph/neighbors", json={"ids": ids, "depth": 1, "limit": 40, "order": "weight", "hub_k": 10})
        names = {n["id"]: n["name"] for lv in r["levels"] for n in lv["nodes"]} | {s["id"]: s["name"] for s in r["seeds"]}
        edges = sorted((e for lv in r["levels"] for e in lv["edges"]), key=lambda e: -(e.get("confidence") or 0))[:EXPAND_MAX]
        rel_ids = [e["id"] for e in edges if e["id"] in self.lk.rel_ids]
        ev, ms2 = self._evidence_docs({"relationships": rel_ids[:20], "per_item": 2}) if rel_ids else ({"relationships": {}}, 0.0)
        out: list[str] = []
        for e in edges:
            other = e["target"] if e["source"] in ids else e["source"]
            if e["id"] in self.lk.rel_ids:
                ref = f"rel:{e['id']}"
                out.append(ref)
                self.text[ref] = f"{names.get(e['source'], e['source'])} -{e['type']}-> {names.get(e['target'], e['target'])}"
            out.append(f"ent:{other}")
            self.text.setdefault(f"ent:{other}", names.get(other, str(other)))
            for evd in (ev["relationships"].get(str(e["id"])) or {}).get("evidences", []):
                ref = f"doc:{evd['document']['id']}"
                out.append(ref)
                self.text.setdefault(ref, evd["document"]["title"])
        return metrics.dedupe(out)[:EXPAND_MAX], ms + ms2

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
    nb, ms_nb = run.neighbors(sd["ent_ids"])
    l3 = rrf(sd["list"], nb)
    done("neighbors", l3, ms_sd + ms_nb)
    pp, ms_pp = run.ppr(sd["ent_ids"], sd["ent_scores"])
    l4 = rrf(sd["list"], pp)
    done("ppr", l4, ms_sd + ms_pp)
    fused = rrf(sd["list"], nb, pp)
    cref, ctext, ms_cx = run.context(fused)
    l5 = rrf(sd["list"], nb, pp, cref)
    docs5 = [r for r in fused if r.startswith("doc:")][:5]
    ctx_text = ctext + "\n" + run.render(docs5)
    done("context", l5, ms_sd + ms_nb + ms_pp + ms_cx, ctx_refs=set(cref) | set(docs5), ctx_text=ctx_text)
    return out


def run_all(client, db_path: Path, questions: list[dict], warmup: int = 3, progress=None) -> list[dict]:
    lk = Lookup(db_path)
    for q in questions[:warmup]:      # 温めてから測る（初回の接続・ページの読み込みを外す）
        run_question(Run(client, lk), q)
    rows = []
    for i, q in enumerate(questions):
        res = run_question(Run(client, lk), q)
        rows.append({"id": q["id"], "type": q["type"], "q": q["q"], "gold_n": len(gold_refs(q)), "paths": res})
        if progress:
            progress(i + 1, len(questions))
    return rows
