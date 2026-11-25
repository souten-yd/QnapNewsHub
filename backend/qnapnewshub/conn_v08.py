"""v0.8 の収集の connector の取得と整形（日銀・e-Stat・国会会議録・e-Gov 法令・EDINET・DefiLlama・CoinGecko・GDELT・PyPI・npm・OSV）。枠組み・予算・利用条件は connectors.py・connector_terms.py。

conn_impl.py と同じ形: `async def name(ctx) -> Result`。整形（parse_*・*_doc・*_metrics）は通信をしない純粋な関数で、試験が直接呼ぶ。通信は `ctx.get`/`ctx.json`/`ctx.post_json` だけ
（予算・間隔・鍵・内部のアドレスの拒否）。文書の識別子は `ids: {"ref": "名前空間:値", "url": …}`（docstore.paper_idents の ref）で、同じものは 1 件。
数値は数値のまま持つ: Metric の subject（名前空間つき。docstore.SUBJECT_KINDS）・metric・value・unit・at（時点）・source（connector）・cond（系列名・頻度などの条件）。
法制度は段（stage: proposed 提案・enacted 成立〔未施行〕・in_force 施行・repealed 廃止）を meta に持つ（いまは版と日付から enacted／in_force／repealed だけを決める。proposed は取り込み元ができたとき）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from qnapnewshub import net, sources
from qnapnewshub.connector_terms import TERMS
from qnapnewshub.connectors import Ctx, Result

_UTC = timezone.utc
BOJ = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
ESTAT = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"
KOKKAI = "https://kokkai.ndl.go.jp/api/speech"
EGOV = "https://laws.e-gov.go.jp/api/2/law_revisions/"
EDINET = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
LLAMA = "https://api.llama.fi"
LLAMA_STABLE = "https://stablecoins.llama.fi/stablecoins"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
PYPI = "https://pypi.org/pypi/"
NPM = "https://registry.npmjs.org/"
OSV = "https://api.osv.dev/v1/query"
MAX_LLAMA_BYTES = 25 * 1024 * 1024
LAW_STAGES = ("proposed", "enacted", "in_force", "repealed")
RUNNERS = frozenset({"boj", "estat", "kokkai", "egov_law", "edinet", "defillama", "coingecko", "gdelt", "pypi", "npm", "osv"})


def _ai(cid: str) -> str:
    return TERMS[cid]["ai_use"]


def _day(now: datetime | None = None) -> datetime:
    return (now or datetime.now(_UTC)).replace(hour=0, minute=0, second=0, microsecond=0)


def _num(value: object) -> float | None:
    """数値のまま。null・NA・ND・空・数でない文字は None（欠損を 0 にしない）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def _metric(subject: str, metric: str, value: float, unit: str, at: datetime, source: str, cond: str) -> dict:
    return {"subject": subject, "metric": metric, "value": float(value), "unit": unit[:20], "at": at, "source": source, "cond": cond[:200]}


# ---- 日本銀行 ----

def boj_date(code: str, frequency: str) -> datetime | None:
    """日銀の時点（YYYY・YYYYHH・YYYYQQ・YYYYMM・YYYYMMDD）を UTC の日付に。期種で 6 桁の読み方を分ける。読めなければ None。"""
    c = str(code).strip()
    freq = str(frequency).upper()
    try:
        if len(c) == 4:
            return datetime(int(c), 1, 1, tzinfo=_UTC)
        if len(c) == 6:
            y, n = int(c[:4]), int(c[4:])
            if freq.startswith("QUARTERLY"):
                return datetime(y, (n - 1) * 3 + 1, 1, tzinfo=_UTC) if 1 <= n <= 4 else None
            if freq.startswith("SEMIANNUAL"):
                return datetime(y, (n - 1) * 6 + 1, 1, tzinfo=_UTC) if 1 <= n <= 2 else None
            return datetime(y, n, 1, tzinfo=_UTC)
        if len(c) == 8:
            return datetime(int(c[:4]), int(c[4:6]), int(c[6:]), tzinfo=_UTC)
    except ValueError:
        return None
    return None


def boj_metrics(data: object, *, db: str, last_points: int) -> list[dict]:
    """日銀のコード API の応答（JSON）を Metric の点にする。系列ごとに新しい last_points 点。欠損（null）の点は作らない。"""
    out: list[dict] = []
    rows = data.get("RESULTSET") if isinstance(data, dict) else None
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("SERIES_CODE"):
            continue
        vals = r.get("VALUES") if isinstance(r.get("VALUES"), dict) else {}
        dates, values = vals.get("SURVEY_DATES") or [], vals.get("VALUES") or []
        name = str(r.get("NAME_OF_TIME_SERIES_J") or r.get("NAME_OF_TIME_SERIES") or "")[:90]
        unit = str(r.get("UNIT_J") or r.get("UNIT") or "")
        freq = str(r.get("FREQUENCY") or "")
        subject = f"indicator:boj:{db}:{r['SERIES_CODE']}"
        pts = []
        for d, v in zip(dates, values):
            at, val = boj_date(d, freq), _num(v)
            if at is not None and val is not None:
                pts.append(_metric(subject, "value", val, unit, at, "boj", f"{name} / {freq}"))
        out.extend(pts[-last_points:])
    return out


async def boj(ctx: Ctx) -> Result:
    res = ctx.begin()
    for ref in ctx.settings["series"]:
        db, code = ref.split(":", 1)
        data = await ctx.json(BOJ, params={"format": "json", "lang": "jp", "db": db, "code": code}, max_bytes=3 * 1024 * 1024)
        status = data.get("STATUS") if isinstance(data, dict) else None
        if status not in (200, "200"):
            ctx.notes.append(f"{ref}: 日銀が受け付けませんでした（{str((data or {}).get('MESSAGE') or status)[:60]}）")
            continue
        res.metrics.extend(boj_metrics(data, db=db, last_points=ctx.settings["last_points"]))
    res.note = "; ".join(ctx.notes)
    return res


# ---- e-Stat ----

_YM = re.compile(r"(\d{4})年(?:\s*(\d{1,2})月)?")


def estat_time(code: str, name: str) -> datetime | None:
    """e-Stat の時間軸（名前「2020年12月」・コード 2020001212 など）を日付に。名前を先に見て、読めなければコードの頭 4 桁（年）。"""
    m = _YM.search(name or "")
    try:
        if m:
            return datetime(int(m.group(1)), int(m.group(2) or 1), 1, tzinfo=_UTC)
        if re.fullmatch(r"\d{10}", str(code)):
            return datetime(int(str(code)[:4]), 1, 1, tzinfo=_UTC)
    except ValueError:
        return None
    return None


def estat_metrics(data: object, *, table: str) -> tuple[list[dict], str]:
    """getStatsData の応答（JSON）を Metric の点にする。分類はコードを subject に、名前を条件に。欠損の記号（-・…・x）は点にしない。{点, 注意}。"""
    root = data.get("GET_STATS_DATA") if isinstance(data, dict) else None
    if not isinstance(root, dict):
        return [], "応答の形が違います"
    status = (root.get("RESULT") or {}).get("STATUS")
    if status not in (0, 1, 2, "0", "1", "2"):
        return [], f"e-Stat がエラーを返しました（{str((root.get('RESULT') or {}).get('ERROR_MSG') or status)[:80]}）"
    sd = root.get("STATISTICAL_DATA") or {}
    classes: dict[str, dict[str, dict]] = {}
    objs = (sd.get("CLASS_INF") or {}).get("CLASS_OBJ") or []
    for o in objs if isinstance(objs, list) else [objs]:
        cl = o.get("CLASS") or []
        classes[str(o.get("@id"))] = {str(c.get("@code")): c for c in (cl if isinstance(cl, list) else [cl]) if isinstance(c, dict)}
    vals = (sd.get("DATA_INF") or {}).get("VALUE") or []
    out: list[dict] = []
    for v in vals if isinstance(vals, list) else [vals]:
        if not isinstance(v, dict):
            continue
        val = _num(v.get("$"))
        if val is None:
            continue
        tcode = str(v.get("@time") or "")
        tname = str((classes.get("time", {}).get(tcode) or {}).get("@name") or "")
        at = estat_time(tcode, tname)
        if at is None:
            continue
        dims = [(k[1:], str(c)) for k, c in v.items() if k.startswith("@") and k[1:] not in ("time", "unit", "annotation") and c not in (None, "")]
        names = [str((classes.get(k, {}).get(c) or {}).get("@name") or c) for k, c in dims]
        sub = f"stat:estat:{table}" + "".join(f":{k}={c}" for k, c in dims)
        unit = str(v.get("@unit") or "")
        out.append(_metric(sub, "value", val, unit, at, "estat", " / ".join(names)))
    return out, ""


async def estat(ctx: Ctx) -> Result:
    res = ctx.begin()
    for table in ctx.settings["tables"]:
        data = await ctx.json(ESTAT, params={"statsDataId": table, "limit": ctx.settings["limit"], "metaGetFlg": "Y", "lang": "J"}, max_bytes=8 * 1024 * 1024)
        pts, why = estat_metrics(data, table=table)
        if why:
            ctx.notes.append(f"{table}: {why}")
        res.metrics.extend(pts)
    res.note = "; ".join(ctx.notes)
    return res


# ---- 国会会議録 ----

def kokkai_docs(data: object, *, term: str) -> list[dict]:
    """speech API の応答（JSON）を record の文書（L1）にする。発言の冒頭だけ保存（発言は発言者の著作物）。話者・院・会議・回・日付を持つ。"""
    out = []
    recs = data.get("speechRecord") if isinstance(data, dict) else None
    for r in recs if isinstance(recs, list) else []:
        if not isinstance(r, dict) or not r.get("speechID") or str(r.get("speaker") or "") == "会議録情報":      # 会議の見出しの行（speechOrder 0）は発言ではない
            continue
        url = sources.safe_url(r.get("speechURL"))
        speaker = str(r.get("speaker") or "")[:60]
        house, meeting = str(r.get("nameOfHouse") or ""), str(r.get("nameOfMeeting") or "")
        issue, date = str(r.get("issue") or ""), str(r.get("date") or "")[:10]
        out.append({"kind": "record", "ids": {"ref": f"kokkai:{r['speechID']}", **({"url": url} if url else {})}, "title": f"{speaker}（{house} {meeting} {issue}）".strip()[:200],
                    "url": url or "", "source": "国会会議録検索システム", "published_at": sources.parse_time(date), "abstract": sources.plain(r.get("speech"), 600), "tier": 1,
                    "reason": f"国会会議録の検索（語「{term}」）", "ai_use": _ai("kokkai"), "connector": "kokkai",
                    "meta": {"speaker": speaker, "house": house, "meeting": meeting, "issue": issue, "date": date, "speech_id": str(r["speechID"]), "meeting_url": sources.safe_url(r.get("meetingURL")),
                             "term": term, "speaker_position": str(r.get("speakerPosition") or "")[:60]}})
    return out


async def kokkai(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    today = _day()
    for term in s["terms"]:
        params = {"any": term, "maximumRecords": s["per_term"], "recordPacking": "json", "from": (today - timedelta(days=s["days"])).strftime("%Y-%m-%d"), "until": today.strftime("%Y-%m-%d")}
        if s["house"]:
            params["nameOfHouse"] = s["house"]
        data = await ctx.json(KOKKAI, params=params, max_bytes=6 * 1024 * 1024)
        if isinstance(data, dict) and data.get("message"):
            ctx.notes.append(f"{term}: {str(data['message'])[:60]}")
        res.docs.extend(kokkai_docs(data, term=term))
    res.note = "; ".join(ctx.notes)
    return res


# ---- e-Gov 法令 ----

def law_stage(rev: dict, today: datetime | None = None) -> tuple[str, bool]:
    """法令の版の段と、「いまの版か」。廃止→repealed・施行日が先→enacted（成立・未施行）・それ以外→in_force。提案（proposed）は取り込み元が無いのでここでは作らない。"""
    status = str(rev.get("current_revision_status") or "")
    repeal = str(rev.get("repeal_status") or "None")
    if status == "Repeal" or repeal not in ("None", ""):
        return "repealed", False
    enf = sources.parse_time(rev.get("amendment_enforcement_date"))
    if status == "UnEnforced" or (enf is not None and enf > (today or datetime.now(_UTC))):
        return "enacted", False
    return "in_force", status != "PreviousEnforced"


def law_docs(data: object, *, today: datetime | None = None, max_revisions: int = 30) -> tuple[list[dict], list[dict]]:
    """law_revisions の応答（JSON）を law の文書（版ごと）と、版どうしの辺（新しい版 → 古い版。supersedes）にする。"""
    info = data.get("law_info") if isinstance(data, dict) and isinstance(data.get("law_info"), dict) else {}
    revs = data.get("revisions") if isinstance(data, dict) else None
    law_id = str(info.get("law_id") or "")
    docs: list[dict] = []
    for r in (revs if isinstance(revs, list) else [])[:max_revisions]:
        if not isinstance(r, dict) or not r.get("law_revision_id"):
            continue
        stage, current = law_stage(r, today)
        rid = str(r["law_revision_id"])
        title = str(r.get("law_title") or "")[:160]
        amend = str(r.get("amendment_law_title") or "")
        enf, prom = str(r.get("amendment_enforcement_date") or ""), str(r.get("amendment_promulgate_date") or "")
        docs.append({"kind": "law", "ids": {"ref": f"egov:{rid}", "url": f"https://laws.e-gov.go.jp/law/{rid}"}, "title": f"{title}（{amend or '制定'}）"[:300],
                     "url": f"https://laws.e-gov.go.jp/law/{rid}", "source": "e-Gov 法令検索", "published_at": sources.parse_time(prom), "version": rid[:64], "tier": 1,
                     "abstract": f"{title} {str(r.get('law_num') or info.get('law_num') or '')} 公布 {prom[:10]} 施行 {enf[:10] or '未定'} 段 {stage}", "reason": "e-Gov 法令の版の履歴", "ai_use": _ai("egov_law"),
                     "connector": "egov_law", "meta": {"law_id": law_id or str(r.get("law_id") or ""), "law_num": str(r.get("law_num") or info.get("law_num") or ""), "stage": stage, "current": current,
                                                       "promulgated": prom[:10], "enforced": enf[:10], "amendment_law_num": str(r.get("amendment_law_num") or ""), "amendment_title": amend[:160],
                                                       "revision_status": str(r.get("current_revision_status") or ""), "repeal_status": str(r.get("repeal_status") or "")}})
    docs.sort(key=lambda d: (d["meta"]["enforced"] or "9999", d["version"]))
    links = [{"src": docs[i], "dst": docs[i - 1], "type": "supersedes"} for i in range(1, len(docs))]
    return docs, links


async def egov_law(ctx: Ctx) -> Result:
    res = ctx.begin()
    for ref in ctx.settings["laws"]:
        try:
            data = await ctx.json(EGOV + quote(ref, safe=""), params={"response_format": "json"})
        except net.HttpError as exc:
            if exc.status in (400, 404):
                ctx.notes.append(f"{ref[:30]}: 見つかりません")
                continue
            raise
        docs, links = law_docs(data, max_revisions=ctx.settings["max_revisions"])
        res.docs.extend(docs)
        res.links.extend(links)
    res.note = "; ".join(ctx.notes)
    return res


# ---- EDINET ----

def edinet_docs(data: object, *, sec_codes: list[str], limit: int) -> list[dict]:
    """documents.json（type=2）の応答を filing の文書（知らせ）にする。証券コードを指定したらその会社だけ。指定が無ければ証券コードのある（上場）会社だけ。"""
    rows = data.get("results") if isinstance(data, dict) else None
    want = {c[:4] for c in sec_codes}
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("docID"):
            continue
        sec = str(r.get("secCode") or "")
        if (want and sec[:4] not in want) or (not want and not sec):
            continue
        name = str(r.get("filerName") or "")[:80]
        desc = str(r.get("docDescription") or "")[:120]
        out.append({"kind": "filing", "ids": {"ref": f"edinet:{r['docID']}"}, "title": f"{name}: {desc}"[:300], "url": "", "source": "EDINET", "published_at": sources.parse_time(str(r.get("submitDateTime") or "").replace(" ", "T")),
                    "abstract": f"{name}（証券コード {sec[:4]}）が {desc} を提出", "tier": 1, "reason": "EDINET の提出書類の知らせ", "ai_use": _ai("edinet"), "connector": "edinet",
                    "meta": {"doc_id": str(r["docID"]), "edinet_code": str(r.get("edinetCode") or ""), "sec_code": sec, "filer": name, "doc_type_code": str(r.get("docTypeCode") or ""),
                             "form_code": str(r.get("formCode") or ""), "period_end": str(r.get("periodEnd") or ""), "submitted": str(r.get("submitDateTime") or "")}})
        if len(out) >= limit:
            break
    return out


async def edinet(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    today = _day()
    for i in range(s["days"]):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        data = await ctx.json(EDINET, params={"date": day, "type": 2}, max_bytes=8 * 1024 * 1024)
        res.docs.extend(edinet_docs(data, sec_codes=s["sec_codes"], limit=s["limit"] - len(res.docs)))
        if len(res.docs) >= s["limit"]:
            break
    return res


# ---- DefiLlama ----

def llama_chain_metrics(data: object, chains: list[str], at: datetime) -> list[dict]:
    want = {c.lower() for c in chains}
    out = []
    for r in data if isinstance(data, list) else []:
        if isinstance(r, dict) and str(r.get("name") or "").lower() in want and _num(r.get("tvl")) is not None:
            out.append(_metric(f"chain:{r['name']}", "tvl", _num(r["tvl"]), "USD", at, "defillama", "チェーンの TVL（DefiLlama）"))
    return out


def llama_protocol_metrics(data: object, *, protocols: list[str], rwa_top: int, at: datetime) -> list[dict]:
    """protocols の応答から、監視のプロトコルの TVL と、category が RWA のものの上位の TVL。RWA は rwa_token:（RWA トークン）の名前空間。デジタル証券は digital_security: で別に持つ（ここでは作らない）。"""
    rows = [r for r in data if isinstance(r, dict) and r.get("slug") and _num(r.get("tvl")) is not None] if isinstance(data, list) else []
    want = set(protocols)
    out, seen = [], set()
    rwa = sorted((r for r in rows if str(r.get("category") or "").upper() == "RWA"), key=lambda r: -float(_num(r["tvl"]) or 0))[: max(0, rwa_top)]
    for r in rwa:
        seen.add(r["slug"])
        out.append(_metric(f"rwa_token:{r['slug']}", "tvl", _num(r["tvl"]), "USD", at, "defillama", f"RWA の TVL（DefiLlama・{str(r.get('name') or '')[:60]}）"))
    for r in rows:
        if r["slug"] in want and r["slug"] not in seen:
            out.append(_metric(f"protocol:{r['slug']}", "tvl", _num(r["tvl"]), "USD", at, "defillama", f"プロトコルの TVL（DefiLlama・{str(r.get('name') or '')[:60]}）"))
    return out


def llama_stable_metrics(data: object, top: int, at: datetime) -> list[dict]:
    """stablecoins の応答から、供給量の上位。単位は pegged<通貨> の通貨（USD など）。"""
    assets = data.get("peggedAssets") if isinstance(data, dict) else None
    rows = []
    for a in assets if isinstance(assets, list) else []:
        circ = a.get("circulating") if isinstance(a, dict) and isinstance(a.get("circulating"), dict) else {}
        for key, v in circ.items():
            if key.startswith("pegged") and _num(v) is not None:
                rows.append((float(_num(v)), a, key[6:] or "USD"))
                break
    rows.sort(key=lambda t: -t[0])
    out = []
    for val, a, cur in rows[: max(0, top)]:
        sym = str(a.get("symbol") or a.get("name") or "")[:20]
        out.append(_metric(f"stablecoin:{sym}", "circulating", val, cur, at, "defillama", f"ステーブルコインの供給量（DefiLlama・{str(a.get('name') or '')[:60]}）"))
    return out


async def defillama(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    at = _day()
    if s["chains"]:
        res.metrics.extend(llama_chain_metrics(await ctx.json(f"{LLAMA}/v2/chains", with_key=False), s["chains"], at))
    if s["stablecoins_top"]:
        res.metrics.extend(llama_stable_metrics(await ctx.json(LLAMA_STABLE, params={"includePrices": "true"}, with_key=False, max_bytes=MAX_LLAMA_BYTES), s["stablecoins_top"], at))
    if s["protocols"] or s["rwa_top"]:
        res.metrics.extend(llama_protocol_metrics(await ctx.json(f"{LLAMA}/protocols", with_key=False, max_bytes=MAX_LLAMA_BYTES), protocols=s["protocols"], rwa_top=s["rwa_top"], at=at))
    return res


# ---- CoinGecko ----

def coingecko_metrics(data: object, vs: list[str], at: datetime) -> list[dict]:
    out = []
    for coin, row in (data.items() if isinstance(data, dict) else []):
        if not isinstance(row, dict):
            continue
        for cur in vs:
            val = _num(row.get(cur))
            if val is None:
                continue
            ts = _num(row.get(f"{cur}_last_updated_at"))
            when = datetime.fromtimestamp(ts, _UTC) if ts else at
            out.append(_metric(f"asset:{coin}", f"price_{cur}", val, cur.upper(), when, "coingecko", "現在値（CoinGecko・Demo）"))
    return out


async def coingecko(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    data = await ctx.json(COINGECKO, params={"ids": ",".join(s["coins"]), "vs_currencies": ",".join(s["vs"]), "include_last_updated_at": "true"})
    res.metrics.extend(coingecko_metrics(data, s["vs"], _day()))
    return res


# ---- GDELT ----

def gdelt_docs(data: object, *, query: str) -> list[dict]:
    """DOC 2.0 の artlist（JSON）を L0 の文書にする。機械の抽出なので verification=unverified（未確認）の印をつける。"""
    out = []
    arts = data.get("articles") if isinstance(data, dict) else None
    for a in arts if isinstance(arts, list) else []:
        url = sources.safe_url(a.get("url")) if isinstance(a, dict) else None
        if not url:
            continue
        seen = str(a.get("seendate") or "")
        when = None
        try:
            when = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=_UTC)
        except ValueError:
            pass
        out.append({"kind": "other", "ids": {"url": url}, "title": sources.plain(a.get("title"), 200) or url, "url": url, "source": f"GDELT（{str(a.get('domain') or '')[:60]}）", "published_at": when, "tier": 0,
                    "reason": f"GDELT の語の検索「{query}」で見つかった（未確認）", "ai_use": _ai("gdelt"), "connector": "gdelt",
                    "meta": {"verification": "unverified", "provider": "GDELT Project", "domain": str(a.get("domain") or "")[:80], "language": str(a.get("language") or "")[:30],
                             "country": str(a.get("sourcecountry") or "")[:40], "query": query}})
    return out


async def gdelt(ctx: Ctx) -> Result:
    res = ctx.begin()
    s = ctx.settings
    for q in s["queries"]:
        try:
            data = await ctx.json(GDELT, params={"query": q, "mode": "artlist", "format": "json", "maxrecords": s["max_records"], "timespan": s["timespan"], "sort": "datedesc"}, with_key=False)
        except net.HttpError as exc:
            if exc.status == 429:      # 「5 秒に 1 回」の制限（2026-11-25 の実走で、続けて 2 回目から 429 になった）。残りの語は次の回へ
                ctx.notes.append("GDELT のレートの上限（429）に当たったため、残りの語は次の回にします")
                break
            raise
        except net.NetError as exc:
            if "JSON" in str(exc):      # 制限のときは JSON でなく文章が返る
                ctx.notes.append(f"{q[:20]}: GDELT が JSON を返しませんでした（制限の可能性）")
                break
            raise
        res.docs.extend(gdelt_docs(data, query=q))
    res.note = "; ".join(ctx.notes)
    return res


# ---- PyPI・npm ----

def pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pypi_docs(data: object, name: str, per_package: int) -> list[dict]:
    """PyPI の JSON から、公開の新しい順に per_package 件の版を release の文書にする（取り下げ〔yanked〕の版は印をつけて残す）。"""
    if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
        return []
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    vers = []
    for ver, files in data["releases"].items():
        times = [sources.parse_time(f.get("upload_time_iso_8601")) for f in files or [] if isinstance(f, dict)]
        times = [t for t in times if t]
        if times:
            vers.append((min(times), str(ver), any(f.get("yanked") for f in files if isinstance(f, dict))))
    vers.sort(reverse=True)
    out = []
    for when, ver, yanked in vers[:per_package]:
        url = f"https://pypi.org/project/{name}/{ver}/"
        out.append({"kind": "release", "ids": {"ref": f"pypi:{pypi_name(name)}@{ver}", "url": url}, "title": f"{name} {ver}", "url": url, "source": "PyPI", "published_at": when, "version": ver,
                    "abstract": sources.plain(info.get("summary"), 300), "tier": 1, "reason": "PyPI の監視のパッケージの版", "ai_use": _ai("pypi"), "connector": "pypi",
                    "meta": {"ecosystem": "PyPI", "package": name, "version": ver, "yanked": yanked, "prerelease": bool(re.search(r"[a-zA-Z]", ver)), "latest": ver == str(info.get("version") or "")}})
    return out


async def pypi(ctx: Ctx) -> Result:
    res = ctx.begin()
    for name in ctx.settings["packages"]:
        try:
            data = await ctx.json(f"{PYPI}{quote(name, safe='')}/json", with_key=False, max_bytes=8 * 1024 * 1024)
        except net.HttpError as exc:
            if exc.status == 404:
                ctx.notes.append(f"{name}: 見つかりません")
                continue
            raise
        res.docs.extend(pypi_docs(data, name, ctx.settings["per_package"]))
    res.note = "; ".join(ctx.notes)
    return res


def npm_docs(data: object, name: str) -> list[dict]:
    """npm の省略形のメタデータから、dist-tags の版（latest・next など。同じ版は 1 件）を release の文書にする。公開日時は省略形に無いので持たない（modified は meta に）。"""
    tags = data.get("dist-tags") if isinstance(data, dict) and isinstance(data.get("dist-tags"), dict) else {}
    by_ver: dict[str, list[str]] = {}
    for tag, ver in list(tags.items())[:8]:
        if isinstance(ver, str) and ver:
            by_ver.setdefault(ver, []).append(str(tag))
    out = []
    for ver, tg in by_ver.items():
        url = f"https://www.npmjs.com/package/{name}/v/{ver}"
        out.append({"kind": "release", "ids": {"ref": f"npm:{name}@{ver}", "url": url}, "title": f"{name} {ver}", "url": url, "source": "npm", "published_at": None, "version": ver, "tier": 1,
                    "abstract": "", "reason": "npm の監視のパッケージの版", "ai_use": _ai("npm"), "connector": "npm",
                    "meta": {"ecosystem": "npm", "package": name, "version": ver, "tags": tg, "latest": "latest" in tg, "prerelease": "-" in ver, "registry_modified": str(data.get("modified") or "")[:30]}})
    return out


async def npm(ctx: Ctx) -> Result:
    res = ctx.begin()
    for name in ctx.settings["packages"]:
        try:
            data = await ctx.json(NPM + quote(name, safe="@"), accept="application/vnd.npm.install-v1+json", with_key=False, max_bytes=4 * 1024 * 1024)
        except net.HttpError as exc:
            if exc.status == 404:
                ctx.notes.append(f"{name}: 見つかりません")
                continue
            raise
        res.docs.extend(npm_docs(data, name))
    res.note = "; ".join(ctx.notes)
    return res


# ---- OSV ----

def osv_docs(data: object, *, ecosystem: str, name: str, per_package: int) -> list[dict]:
    """OSV の query の応答（vulns）を、更新の新しい順に per_package 件、advisory の文書にする。深刻度・別名・修正済みの版を meta に。取り下げ（withdrawn）は印をつける。"""
    vulns = data.get("vulns") if isinstance(data, dict) else None
    rows = [v for v in vulns if isinstance(v, dict) and v.get("id")] if isinstance(vulns, list) else []
    rows.sort(key=lambda v: str(v.get("modified") or ""), reverse=True)
    out = []
    for v in rows[:per_package]:
        vid = str(v["id"])
        fixed = sorted({str(e["fixed"]) for a in v.get("affected") or [] if isinstance(a, dict) for rg in a.get("ranges") or [] if isinstance(rg, dict)
                        for e in rg.get("events") or [] if isinstance(e, dict) and e.get("fixed")})[:10]
        sev = next((str(s.get("score")) for s in v.get("severity") or [] if isinstance(s, dict) and s.get("score")), "") or str((v.get("database_specific") or {}).get("severity") or "")
        summary = sources.plain(v.get("summary"), 200) or sources.plain(v.get("details"), 120)
        url = f"https://osv.dev/vulnerability/{quote(vid, safe='')}"
        out.append({"kind": "advisory", "ids": {"ref": f"osv:{vid}", "url": url}, "title": f"{vid}: {summary}"[:300], "url": url, "source": "OSV", "published_at": sources.parse_time(v.get("published")),
                    "version": str(v.get("modified") or "")[:64], "abstract": sources.plain(v.get("details"), 800), "tier": 1, "reason": f"OSV の監視のパッケージ {ecosystem}:{name} の脆弱性",
                    "ai_use": _ai("osv"), "connector": "osv",
                    "meta": {"ecosystem": ecosystem, "package": name, "aliases": [str(x) for x in (v.get("aliases") or [])[:10]], "severity": sev[:80], "fixed": fixed,
                             "modified": str(v.get("modified") or ""), "withdrawn": bool(v.get("withdrawn"))}})
    return out


async def osv(ctx: Ctx) -> Result:
    res = ctx.begin()
    for ref in ctx.settings["packages"]:
        eco, name = ref.split(":", 1)
        data = await ctx.post_json(OSV, {"package": {"name": name, "ecosystem": eco}}, max_bytes=8 * 1024 * 1024)
        res.docs.extend(osv_docs(data, ecosystem=eco, name=name, per_package=ctx.settings["per_package"]))
    return res
