"""記事の本文の取り出し（Trafilatura）。QNAP 側で、重要度の高い記事から、1 日と 1 分の上限の中で記事のページの本文を取り出す。

- **本文は画面に出さない**（元のページを開く方針のまま）。使うのは 下ごしらえ（LLM）への入力（抜粋の代わりに本文の頭）と「本文あり」の印だけ。検索の索引にも入れない。
- 守ること（AGENTS.md 7）: 通信は net.get（私的な IP へ行かない・大きさと時間の上限・リダイレクトは各段を検査）。**robots.txt を守る**（リダイレクト先のホストも）。
  有料の記事・ロボット確認・ログインの画面・noarchive／noai の指示は**すり抜けない**。取れなければ理由を記録して（state=2）**二度と試さない**。
  robots.txt そのものが読めないとき（5xx・通信の失敗）だけは、その記事を記録せず後で試す（記事のせいではない）。
- 上限: 1 日 per_day 件（既定 200・試行は成功も失敗も数える）・1 分 per_minute 件（既定 6。maintenance の 1 分ごとの巡回が 1 回に取る件数）・ページ 2MB・本文 6 万文字・同じホストへは 3 秒あける。
- 順番（重要度）: 近い重複の数 × 3 ＋ 取り出した物の数 ＋ 新しさ（0〜2）。直近 14 日の新着の記事（kind=news・代表の記事だけ）から。
- 保存: Zstandard で圧縮した BLOB（news_body）。結果は Write Coordinator に積み（stage_body）、まとめ書きで書く。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from sqlalchemy import func, select

from qnapnewshub import db as news_db
from qnapnewshub import net, probe, rest, settings, writer, ztext
from qnapnewshub.db import NewsBody, NewsItem

logger = logging.getLogger("qnapnewshub.bodies")

KINDS = ("news",)
CANDIDATE_DAYS = 14
PAGE_MAX_BYTES = 2 * 1024 * 1024
BODY_MAX_CHARS = 60_000
MIN_CHARS = 200
HOST_PACE = 3.0
ROBOTS_TTL = 6 * 3600
ROBOTS_DEFER = 1800
TIMEOUT = 15
REASONS = {"robots": "robots.txt が許していません", "blocked": "アクセスが制限されています（ログイン・有料・拒否）", "gone": "ページがありません", "http": "HTTP のエラー",
           "format": "HTML のページではありません", "challenge": "ロボット確認の画面でした", "paywall": "有料の記事です", "noarchive": "保存しない指示（noarchive・noai）があります",
           "short": "本文を取り出せませんでした（短い・空）", "toolarge": "ページが大きすぎます", "network": "通信できませんでした", "private": "内部のアドレスへは取りに行きません"}
_UTC = timezone.utc
_PAYWALL = re.compile(r'"isAccessibleForFree"\s*:\s*"?false', re.I)
_NOARCHIVE = re.compile(r"(noarchive|noai|noimageai|none)", re.I)
_META_ROBOTS = re.compile(r"<meta[^>]+name=[\"']?(?:robots|googlebot)[\"']?[^>]*content=[\"']([^\"']+)", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_CHALLENGE = re.compile(r"just a moment|attention required|captcha|are you a robot|verify you are human|access denied|ロボットではありません|enable javascript and cookies", re.I)

_robots: dict[str, tuple[float, probe._Robots | None]] = {}      # ホスト -> (取った時刻, 判定。None は「robots.txt なし＝許す」)
_deferred: dict[str, float] = {}                                  # ホスト -> robots.txt が読めなかったときの再試行の時刻
_host_last: dict[str, float] = {}


async def _http(url: str, *, max_bytes: int = PAGE_MAX_BYTES, accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"):
    """1 回の GET（リダイレクトは追わない。呼ぶ側が各段で robots を確かめる）。試験はここを差し替える。"""
    return await net.get(url, accept=accept, timeout=TIMEOUT, follow=False, max_bytes=max_bytes)


class Deferred(Exception):
    """robots.txt が読めない（あとで試す。記事には記録しない）。"""


async def _robots_for(url: str) -> probe._Robots | None:
    parts = urlsplit(url)
    host = f"{parts.scheme}://{parts.netloc}"
    now = time.monotonic()
    hit = _robots.get(host)
    if hit and now - hit[0] < ROBOTS_TTL:
        return hit[1]
    if _deferred.get(host, 0) > now:
        raise Deferred(host)
    try:
        status, _, body = await _http(f"{host}/robots.txt", max_bytes=512 * 1024, accept="text/plain,*/*")
    except net.NetError as exc:
        _deferred[host] = now + ROBOTS_DEFER
        raise Deferred(host) from exc
    if 300 <= status < 400:      # robots.txt のリダイレクトは追わない（読めない扱い）
        _deferred[host] = now + ROBOTS_DEFER
        raise Deferred(host)
    if 500 <= status:
        _deferred[host] = now + ROBOTS_DEFER
        raise Deferred(host)
    rules = probe._Robots(body.decode("utf-8", "replace")) if status < 400 else None      # 4xx（robots.txt なし）は許す
    _robots[host] = (now, rules)
    return rules


async def _allowed(url: str) -> bool:
    rules = await _robots_for(url)
    return True if rules is None else rules.allowed(url)


def extract_text(html: bytes, url: str) -> str:
    """Trafilatura で本文（プレーンテキスト）を取り出す。取れなければ空。コメント・表は入れない（精度優先）。"""
    import trafilatura

    got = trafilatura.extract(html, url=url, include_comments=False, include_tables=False, favor_precision=True, output_format="txt")
    return (got or "").strip()


def _clean(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t　]+", " ", text)).strip()[:BODY_MAX_CHARS]


def _fail(reason: str, now: datetime) -> dict:
    return {"state": 2, "body": None, "raw_len": 0, "reason": reason, "at": now}


async def fetch_body(url: str, now: datetime | None = None) -> dict:
    """記事 1 件の本文を取る。結果は {"state": 1|2, "body": 圧縮した本文 | None, "raw_len", "reason", "at"}。
    robots.txt が読めないときは Deferred（記録しない）。ここでは書かない。"""
    now = now or datetime.now(_UTC)
    try:
        current = url
        status, head, page = 0, {}, b""
        for _ in range(net.MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
                return _fail("format", now)
            if not await _allowed(current):
                return _fail("robots", now)
            wait = HOST_PACE - (time.monotonic() - _host_last.get(parts.hostname, 0))
            _host_last[parts.hostname] = time.monotonic() + max(wait, 0)
            if wait > 0:
                await asyncio.sleep(min(wait, HOST_PACE))
            status, head, page = await _http(current)
            if status in (301, 302, 303, 307, 308) and head.get("location"):
                current = urljoin(current, head["location"])
                continue
            break
        else:
            return _fail("http", now)
    except Deferred:
        raise
    except net.NetError as exc:
        text = str(exc)
        return _fail("toolarge" if "大きすぎ" in text else "private" if "内部" in text or "アドレス" in text else "network", now)
    if status in (401, 402, 403, 407, 429, 451):
        return _fail("blocked", now)
    if status in (404, 410):
        return _fail("gone", now)
    if status >= 300:
        return _fail("http", now)
    ctype = (head.get("content-type") or "").lower()
    if ctype and "html" not in ctype and "xml" not in ctype:
        return _fail("format", now)
    if _NOARCHIVE.search(head.get("x-robots-tag", "")):
        return _fail("noarchive", now)
    top = page[:60_000].decode("utf-8", "ignore")
    meta = _META_ROBOTS.search(top)
    if meta and _NOARCHIVE.search(meta.group(1)):
        return _fail("noarchive", now)
    if _PAYWALL.search(top):
        return _fail("paywall", now)
    try:
        text = _clean(await asyncio.to_thread(extract_text, page, url))
    except Exception as exc:  # noqa: BLE001 - 壊れた HTML で止めない
        logger.info("本文の取り出しに失敗しました: %s", type(exc).__name__)
        return _fail("short", now)
    if len(text) < MIN_CHARS:
        title = _TITLE.search(top)
        return _fail("challenge" if title and _CHALLENGE.search(title.group(1)) else "short", now)
    return {"state": 1, "body": ztext.pack(text), "raw_len": len(text.encode("utf-8")), "reason": "", "at": now}


# ---- 設定・上限 ----

def config() -> dict:
    return settings.load()["body"]


def day_start(now: datetime | None = None) -> datetime:
    """今日の始まり（サーバーの地域の 0 時）の UTC。"""
    local = (now or datetime.now(_UTC)).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(_UTC)


def used_today(db, now: datetime | None = None) -> int:
    """今日の試行の数（保存済み ＋ まだ書いていない分。成功も失敗も数える）。"""
    since = day_start(now)
    saved = db.scalar(select(func.count()).select_from(NewsBody).where(NewsBody.at >= since.replace(tzinfo=None))) or 0
    return int(saved) + writer.get().bodies_today(since)


# ---- 重要度 ----

def candidates(db, limit: int, now: datetime | None = None) -> list[dict]:
    """本文を取る候補（重要度の高い順）。{"id", "url", "score"}。まだ試していない（body_state=0）・直近の新着の代表の記事。"""
    now = now or datetime.now(_UTC)
    since = (now - timedelta(days=CANDIDATE_DAYS)).replace(tzinfo=None)
    d = NewsItem.__table__.alias("d")
    dups = select(func.count()).select_from(d).where(d.c.dup_of == NewsItem.id).correlate(NewsItem).scalar_subquery()
    ae = news_db.ArticleEntity.__table__
    ents = select(func.count()).select_from(ae).where(ae.c.article_id == NewsItem.id).correlate(NewsItem).scalar_subquery()
    rows = db.execute(select(NewsItem.id, NewsItem.url, NewsItem.published_at, dups, ents).where(
        NewsItem.kind.in_(KINDS), NewsItem.dup_of.is_(None), NewsItem.body_state == 0, NewsItem.published_at >= since,
        NewsItem.url.like("http%"), NewsItem.url.not_like("%://news.google.com/%")).order_by(NewsItem.published_at.desc()).limit(600)).all()
    coord = writer.get()
    out = []
    for item_id, url, published, n_dups, n_ents in rows:
        if coord.has_unsaved_body(item_id):
            continue
        age = max(0.0, (now.replace(tzinfo=None) - published.replace(tzinfo=None)).total_seconds()) / (CANDIDATE_DAYS * 86400)
        out.append({"id": item_id, "url": url, "score": 3 * int(n_dups or 0) + int(n_ents or 0) + 2 * (1 - min(age, 1.0))})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


async def run_once(now: datetime | None = None) -> dict:
    """1 回の巡回: 上限（1 日・1 分）の中で、重要度の高い記事から本文を取り、結果を積む。
    {"tried", "ok", "failed", "deferred", "reason": 取らなかった理由 or ""}。"""
    cfg = config()
    out = {"tried": 0, "ok": 0, "failed": 0, "deferred": 0, "reason": ""}
    if not cfg["enabled"]:
        out["reason"] = "off"
        return out
    if rest.blocks_fetch() or rest.blocks_analysis():
        out["reason"] = "rest"
        return out
    if settings.in_quiet(settings.load(), datetime.now()):
        out["reason"] = "quiet"
        return out
    now = now or datetime.now(_UTC)

    def pick() -> list[dict]:
        with news_db.session() as db:
            left = cfg["per_day"] - used_today(db, now)
            return candidates(db, max(0, min(cfg["per_minute"], left)), now) if left > 0 else []

    todo = await asyncio.to_thread(pick)
    if not todo:
        out["reason"] = "none"
        return out

    async def one(c: dict) -> None:
        try:
            rec = await fetch_body(c["url"], now)
        except Deferred:
            out["deferred"] += 1
            return
        except Exception:  # noqa: BLE001 - 1 件の失敗で巡回を止めない
            logger.exception("本文の取り出しで予期しない失敗")
            rec = _fail("network", now)
        await asyncio.to_thread(writer.get().stage_body, c["id"], rec)
        out["tried"] += 1
        out["ok" if rec["state"] == 1 else "failed"] += 1

    await asyncio.gather(*[one(c) for c in todo])
    return out


# ---- 保存・読み ----

def apply(db, staged: dict[int, dict]) -> int:
    """積んだ結果を news_body へ（コミットは呼ぶ側。1 トランザクション）。"""
    for item_id, rec in staged.items():
        db.merge(NewsBody(item_id=item_id, state=rec["state"], body=rec.get("body"), raw_len=rec.get("raw_len", 0), reason=rec.get("reason", ""),
                          at=rec["at"]))
    db.flush()
    return len(staged)


def get_body(db, item_id: int) -> str:
    row = db.get(NewsBody, item_id)
    return ztext.unpack(row.body) if row is not None and row.state == 1 else ""


def leads(ids: list[int], chars: int = 1500) -> dict[int, str]:
    """記事ごとの本文の頭（下ごしらえの入力）。本文のある記事だけ。"""
    if not ids:
        return {}
    out: dict[int, str] = {}
    with news_db.session() as db:
        for item_id, blob in db.execute(select(NewsBody.item_id, NewsBody.body).where(NewsBody.item_id.in_(ids), NewsBody.state == 1)):
            try:
                out[item_id] = re.sub(r"\s+", " ", ztext.unpack(blob))[:chars]
            except ValueError:
                continue
    return out


def stats(db) -> dict:
    """診断: 今日の使い方・成否の数・失敗の理由・圧縮の効果。"""
    cfg = config()
    ok, failed = (db.scalar(select(func.count()).select_from(NewsBody).where(NewsBody.state == s)) or 0 for s in (1, 2))
    reasons = {r or "other": int(n) for r, n in db.execute(select(NewsBody.reason, func.count()).where(NewsBody.state == 2).group_by(NewsBody.reason))}
    raw, packed = db.execute(select(func.coalesce(func.sum(NewsBody.raw_len), 0), func.coalesce(func.sum(func.length(NewsBody.body)), 0)).where(NewsBody.state == 1)).one()
    cold_n, cold_raw, cold_z = db.execute(select(func.count(), func.coalesce(func.sum(news_db.NewsCold.raw_len), 0),
                                                 func.coalesce(func.sum(func.length(news_db.NewsCold.z)), 0))).one()
    return {"enabled": cfg["enabled"], "per_day": cfg["per_day"], "per_minute": cfg["per_minute"], "today": used_today(db), "ok": int(ok), "failed": int(failed),
            "reasons": reasons, "reason_labels": REASONS, "raw_bytes": int(raw), "packed_bytes": int(packed),
            "cold": {"items": int(cold_n), "raw_bytes": int(cold_raw), "packed_bytes": int(cold_z)}}
