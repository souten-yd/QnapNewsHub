"""重複の除去（ARCHITECTURE 10 章）: URL の正規化・GUID・本文の抜粋の SimHash。

- 同じ記事の URL の揺れ（追跡の引数・末尾の /・http と https・AMP・www）は、正規化した URL（canon_url）が同じなら同じ記事。
  同じ取得先から来たら捨てる。別の取得先から来たら「同じ記事の別の出どころ」として保存し、代表の記事（dup_of）へまとめる。
- 同じ取得先の GUID が同じで URL だけ変わった記事は、同じ記事として捨てる。
- 本文の抜粋（無いときは題）の SimHash（64bit）のハミング距離が 3 以下で、48 時間の窓に入る別の取得先の記事は、近い重複。捨てずに代表へまとめる。
  代表は先に入った記事（代表がすでに別の記事の出どころなら、その代表）。一覧・ホーム・まとめは代表だけを 1 件として出し、`also` に出どころの数を持つ。
- 短すぎる文（正規化して 20 文字未満）は SimHash を作らない（短い見出しの偶然の一致でまとめない）。
- 将来のイベント（同じ出来事の報道の束）の土台。意味の近さは ControlDeck の仕事（v0.3）。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

WINDOW_HOURS = 48
HAMMING_MAX = 3
MIN_TEXT = 20
SHINGLE = 3
_UTC = timezone.utc

_TRACKING_EXACT = {"fbclid", "gclid", "gclsrc", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url", "_ga", "_gl", "spm",
                   "at_medium", "at_campaign", "cmpid", "ocid", "s_cid", "wt_mc", "src_nl", "ncid", "taid", "xtor", "zanpid"}
_AMP_PARAMS = {"amp", "outputtype", "output"}
_AMP_PATH_TAIL = re.compile(r"(?:/amp(?:/index\.html)?|\.amp(?:\.html)?|/amp\.html)$", re.I)
_AMP_PATH_HEAD = re.compile(r"^/amp(?=/)", re.I)


def _is_tracking(name: str) -> bool:
    low = name.lower()
    return low.startswith("utm_") or low in _TRACKING_EXACT


def normalize_url(url: str) -> str:
    """同じ記事の URL を同じ文字列にする。http→https・小文字のホスト・www と amp の前置き・既定のポート・fragment・追跡の引数・
    AMP の印（/amp・.amp.html・?amp=1・?output=amp）・末尾の /・引数の並び順を整える。読めない URL はそのまま返す。"""
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return text
    host = parts.hostname.lower()
    for prefix in ("www.", "amp.", "m."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = _AMP_PATH_TAIL.sub("", parts.path)
    path = _AMP_PATH_HEAD.sub("", path)
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if _is_tracking(name):
            continue
        if name.lower() in _AMP_PARAMS and value.lower() in ("1", "true", "amp", "yes", ""):
            continue
        query.append((name, value))
    query.sort()
    return urlunsplit(("https", netloc, path or "/", urlencode(query), ""))


def _clean_text(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in folded if ch.isalnum())


def simhash(text: str) -> int | None:
    """題＋抜粋の SimHash（64bit・符号つきの整数で返す＝SQLite の INTEGER に入る）。短すぎる文は None。
    文字の 3 つ組（空白・記号を除く。日本語にも英語にも効く）を blake2b で 64bit にして、出る回数で重みを付けて足す。"""
    body = _clean_text(text)
    if len(body) < MIN_TEXT:
        return None
    weights = [0] * 64
    counts: dict[str, int] = {}
    for i in range(len(body) - SHINGLE + 1):
        gram = body[i:i + SHINGLE]
        counts[gram] = counts.get(gram, 0) + 1
    for gram, n in counts.items():
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += n if (h >> bit) & 1 else -n
    value = 0
    for bit in range(64):
        if weights[bit] > 0:
            value |= 1 << bit
    return value - (1 << 64) if value >= (1 << 63) else value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def item_simhash(item: dict) -> int | None:
    """本文の抜粋の SimHash。抜粋が短いか無いとき（Google ニュースなど）は題を足す。
    抜粋が十分あるときは題を入れない（同じ配信記事でも、媒体ごとに題は違うため）。"""
    summary = item.get("summary") or ""
    if len(_clean_text(summary)) >= MIN_TEXT:
        return simhash(summary)
    return simhash(f"{item.get('title') or ''} {summary}")


class Known:
    """窓の中にすでにある記事の索引（DB・まだ書いていないバッファ・同じ回の先に入った記事）。"""

    def __init__(self) -> None:
        self.by_canon: dict[str, dict] = {}
        self.by_guid: dict[tuple[str, str], dict] = {}
        self.hashes: list[dict] = []
        self.urls: set[str] = set()

    def add(self, row: dict) -> None:
        """row: {id, url, source_key, canon_url, guid, simhash, dup_of, dup_of_url}。"""
        self.urls.add(row["url"])
        if row.get("canon_url"):
            self.by_canon.setdefault(row["canon_url"], row)
        if row.get("guid"):
            self.by_guid.setdefault((row["source_key"], row["guid"]), row)
        if row.get("simhash") is not None:
            self.hashes.append(row)


def _root(row: dict, known: Known) -> dict:
    """行が別の記事の出どころなら、その代表の行（代表が索引に無いときは、代表の id だけを持つ行）。"""
    if row.get("dup_of_url"):
        return next((r for r in known.hashes if r["url"] == row["dup_of_url"]), None) or {"id": None, "url": row["dup_of_url"]}
    if row.get("dup_of"):
        return {"id": row["dup_of"], "url": ""}
    return row


def annotate(items: list[dict], known: Known, *, now: datetime | None = None) -> tuple[list[dict], dict]:
    """取った記事に canon_url・guid・simhash・dup_of（または同じ回の代表の URL: dup_of_url）を付け、捨てるものを除く。
    すでに保存されている URL の記事は、そのまま通す（取り直し＝更新。重複の判定はしない）。(残す記事, 数) を返す。"""
    kept: list[dict] = []
    stats = {"dropped_same_source": 0, "dropped_guid": 0, "merged_url": 0, "merged_simhash": 0}
    batch: set[str] = set()
    for item in items:
        if item["url"] in batch:      # 同じ回に同じ URL が 2 回（後のものは捨てる。先の注釈を保つ）
            continue
        batch.add(item["url"])
        if item["url"] in known.urls:
            kept.append(item)
            continue
        canon = normalize_url(item["url"])
        guid = str(item.get("guid") or "")[:500]
        item["canon_url"], item["guid"] = canon[:1000], guid
        item["simhash"] = item_simhash(item)
        hit = known.by_canon.get(canon)
        if hit is not None:
            if hit["source_key"] == item["source_key"]:
                stats["dropped_same_source"] += 1
                continue
            _link(item, _root(hit, known))
            stats["merged_url"] += 1
        else:
            same = known.by_guid.get((item["source_key"], guid)) if guid else None
            if same is not None:
                stats["dropped_guid"] += 1
                continue
            near = _nearest(item, known)
            if near is not None:
                _link(item, _root(near, known))
                stats["merged_simhash"] += 1
        known.add({"id": None, "url": item["url"], "source_key": item["source_key"], "canon_url": canon, "guid": guid, "simhash": item["simhash"],
                   "dup_of": item.get("dup_of"), "dup_of_url": item.get("dup_of_url")})
        kept.append(item)
    return kept, stats


def _link(item: dict, rep: dict) -> None:
    if rep.get("id"):
        item["dup_of"] = rep["id"]
    else:
        item["dup_of_url"] = rep["url"]


def _nearest(item: dict, known: Known) -> dict | None:
    if item["simhash"] is None:
        return None
    best, best_d = None, HAMMING_MAX + 1
    for row in known.hashes:
        if row["source_key"] == item["source_key"]:      # 同じ取得先の中は、定型の題（週ごとのスレッドなど）が多いので、まとめない
            continue
        d = hamming(item["simhash"], row["simhash"])
        if d < best_d:
            best, best_d = row, d
    return best


def load_known(db, urls: list[str], canons: list[str], pending: list, now: datetime | None = None) -> Known:
    """DB の窓（48 時間）の記事・今回の URL／正規化 URL に当たる記事（期間を問わない）・まだ書いていない記事から索引を作る。"""
    from sqlalchemy import or_, select

    from qnapnewshub.db import NewsItem

    now = now or datetime.now(_UTC)
    since = (now - timedelta(hours=WINDOW_HOURS)).replace(tzinfo=None)
    known = Known()
    cols = (NewsItem.id, NewsItem.url, NewsItem.source_key, NewsItem.canon_url, NewsItem.guid, NewsItem.simhash, NewsItem.dup_of)

    def put(r) -> None:
        known.add({"id": r[0], "url": r[1], "source_key": r[2], "canon_url": r[3] or "", "guid": r[4] or "", "simhash": r[5], "dup_of": r[6]})

    for row in db.execute(select(*cols).where(NewsItem.fetched_at >= since, NewsItem.kind != "event")):
        put(row)
    for i in range(0, len(canons), 400):
        for row in db.execute(select(*cols).where(NewsItem.canon_url.in_(canons[i:i + 400]))):
            put(row)
    for i in range(0, len(urls), 400):
        for row in db.execute(select(*cols).where(NewsItem.url.in_(urls[i:i + 400]))):
            put(row)
    for p in pending:
        put((p.id, p.url, p.source_key, getattr(p, "canon_url", ""), getattr(p, "guid", ""), getattr(p, "simhash", None), getattr(p, "dup_of", None)))
    return known


def prepare(items: list[dict]) -> tuple[list[dict], dict]:
    """service から呼ぶ入口: DB とバッファから窓を読み、annotate する。"""
    from qnapnewshub import db as news_db
    from qnapnewshub import writer

    if not items:
        return items, {}
    canons = list({normalize_url(i["url"]) for i in items})
    with news_db.session() as db:
        known = load_known(db, [i["url"] for i in items], canons, writer.get().pending_items())
    return annotate(items, known)
