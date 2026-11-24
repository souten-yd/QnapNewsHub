"""新しい connector の取得と整形（v0.7 前半）。枠組み・予算・利用条件は connectors.py。

どの取得も `async def name(ctx) -> Result`（`ctx.begin()` の入れ物へ積む。途中で予算・失敗で止まっても、そこまでの分は保存される）。
整形（parse_*・derivative_of・quant_kinds・parse_sitemap など）は通信をしない純粋な関数で、試験が直接呼ぶ。
通信は `ctx.get`/`ctx.json` だけ（予算・間隔・鍵・内部のアドレスの拒否）。ページの取得は robots.txt を守る（bodies の判定を使い回す）。
文書の区分は public（公開の情報源だけ。利用条件が分からないものの ai_use は local_only）。
"""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlsplit

from qnapnewshub import connectors, net, sources
from qnapnewshub.connector_terms import TERMS
from qnapnewshub.connectors import Ctx, Result

_UTC = timezone.utc
GITHUB = "https://api.github.com"
HF = "https://huggingface.co/api/models"
OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works/"
S2 = "https://api.semanticscholar.org/graph/v1/paper/"
HN = "https://hn.algolia.com/api/v1/search"
MAX_SITEMAP_BYTES = 10 * 1024 * 1024
MAX_SITEMAP_CHILDREN = 5
MAX_PAGE_BYTES = 2 * 1024 * 1024
CHANGE_MIN_GAP = 15 * 60


def _day_start(now: datetime | None = None) -> datetime:
    return (now or datetime.now(_UTC)).replace(hour=0, minute=0, second=0, microsecond=0)


def _ai_use(cid: str) -> str:
    return TERMS[cid]["ai_use"]


def _text(value: object, limit: int) -> str:
    return sources.plain(value, limit)


# ---- GitHub: 発見・スターの急増 ----

def repo_doc(row: dict, *, via: str, cid: str = "github_discover") -> dict | None:
    """GitHub の検索（または /repos/…）の 1 件を文書（kind repo・L1）にする。"""
    name = str(row.get("full_name") or "")
    url = sources.safe_url(row.get("html_url"))
    if not url or not sources._REPO.match(name):
        return None
    lic = row.get("license") if isinstance(row.get("license"), dict) else {}
    meta = {"stars": int(row.get("stargazers_count") or 0), "language": row.get("language"), "topics": [str(t) for t in (row.get("topics") or [])[:12]],
            "license": lic.get("spdx_id"), "created_at": row.get("created_at"), "pushed_at": row.get("pushed_at"), "github_id": row.get("id"), "via": via}
    return {"kind": "repo", "ids": {"url": url}, "title": name, "url": url, "source": "GitHub", "published_at": sources.parse_time(row.get("created_at")),
            "abstract": _text(row.get("description"), 400), "tier": 1, "reason": f"{via}で見つかった", "meta": meta, "ai_use": _ai_use(cid), "connector": cid}


def star_metric(name: str, stars: int, *, at: datetime | None = None, cid: str = "github_discover") -> dict:
    return {"subject": f"repo:{name}", "metric": "stars", "value": float(stars), "unit": "count", "at": _day_start(at), "source": cid, "cond": "daily"}


def search_query(topic: str, *, days: int, min_stars: int, now: datetime | None = None) -> str:
    since = (_day_start(now) - timedelta(days=days)).strftime("%Y-%m-%d")
    return f"topic:{topic} created:>{since} stars:>={min_stars}"


async def github_discover(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    head = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    seen: set[str] = set()
    for topic in s["topics"]:
        data = await ctx.json(f"{GITHUB}/search/repositories", params={"q": search_query(topic, days=s["window_days"], min_stars=s["min_stars"]), "sort": "stars", "order": "desc",
                                                                       "per_page": s["per_topic"]}, headers=head)
        for row in (data.get("items") if isinstance(data, dict) else None) or []:
            doc = repo_doc(row, via=f"検索（トピック {topic}）") if isinstance(row, dict) else None
            if doc and doc["title"] not in seen:
                seen.add(doc["title"])
                res.docs.append(doc)
                res.metrics.append(star_metric(doc["title"], doc["meta"]["stars"]))
    if s.get("watch_stars"):
        for repo in sources.load_config()["github"][:30]:
            if repo in seen:
                continue
            try:
                row = await ctx.json(f"{GITHUB}/repos/{repo}", headers=head)
            except net.HttpError as exc:
                if exc.status in (403, 429):
                    res.note = "レートの上限に当たったため、監視のリポジトリのスター数は途中までです"
                    break
                continue
            doc = repo_doc(row, via="監視のリポジトリ") if isinstance(row, dict) else None
            if doc:
                seen.add(repo)
                res.docs.append(doc)
                res.metrics.append(star_metric(doc["title"], doc["meta"]["stars"]))
    return res


# ---- Hugging Face: 新しいモデル・派生版 ----

_QUANT_TAGS = {"gguf": "gguf", "awq": "awq", "gptq": "gptq", "exl2": "exl2", "exl3": "exl3", "mlx": "mlx", "bitsandbytes": "bnb", "fp8": "fp8", "nvfp4": "nvfp4",
               "mxfp4": "mxfp4", "int4": "int4", "int8": "int8", "4-bit": "int4", "8-bit": "int8", "compressed-tensors": "compressed-tensors"}
_BASE_TAG = re.compile(r"^base_model:(?:(finetune|quantized|adapter|merge):)?(.+)$", re.I)


def quant_kinds(row: dict) -> list[str]:
    """量子化の種類（GGUF・AWQ・GPTQ・EXL2・MLX・bnb・FP8 など）。タグ・library_name・モデル名から。"""
    found: list[str] = []
    hay = [str(t).lower() for t in (row.get("tags") or [])] + [str(row.get("library_name") or "").lower()]
    name = str(row.get("id") or row.get("modelId") or "").lower()
    for token, kind in _QUANT_TAGS.items():
        if token in hay or re.search(rf"(^|[^a-z0-9]){re.escape(token)}($|[^a-z0-9])", name):
            found.append(kind)
    return list(dict.fromkeys(found))


def base_models(row: dict) -> list[tuple[str, str]]:
    """（元のモデルの id・関係）の一覧。タグ base_model:<関係>:<id> と cardData.base_model。関係が分からないときは ""。id は小文字にそろえる。"""
    out: list[tuple[str, str]] = []
    for t in row.get("tags") or []:
        m = _BASE_TAG.match(str(t))
        if m:
            out.append((m.group(2).strip().lower(), (m.group(1) or "").lower()))
    card = row.get("cardData") if isinstance(row.get("cardData"), dict) else {}
    bm = card.get("base_model")
    for b in ([bm] if isinstance(bm, str) else bm if isinstance(bm, list) else []):
        if isinstance(b, str) and "/" in b:
            out.append((b.strip().lower(), ""))
    return list(dict.fromkeys(out))


def derivative_of(row: dict, watched: list[str]) -> tuple[str, str] | None:
    """監視のモデル（owner/name。大文字小文字は区別しない）の派生版なら（監視のモデル・関係）。関係が不明なら量子化の種類があれば quantized、無ければ finetune とみなす。"""
    want = {w.lower(): w for w in watched}
    for base, rel in base_models(row):
        if base in want:
            return want[base], rel or ("quantized" if quant_kinds(row) else "finetune")
    return None


def model_doc(row: dict, watched: list[str], *, cid: str = "hf_models") -> dict | None:
    mid = str(row.get("id") or row.get("modelId") or "")
    if not sources._REPO.match(mid):
        return None
    url = f"https://huggingface.co/{mid}"
    tags = [str(t) for t in (row.get("tags") or [])]
    meta = {"downloads": int(row.get("downloads") or 0), "likes": int(row.get("likes") or 0), "pipeline": row.get("pipeline_tag"), "author": mid.split("/")[0],
            "library": row.get("library_name"), "tags": [t for t in tags if not t.startswith(("base_model:", "region:", "license:"))][:15], "quant": quant_kinds(row),
            "license": next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)}
    dv = derivative_of(row, watched)
    reason = "新しいモデル"
    if dv:
        meta["derivative_of"], meta["relation"] = dv
        reason = f"監視のモデル {dv[0]} の派生版（{dv[1]}）"
    abstract = " · ".join(x for x in [str(row.get("pipeline_tag") or ""), ", ".join(meta["quant"]), ", ".join(meta["tags"][:6])] if x)
    return {"kind": "model", "ids": {"url": url}, "title": mid, "url": url, "source": "Hugging Face", "published_at": sources.parse_time(row.get("createdAt")),
            "abstract": abstract, "tier": 1, "reason": reason, "meta": meta, "ai_use": _ai_use(cid), "connector": cid}


_HF_EXPAND = ["downloads", "likes", "createdAt", "pipeline_tag", "tags", "library_name", "author", "cardData"]


async def hf_models(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    watched = list(s["watch_models"])
    seen: set[str] = set()

    def add(rows: object, *, only_derivative: bool = False) -> None:
        for row in rows if isinstance(rows, list) else []:
            doc = model_doc(row, watched) if isinstance(row, dict) else None
            if doc and only_derivative and "derivative_of" not in doc["meta"]:      # サーバーの絞り込みが効かなくても、監視のモデルの派生版でないものは入れない
                continue
            if doc and doc["title"] not in seen:
                seen.add(doc["title"])
                res.docs.append(doc)

    base = {"sort": "createdAt", "direction": "-1", "limit": s["limit"], "expand[]": _HF_EXPAND}
    groups: list[dict] = []
    if s["authors"]:
        groups += [{"author": a} for a in s["authors"][:5]]
    if s["pipelines"]:
        groups += [{"pipeline_tag": p} for p in s["pipelines"][:5]]
    if s["tags"] and not groups:
        groups = [{}]
    for g in groups or ([{}] if not watched else []):
        params = dict(base, **g)
        if s["tags"]:
            params["filter"] = s["tags"]
        add(await ctx.json(HF, params=params))
    for mid in watched[:10]:
        for rel in s["relations"]:
            add(await ctx.json(HF, params={"filter": f"base_model:{rel}:{mid}", "sort": "createdAt", "direction": "-1", "limit": 20, "expand[]": _HF_EXPAND}), only_derivative=True)
    return res


# ---- 論文: arXiv（既存の RSS から）・OpenAlex・Crossref・Semantic Scholar ----

_ARXIV_PREFIX = re.compile(r"^arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*", re.I)


def papers_from_feed_items(items: list[dict]) -> list[dict]:
    """arXiv の RSS から取れた記事（sources.parse_feed の形）を、論文の文書（L1・arXiv ID）にする。ID が読めないものは捨てる。"""
    out = []
    for it in items:
        aid = norm = ""
        from qnapnewshub import docstore

        for cand in (it.get("url", ""), it.get("guid", "")):
            m = re.search(r"(?:arxiv\.org/abs/|oai:arXiv\.org:)([^\s?#]+)", str(cand), re.I)
            norm = docstore.norm_arxiv(m.group(1)) if m else ""
            if norm:
                aid = norm
                break
        if not aid:
            continue
        out.append({"kind": "paper", "ids": {"arxiv": aid}, "title": it["title"], "url": f"https://arxiv.org/abs/{aid}", "source": "arXiv", "published_at": it.get("published_at"),
                    "abstract": _ARXIV_PREFIX.sub("", it.get("summary") or ""), "tier": 1, "reason": "arXiv の RSS", "meta": {"arxiv": aid}, "ai_use": _ai_use("arxiv"), "connector": "arxiv"})
    return out


def stage_arxiv_papers(items: list[dict]) -> int:
    """service が arXiv の取得先の結果を渡す。論文の文書をまとめ書きへ積む（件数は arxiv の予算に数える）。"""
    from qnapnewshub import writer

    docs = papers_from_feed_items(items)
    if docs:
        writer.get().stage_connector({"connector": "arxiv", "docs": docs, "links": [], "metrics": []})
    return len(docs)


def openalex_doc(row: dict, *, cid: str = "openalex") -> dict | None:
    title = _text(row.get("display_name") or row.get("title"), 400)
    ids = row.get("ids") if isinstance(row.get("ids"), dict) else {}
    paper_ids = {"doi": row.get("doi") or ids.get("doi") or "", "openalex": row.get("id") or ids.get("openalex") or ""}
    loc = row.get("primary_location") if isinstance(row.get("primary_location"), dict) else {}
    src = loc.get("source") if isinstance(loc.get("source"), dict) else {}
    for key in ("landing_page_url", "pdf_url"):      # arXiv に載っている論文は arXiv ID でも同定する
        m = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+)", str(loc.get(key) or ""), re.I)
        if m:
            paper_ids["arxiv"] = m.group(1)
    from qnapnewshub import docstore

    if not title or not docstore.paper_idents(paper_ids):
        return None
    authors = [str((a.get("author") or {}).get("display_name") or "") for a in (row.get("authorships") or []) if isinstance(a, dict)]
    topic = row.get("primary_topic") if isinstance(row.get("primary_topic"), dict) else {}
    meta = {"authors": [a for a in authors if a][:8], "citations": int(row.get("cited_by_count") or 0), "citations_at": _day_start().strftime("%Y-%m-%d"),
            "venue": src.get("display_name"), "topic": topic.get("display_name"), "year": row.get("publication_year"), "type": row.get("type")}
    url = sources.safe_url(loc.get("landing_page_url") or row.get("doi") or row.get("id")) or ""
    return {"kind": "paper", "ids": {**paper_ids, "url": ""}, "title": title, "url": url, "source": "OpenAlex", "published_at": sources.parse_time(row.get("publication_date")),
            "abstract": "", "tier": 1, "reason": "OpenAlex の書誌", "meta": meta, "ai_use": _ai_use(cid), "connector": cid}


async def openalex(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    since = (_day_start() - timedelta(days=s["days"])).strftime("%Y-%m-%d")
    params = {"filter": f"{s['filter']},from_publication_date:{since},type:article", "sort": "publication_date:desc", "per_page": s["per_page"],
              "select": "id,doi,ids,title,display_name,publication_date,publication_year,cited_by_count,authorships,primary_location,primary_topic,type"}
    if s["search"]:
        params["search"] = s["search"]
    data = await ctx.json(OPENALEX, params=params)
    for row in (data.get("results") if isinstance(data, dict) else None) or []:
        doc = openalex_doc(row) if isinstance(row, dict) else None
        if doc:
            res.docs.append(doc)
    return res


def crossref_doc(doi: str, msg: dict, *, cid: str = "crossref") -> dict | None:
    """Crossref の /works/{doi} の message から書誌（題・著者・掲載誌・日付・引用数）。要旨は保存しない。"""
    title = _text((msg.get("title") or [""])[0] if isinstance(msg.get("title"), list) else msg.get("title"), 400)
    if not title:
        return None
    parts = ((msg.get("issued") or {}).get("date-parts") or [[None]])[0]
    when = None
    if parts and parts[0]:
        when = datetime(int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1] else 1, int(parts[2]) if len(parts) > 2 and parts[2] else 1, tzinfo=_UTC)
    authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or str(a.get("name") or "") for a in (msg.get("author") or []) if isinstance(a, dict)]
    venue = (msg.get("container-title") or [None])[0] if isinstance(msg.get("container-title"), list) else None
    return {"kind": "paper", "ids": {"doi": doi}, "title": title, "url": sources.safe_url(msg.get("URL")) or f"https://doi.org/{doi}", "source": "Crossref", "published_at": when,
            "abstract": "", "tier": 1, "reason": "Crossref で書誌を解決した", "ai_use": _ai_use(cid), "connector": cid,
            "meta": {"authors": [a for a in authors if a][:8], "venue": venue, "crossref": "ok", "crossref_citations": msg.get("is-referenced-by-count"), "type": msg.get("type")}}


def _targets_crossref(limit: int) -> list[str]:
    from sqlalchemy import select

    from qnapnewshub import db as news_db
    from qnapnewshub.db import Document

    with news_db.session() as db:
        rows = db.execute(select(Document.ident, Document.meta).where(Document.kind == "paper", Document.ident.like("doi:%"), Document.tier <= 1).order_by(Document.id).limit(limit * 5))
        out = []
        for ident, meta in rows:
            if '"crossref"' in (meta or ""):
                continue
            out.append(ident[4:])
            if len(out) >= limit:
                break
        return out


async def crossref(ctx: Ctx, *, dois: list[str] | None = None) -> Result:
    res = ctx.begin()
    import asyncio

    targets = dois if dois is not None else await asyncio.to_thread(_targets_crossref, ctx.settings["batch"])
    for doi in targets:
        status, _head, body = await ctx.get(CROSSREF + quote(doi, safe="/"), with_key=False)
        if status == 404:      # 解決できない DOI は二度と試さない
            res.docs.append({"kind": "paper", "ids": {"doi": doi}, "title": "", "tier": 0, "meta": {"crossref": "not_found"}, "connector": "crossref", "ai_use": _ai_use("crossref")})
            continue
        if status >= 400:
            raise net.HttpError(status)
        try:
            msg = json.loads(body).get("message") or {}
        except ValueError:
            continue
        doc = crossref_doc(doi, msg) if isinstance(msg, dict) else None
        if doc:
            res.docs.append(doc)
    return res


def s2_paper_rec(p: dict, *, cid: str = "semanticscholar") -> dict | None:
    """Semantic Scholar の論文 1 件を L0 の文書にする（URL・題・日付・ID だけ。DOI／arXiv ID があればそれで同定する）。"""
    if not isinstance(p, dict) or not p.get("paperId"):
        return None
    ext = p.get("externalIds") if isinstance(p.get("externalIds"), dict) else {}
    ids = {"s2": p["paperId"], "doi": ext.get("DOI") or "", "arxiv": ext.get("ArXiv") or ""}
    title = _text(p.get("title"), 400)
    return {"kind": "paper", "ids": ids, "title": title, "url": f"https://www.semanticscholar.org/paper/{p['paperId']}", "source": "Semantic Scholar",
            "published_at": sources.parse_time(p.get("publicationDate")) or (datetime(int(p["year"]), 1, 1, tzinfo=_UTC) if p.get("year") else None), "tier": 0,
            "reason": "引用をたどって見つけた", "meta": {"citations": p.get("citationCount")} if p.get("citationCount") is not None else {}, "ai_use": _ai_use(cid), "connector": cid}


def _seed_ref(doc_ident: str) -> str:
    kind, _, rest = doc_ident.partition(":")
    return {"arxiv": f"ARXIV:{rest}", "doi": f"DOI:{rest}", "s2": rest}.get(kind, "")


def _seeds_s2(limit: int) -> list[tuple[str, dict]]:
    """引用をたどる論文: 本文まで進んだ（L2 以上）か、監視に合って昇格した論文のうち、まだたどっていないもの（昇格の新しい順）。"""
    from sqlalchemy import select

    from qnapnewshub import db as news_db
    from qnapnewshub.db import Document

    out = []
    with news_db.session() as db:
        for d in db.scalars(select(Document).where(Document.kind == "paper", Document.tier >= 2).order_by(Document.tier_at.desc()).limit(limit * 10)):
            if "s2_expanded_at" in (d.meta or ""):
                continue
            ref = _seed_ref(d.ident)
            if ref:
                rec = {"kind": "paper", "ids": {"url": ""}, "title": d.title}
                out.append((ref, {"ident": d.ident, "title": d.title}))
            if len(out) >= limit:
                break
    return out


def _ident_ids(ident: str) -> dict:
    kind, _, rest = ident.partition(":")
    return {kind: rest} if kind in ("arxiv", "doi", "s2") else {}


async def _s2_edges(ctx: Ctx, ref: str, per_paper: int) -> tuple[list[dict], list[dict]]:
    fields = "paperId,externalIds,title,year,publicationDate,citationCount"
    cites = await ctx.json(f"{S2}{quote(ref, safe=':/')}/citations", params={"fields": fields, "limit": per_paper})
    refs = await ctx.json(f"{S2}{quote(ref, safe=':/')}/references", params={"fields": fields, "limit": per_paper})
    citing = [r for r in (s2_paper_rec(x.get("citingPaper")) for x in (cites.get("data") or []) if isinstance(x, dict)) if r]
    cited = [r for r in (s2_paper_rec(x.get("citedPaper")) for x in (refs.get("data") or []) if isinstance(x, dict)) if r]
    return citing, cited


async def semanticscholar(ctx: Ctx, *, seeds: list[tuple[str, dict]] | None = None) -> Result:
    res = ctx.begin()
    import asyncio

    s = ctx.settings
    todo = seeds if seeds is not None else await asyncio.to_thread(_seeds_s2, s["seeds"])
    now_day = _day_start().strftime("%Y-%m-%d")
    for ref, info in todo:
        me = {"kind": "paper", "ids": _ident_ids(info["ident"]), "title": info.get("title", ""), "tier": 0, "ai_use": _ai_use("semanticscholar"), "connector": "semanticscholar",
              "meta": {"s2_expanded_at": now_day}}
        citing, cited = await _s2_edges(ctx, ref, s["per_paper"])
        res.docs.append(me)
        for other in citing:
            res.links.append({"src": other, "dst": me, "type": "cites"})
        for other in cited:
            res.links.append({"src": me, "dst": other, "type": "cites"})
        if s["depth"] >= 2:      # 2 段目: 見つけた論文のうち引用数の多いものだけ（件数の上限つき）
            second = sorted([*citing, *cited], key=lambda r: -int((r.get("meta") or {}).get("citations") or 0))[: s["second_level"]]
            for other in second:
                o_ref = f"PAPERID:{other['ids']['s2']}"
                c2, d2 = await _s2_edges(ctx, other["ids"]["s2"], max(3, s["per_paper"] // 3))
                for x in c2:
                    res.links.append({"src": x, "dst": other, "type": "cites"})
                for x in d2:
                    res.links.append({"src": other, "dst": x, "type": "cites"})
                del o_ref
    return res


# ---- HN Algolia の検索（道具） ----

def hn_hits(data: object, *, cid: str = "hn_search") -> list[dict]:
    """HN の検索の結果を L0 の文書にする（識別子は議論のページの URL。記事の URL は meta）。"""
    out = []
    for hit in (data.get("hits") if isinstance(data, dict) else None) or []:
        if not isinstance(hit, dict) or not hit.get("objectID") or not hit.get("title"):
            continue
        discuss = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        out.append({"kind": "other", "ids": {"url": discuss}, "title": _text(hit.get("title"), 300), "url": discuss, "source": "Hacker News", "published_at": sources.parse_time(hit.get("created_at_i") or hit.get("created_at")),
                    "tier": 0, "reason": "HN の検索", "ai_use": _ai_use(cid), "connector": cid,
                    "meta": {"points": hit.get("points"), "comments": hit.get("num_comments"), "story_url": sources.safe_url(hit.get("url"))}})
    return out


async def hn_search(ctx: Ctx, *, query: str = "", days: int | None = None, save: bool = False) -> Result:
    res = ctx.begin()
    q = " ".join(str(query).split())[:200]
    if not q:
        return res
    params = {"query": q, "tags": "story", "hitsPerPage": ctx.settings["per_query"]}
    if days:
        params["numericFilters"] = f"created_at_i>{int((datetime.now(_UTC) - timedelta(days=int(days))).timestamp())}"
    data = await ctx.json(HN, params=params)
    docs = hn_hits(data)
    res.note = json.dumps([{"title": d["title"], "url": d["url"], "story_url": d["meta"]["story_url"], "points": d["meta"]["points"], "comments": d["meta"]["comments"],
                            "at": d["published_at"].strftime("%Y-%m-%d") if d["published_at"] else ""} for d in docs], ensure_ascii=False)
    if save:
        res.docs.extend(docs)
    return res


# ---- Sitemap ----

def _decompress(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        try:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out = d.decompress(body, MAX_SITEMAP_BYTES + 1)
        except zlib.error as exc:
            raise ValueError("sitemap を展開できません") from exc
        if len(out) > MAX_SITEMAP_BYTES:
            raise ValueError("sitemap が大きすぎます")
        return out
    return body


def parse_sitemap(raw: bytes) -> tuple[str, list[tuple[str, str]]]:
    """sitemap の XML を ("urlset"|"index", [(loc, lastmod)]) にする。DTD・実体は断る（攻撃の備え）。形が違えば ValueError。"""
    import xml.etree.ElementTree as ET

    raw = _decompress(raw)
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in raw[:20000].lower():
        raise ValueError("DTD・実体を含む sitemap は読みません")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("sitemap の XML を読めません") from exc
    kind = root.tag.rsplit("}", 1)[-1].lower()
    if kind not in ("urlset", "sitemapindex"):
        raise ValueError("sitemap の形ではありません")
    rows = []
    for el in root:
        loc = lastmod = ""
        for c in el:
            tag = c.tag.rsplit("}", 1)[-1].lower()
            if tag == "loc":
                loc = (c.text or "").strip()
            elif tag == "lastmod":
                lastmod = (c.text or "").strip()
        if loc:
            rows.append((loc, lastmod))
    return ("index" if kind == "sitemapindex" else "urlset"), rows


def title_from_url(url: str) -> str:
    """URL のいちばん後ろの区切りから作った題（本文は取らないので、題は URL から）。"""
    parts = urlsplit(url)
    seg = unquote([s for s in parts.path.split("/") if s][-1]) if [s for s in parts.path.split("/") if s] else (parts.hostname or "")
    seg = re.sub(r"\.(html?|php|aspx?)$", "", seg, flags=re.I)
    return re.sub(r"[-_+]+", " ", seg).strip()[:200] or (parts.hostname or "")


def sitemap_docs(rows: list[tuple[str, str]], *, site: str, host: str, allowed, max_urls: int, cid: str = "sitemap") -> tuple[list[dict], int]:
    """sitemap の行を L0 の文書にする。同じホストだけ（sitemap の決まり）・http/https・robots.txt が許すものだけ。新しい lastmod の順に max_urls 件。（文書, robots で除いた数）。"""
    seen, out, blocked = set(), [], 0
    ordered = sorted(rows, key=lambda r: r[1], reverse=True)
    for loc, lastmod in ordered:
        url = sources.safe_url(loc)
        if not url or (urlsplit(url).hostname or "").lower() != host or url in seen:
            continue
        seen.add(url)
        if not allowed(url):
            blocked += 1
            continue
        out.append({"kind": "other", "ids": {"url": url}, "title": title_from_url(url), "url": url, "source": site[:120], "published_at": sources.parse_time(lastmod), "tier": 0,
                    "reason": "sitemap で見つかった", "meta": {"lastmod": lastmod or None}, "ai_use": _ai_use(cid), "connector": cid})
        if len(out) >= max_urls:
            break
    return out, blocked


async def _robots_rules(url: str):
    """ホストの robots.txt の規則（bodies の判定を使い回す）。読めないとき（5xx・通信の失敗）は RobotsUnavailable。"""
    from qnapnewshub import bodies

    try:
        return await bodies._robots_for(url)      # noqa: SLF001 - 同じ判定・同じ覚えを使う
    except bodies.Deferred as exc:
        raise RobotsUnavailable(str(exc)) from exc


class RobotsUnavailable(Exception):
    pass


async def sitemap(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    blocked_total = 0
    for site in s["sites"]:
        url = site["url"]
        host = (urlsplit(url).hostname or "").lower()
        try:
            rules = await _robots_rules(url)
        except RobotsUnavailable:
            ctx.notes.append(f"{host}: robots.txt を読めないため取りません")
            continue
        allowed = (lambda u, r=rules: True if r is None else r.allowed(u))
        if not allowed(url):
            ctx.notes.append(f"{host}: robots.txt が sitemap を許していません")
            continue
        status, _, body = await ctx.get(url, accept="application/xml,text/xml,*/*", max_bytes=MAX_SITEMAP_BYTES, with_key=False)
        if status >= 400:
            ctx.notes.append(f"{host}: sitemap が取れません（HTTP {status}）")
            continue
        kind, rows = parse_sitemap(body)
        if kind == "index":
            children = [loc for loc, _ in sorted(rows, key=lambda r: r[1], reverse=True)][:MAX_SITEMAP_CHILDREN]
            rows = []
            for child in children:
                child_url = sources.safe_url(child)
                if not child_url or (urlsplit(child_url).hostname or "").lower() != host or not allowed(child_url):
                    continue
                st, _, b = await ctx.get(child_url, accept="application/xml,text/xml,*/*", max_bytes=MAX_SITEMAP_BYTES, with_key=False)
                if st < 400:
                    k2, r2 = parse_sitemap(b)
                    if k2 == "urlset":
                        rows += r2
        docs, blocked = sitemap_docs(rows, site=site["name"], host=host, allowed=allowed, max_urls=s["max_urls"])
        blocked_total += blocked
        res.docs.extend(docs)
    res.note = "; ".join([*ctx.notes, *([f"robots.txt で除いた URL {blocked_total} 件"] if blocked_total else [])])
    return res


# ---- 変化の検出 ----

_STRIP = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<!--.*?-->", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


def page_fingerprint(raw: bytes) -> str:
    """ページの見えている文字だけの hash（script・style・コメント・タグ・空白の違いは変化にしない）。"""
    text = raw[:MAX_PAGE_BYTES].decode("utf-8", "replace")
    text = html.unescape(_TAGS.sub(" ", _STRIP.sub(" ", text)))
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_change(prev: dict | None, status: int, head: dict, body: bytes, now: float) -> tuple[dict, str]:
    """前回の状態 prev と今回の応答から、新しい状態と結果（"baseline"・"changed"・"unchanged"・"not_modified"）。通信をしない。"""
    st = dict(prev or {})
    st["checked_at"] = now
    if status == 304:
        return st, "not_modified"
    digest = page_fingerprint(body)
    st["etag"], st["last_modified"] = head.get("etag", "")[:300], head.get("last-modified", "")[:100]
    old = st.get("hash")
    st["hash"] = digest
    if not old:
        return st, "baseline"
    if old == digest:
        return st, "unchanged"
    st["changed_at"] = now
    st["changes"] = [*(st.get("changes") or [])[-19:], {"at": now, "from": old[:12], "to": digest[:12]}]
    return st, "changed"


async def change(ctx: Ctx) -> Result:
    import time

    res = ctx.begin()
    pages = connectors.state("change").setdefault("pages", {})
    changed = 0
    for page in ctx.settings["pages"]:
        url = page["url"]
        prev = pages.get(url)
        if prev and time.time() - float(prev.get("checked_at", 0)) < CHANGE_MIN_GAP:
            continue
        try:
            rules = await _robots_rules(url)
        except RobotsUnavailable:
            ctx.notes.append(f"{page['name']}: robots.txt を読めないため見ません")
            continue
        if rules is not None and not rules.allowed(url):
            ctx.notes.append(f"{page['name']}: robots.txt が許していません")
            continue
        hdr = {}
        if prev and prev.get("etag"):
            hdr["If-None-Match"] = prev["etag"]
        if prev and prev.get("last_modified"):
            hdr["If-Modified-Since"] = prev["last_modified"]
        status, head, body = await ctx.get(url, headers=hdr or None, accept="text/html,*/*", max_bytes=MAX_PAGE_BYTES, with_key=False)
        if status >= 400:
            ctx.notes.append(f"{page['name']}: HTTP {status}")
            continue
        if 300 <= status < 400:
            ctx.notes.append(f"{page['name']}: リダイレクトが多すぎます")
            continue
        new, outcome = detect_change(prev, status, head, body, time.time())
        pages[url] = new
        if outcome in ("baseline", "changed"):
            changed += outcome == "changed"
            res.docs.append({"kind": "other", "ids": {"url": url}, "title": page["name"], "url": url, "source": "変化の検出", "version": new["hash"][:12],
                             "published_at": datetime.now(_UTC) if outcome == "changed" else None, "tier": 0, "ai_use": _ai_use("change"), "connector": "change",
                             "reason": "ページの変化の検出", "meta": {"changed_at": datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if outcome == "changed" else None,
                                                                 "changes": len(new.get("changes") or [])}})
    res.note = "; ".join([f"変化 {changed} 件", *ctx.notes])
    return res
