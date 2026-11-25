"""翻訳の流れ（translate.py の部品を、記事に当てる順番）。ARCHITECTURE 8.5。

1. 英文の概要: LLM の下ごしらえの日本語の概要（summary_ja）があれば、それを使い、概要は機械翻訳しない。
   LLM が無い・止まっている・上限なら、**本文の先頭 3 文（lead-3）**、本文が無ければ RSS の概要を英文の概要とする。
   ただし **LLM が長く使えないとき（無い・1 日の上限・設定の時間以上、下ごしらえが進まない）は、題と本文だけを訳す**（利用者の指示。概要は本文の訳の先頭 3 文から作る）。
   LLM が使えるのに、その記事の下ごしらえがまだなら、概要は待つ（summary_state=wait。済めば LLM の概要を使う。3 回失敗して諦めたら lead-3・RSS の概要を訳す）。
2. QuickMT で題と英文の概要を日本語に（日本語の記事は訳さない。言語の判定は translate.is_english）。
3. 本文は、重要な記事（チャンネルに合う・監視のThings・ホットトピック）を先に、空いている時間（取得中でない・お休みでない）に、1 日 body_per_day 件まで。
   残りは記事を開いたとき（API の POST /translate/articles/{id}）に訳す。
4. 重要な英語の記事（本文あり）は translate_hq の仕事を作る（ControlDeck が借りて高品質に訳す。jobs.py）。
- 1 回の巡回（maintenance の 1 分ごと）は時間の枠（TICK_SECONDS）の中だけ動き、CPU を独占しない。書き込みはまとめ書き（stage_translation）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from qnapnewshub import bodies, rest, settings, translate, writer
from qnapnewshub import db as news_db
from qnapnewshub.db import NewsItem, NewsTr

logger = logging.getLogger("qnapnewshub.translate_flow")

_UTC = timezone.utc
CANDIDATE_DAYS = 14
TICK_SECONDS = 25            # 1 回の巡回で使う時間（秒）
TITLES_PER_TICK = 60
SUMMARY_MAX = 600            # 英文の概要の最大の文字数（RSS の概要・lead-3）
_TAGS = re.compile(r"<[^>]+>")
_ctx_cache: dict = {"at": 0.0, "ctx": None}


def _now() -> datetime:
    return datetime.now(_UTC).replace(tzinfo=None)


def body_text(db, item_id: int, limit: int) -> str:
    return bodies.get_body(db, item_id)[:limit]


# ---- LLM の状態 ----

def llm_state(db, cfg: dict | None = None) -> str:
    """ok・none（鍵もゲートウェイも無い・下ごしらえが切）・limit（1 日の上限）・stuck（下ごしらえが設定の時間以上進まない）。ok 以外は「LLM が使えない」。"""
    from qnapnewshub import llm, prep

    cfg = cfg or settings.load()["translate"]
    lc = settings.load()["llm"]
    gw = lc["gateway"]
    if not lc["prep_enabled"] or not (llm.key_is_set() or (gw["enabled"] and gw["base_url"])):
        return "none"
    st = llm.stats(lc["daily_limit"], lc["minute_limit"])
    if st["total"] >= st["daily_limit"]:
        return "limit"
    now = _now()
    waiting = db.scalar(select(func.count(NewsItem.id)).where(
        NewsItem.prep_tries < prep.MAX_TRIES, NewsItem.kind != "event", NewsItem.fetched_at < now - timedelta(hours=cfg["llm_wait_hours"]),
        NewsItem.published_at >= now - timedelta(days=3))) or 0
    return "stuck" if waiting >= 5 else "ok"


# ---- 重要度 ----

def _context(db) -> dict:
    """重要度の規則に使う語（チャンネル・ホットトピック・監視のThings）。10 分は使い回す。"""
    if _ctx_cache["ctx"] is not None and time.monotonic() - _ctx_cache["at"] < 600:
        return _ctx_cache["ctx"]
    from qnapnewshub import connectors, docstore

    try:
        ctx = docstore.rules_context(db, connectors.promotion_config())
    except Exception:  # noqa: BLE001 - 集計が無くても翻訳は動く
        ctx = {"channels": [], "hot": [], "watch": []}
    _ctx_cache.update(at=time.monotonic(), ctx=ctx)
    return ctx


def reset_context() -> None:
    _ctx_cache.update(at=0.0, ctx=None)


def importance(db, rows: list) -> dict[int, tuple[float, str]]:
    """{記事の id: (点, 理由)}。理由が空でない記事が「重要」（チャンネルに合う・ホットトピック・監視のThings）。点は並べる順番に使う。"""
    from qnapnewshub import channels

    ctx = _context(db)
    out: dict[int, tuple[float, str]] = {}
    ids = [r.id for r in rows]
    ents: dict[int, int] = {}
    if ids:
        for aid, n in db.execute(select(news_db.ArticleEntity.article_id, func.count()).where(news_db.ArticleEntity.article_id.in_(ids)).group_by(news_db.ArticleEntity.article_id)):
            ents[aid] = int(n)
    for r in rows:
        text = f"{r.title}\n{r.summary or ''}"
        low = text.lower()
        score, reason = min(ents.get(r.id, 0), 6) * 0.3, ""
        for ch in ctx["channels"]:
            if channels.matches(ch, kind=r.kind, source_key=r.source_key, title=r.title, summary=r.summary or ""):
                score, reason = score + 3, f"チャンネル「{ch['name']}」"
                break
        hit = next((t for t in ctx["hot"] if t.lower() in low), "")
        if hit:
            score, reason = score + 2, reason or f"ホットトピック「{hit}」"
        hit = next((t for t in ctx["watch"] if t.lower() in low), "")
        if hit:
            score, reason = score + 2, reason or f"監視「{hit}」"
        out[r.id] = (score, reason)
    return out


def hq_candidates(db, limit: int, input_v: str) -> list[int]:
    """translate_hq の仕事を作る記事: 直近 7 日の英語の記事で、本文を取り出せて、重要で、まだ仕事が無い。"""
    from qnapnewshub.db import Job

    since = _now() - timedelta(days=7)
    rows = db.scalars(select(NewsItem).where(NewsItem.body_state == 1, NewsItem.kind.not_in(("event",)), NewsItem.dup_of.is_(None), NewsItem.published_at >= since,
                                             ~select(Job.id).where(Job.kind == "translate_hq", Job.input_v == input_v, Job.ref_id == NewsItem.id).exists())
                      .order_by(NewsItem.published_at.desc()).limit(400)).all()
    rows = [r for r in rows if translate.is_english(r.title)]
    imp = importance(db, rows)
    pick = sorted((r for r in rows if imp[r.id][1]), key=lambda r: -imp[r.id][0])
    return [r.id for r in pick[:limit]]


def hq_record(result: object, model: str) -> dict | None:
    """ControlDeck が返した高品質の訳を検査して、保存の形にする。title_ja・summary_ja・body_ja は文字列（日本語の文字を含む）。合わなければ None。"""
    if not isinstance(result, dict):
        return None
    limits = {"title_ja": 400, "summary_ja": 1500, "body_ja": 120_000}
    rec: dict = {}
    for k, lim in limits.items():
        v = result.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, str) or not translate._JA.search(v):      # noqa: SLF001
            return None
        rec[k] = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", v)[:lim].strip()
    if not rec:
        return None
    rec.update(kind="hq", engine=f"hq:{model[:70]}", at=datetime.now(_UTC), summary_state="done" if rec.get("summary_ja") else "", reset=True)
    if rec.get("summary_ja"):
        rec["summary_src"] = "hq"
    return rec


# ---- 英文の概要 ----

def english_summary(db, item: NewsItem, *, with_body: bool = True) -> tuple[str, str]:
    """(英文の概要, 出どころ lead3|rss)。本文があれば先頭 3 文、無ければ RSS の概要（タグは外す）。無ければ ("", "")。"""
    if with_body and item.body_state == 1:
        lead = translate.lead_sentences(bodies.get_body(db, item.id))
        if lead:
            return lead[:SUMMARY_MAX], "lead3"
    rss = re.sub(r"\s+", " ", _TAGS.sub(" ", item.summary or "")).strip()
    if len(rss) >= 30 and translate.is_english(rss):
        return rss[:SUMMARY_MAX], "rss"
    return "", ""


# ---- 1 回の巡回 ----

def idle() -> bool:
    """空いている時間か（取得の最中でない）。"""
    from qnapnewshub import service

    return not service._running.locked()      # noqa: SLF001


async def run_once() -> dict:
    """1 回の巡回。{"titles", "summaries", "bodies", "reason"}。切・お休み・モデル無し・取得の最中は動かない。"""
    out = {"titles": 0, "summaries": 0, "bodies": 0, "reason": ""}
    cfg = settings.load()["translate"]
    if cfg["engine"] == "off":
        out["reason"] = "off"
        return out
    if rest.blocks_analysis():
        out["reason"] = "rest"
        return out
    if not await asyncio.to_thread(translate.get_engine, cfg):
        out["reason"] = "no_model"
        return out
    if not idle():
        out["reason"] = "busy"
        return out
    return await asyncio.to_thread(work, cfg, out)


def work(cfg: dict, out: dict | None = None, *, budget: float = TICK_SECONDS) -> dict:
    """同期の本体（スレッドで）。試験はここを直接呼ぶ。"""
    out = out if out is not None else {"titles": 0, "summaries": 0, "bodies": 0, "reason": ""}
    engine = translate.get_engine(cfg)
    if engine is None:
        out["reason"] = "no_model"
        return out
    deadline = time.monotonic() + budget
    with translate.run_lock:
        with news_db.session() as db:
            state = llm_state(db, cfg)
            _titles(db, cfg, engine, state, deadline, out)
            if time.monotonic() < deadline:
                _waiting(db, cfg, engine, state, deadline, out)
            if cfg["body_enabled"] and time.monotonic() < deadline:
                _bodies(db, cfg, engine, deadline, out)
    return out


def _unsaved(item_id: int) -> bool:
    return writer.get().pending_translation(item_id) is not None


def _candidates(db) -> list[NewsItem]:
    since = _now() - timedelta(days=CANDIDATE_DAYS)
    done = select(NewsTr.item_id)
    rows = db.scalars(select(NewsItem).where(NewsItem.kind != "event", NewsItem.dup_of.is_(None), NewsItem.published_at >= since, NewsItem.id.not_in(done))
                      .order_by(NewsItem.published_at.desc()).limit(300)).all()
    return [r for r in rows if not _unsaved(r.id)]


def _stage(item_id: int, rec: dict, cache: dict) -> None:
    writer.get().stage_translation(item_id, rec, cache)


def _titles(db, cfg: dict, engine, state: str, deadline: float, out: dict) -> None:
    rows = _candidates(db)
    if not rows:
        return
    imp = importance(db, rows)
    rows.sort(key=lambda r: (-imp[r.id][0], -r.published_at.timestamp()))
    n = 0
    for item in rows:
        if n >= TITLES_PER_TICK or time.monotonic() > deadline:
            break
        n += 1
        if not translate.is_english(f"{item.title} {item.summary or ''}"[:600]) or not translate.is_english(item.title):
            _stage(item.id, {"kind": "ja", "engine": "", "at": datetime.now(_UTC)}, {})
            continue
        ctx = translate.Ctx(translate.entity_names(db, item.id), cfg)
        cache: dict = {}
        rec: dict = {"kind": "mt", "engine": engine.version, "at": datetime.now(_UTC), "add_units": 0, "add_fail": 0, "reset": True}
        title, ok = translate.translate_short(item.title, ctx, engine, cache)
        rec["add_units"] += 1
        if ok:
            rec["title_ja"] = title
        else:
            rec["add_fail"] += 1
        out["titles"] += 1
        _summary(db, item, cfg, ctx, engine, state, rec, cache, out)
        _stage(item.id, rec, cache)


def _summary(db, item: NewsItem, cfg: dict, ctx, engine, state: str, rec: dict, cache: dict, out: dict) -> None:
    """概要の扱いを rec に入れる（上のモジュールの説明の 1）。"""
    from qnapnewshub import prep

    if item.summary_ja:      # LLM の日本語の概要がある。機械翻訳しない
        rec["summary_state"] = ""
        return
    given_up = (item.prep_tries or 0) >= prep.MAX_TRIES and (item.prep_tries or 0) < prep.PREP_DONE
    if state != "ok":      # LLM が使えない: 題と本文だけ訳す。概要は本文の訳の先頭 3 文から（_bodies）
        rec["summary_state"] = ""
        return
    if not given_up:
        rec["summary_state"] = "wait"
        return
    _translate_summary(db, item, ctx, engine, rec, cache, out)


def _translate_summary(db, item: NewsItem, ctx, engine, rec: dict, cache: dict, out: dict) -> None:
    text, src = english_summary(db, item)
    rec["summary_state"] = "done"
    if not text:
        return
    ja, n_units, n_fail = translate.translate_text(text, ctx, engine, cache)
    rec["add_units"] = rec.get("add_units", 0) + n_units
    rec["add_fail"] = rec.get("add_fail", 0) + n_fail
    if n_fail == 0 and ja:
        rec["summary_ja"], rec["summary_src"] = ja, src
        out["summaries"] += 1


def _waiting(db, cfg: dict, engine, state: str, deadline: float, out: dict) -> None:
    """概要を待っている記事（summary_state=wait）。LLM の概要が付いたら待ちを解く。LLM が使えなくなった・3 回失敗して諦めたら、題だけにする／lead-3・RSS の概要を訳す。"""
    from qnapnewshub import prep

    rows = db.execute(select(NewsTr, NewsItem).join(NewsItem, NewsItem.id == NewsTr.item_id).where(NewsTr.summary_state == "wait", NewsTr.kind == "mt").limit(60)).all()
    for tr, item in rows:
        if time.monotonic() > deadline:
            break
        if _unsaved(item.id):
            continue
        if item.summary_ja:
            _stage(item.id, {"summary_state": ""}, {})
        elif state != "ok":
            _stage(item.id, {"summary_state": ""}, {})      # LLM が使えない: 題と本文だけ
        elif (item.prep_tries or 0) >= prep.MAX_TRIES:
            ctx = translate.Ctx(translate.entity_names(db, item.id), cfg)
            rec: dict = {"engine": tr.engine, "add_units": 0, "add_fail": 0}
            cache: dict = {}
            _translate_summary(db, item, ctx, engine, rec, cache, out)
            _stage(item.id, {"engine": None, **{k: v for k, v in rec.items() if k != "engine"}}, cache)


def _bodies_today(db) -> int:
    since = datetime.now(_UTC).astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(_UTC).replace(tzinfo=None)
    saved = db.scalar(select(func.count()).select_from(NewsTr).where(NewsTr.at >= since, NewsTr.body_ja != "")) or 0
    return int(saved) + writer.get().translations_today(datetime.now(_UTC).astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(_UTC))


def _bodies(db, cfg: dict, engine, deadline: float, out: dict) -> None:
    left = cfg["body_per_day"] - _bodies_today(db)
    if left <= 0:
        return
    rows = db.scalars(select(NewsItem).join(NewsTr, NewsTr.item_id == NewsItem.id).where(NewsItem.body_state == 1, NewsTr.kind == "mt", NewsTr.body_ja == "",
                                                                                       NewsItem.published_at >= _now() - timedelta(days=CANDIDATE_DAYS))
                      .order_by(NewsItem.published_at.desc()).limit(200)).all()
    rows = [r for r in rows if not _unsaved(r.id) or (writer.get().pending_translation(r.id) or {}).get("body_ja") is None]
    imp = importance(db, rows)
    rows = sorted((r for r in rows if imp[r.id][1]), key=lambda r: -imp[r.id][0])      # 背景で訳すのは重要な記事だけ。残りは開いたとき
    for item in rows[:left]:
        if time.monotonic() > deadline:
            break
        translate_body(db, item, cfg, engine, out=out, background=True)


def translate_body(db, item: NewsItem, cfg: dict, engine, *, out: dict | None = None, background: bool = False) -> dict:
    """記事 1 件の本文を訳して積む（題・概要が未なら一緒に）。返す: {"title_ja", "summary_ja", "body_ja", "n_units", "n_fail"}（積んだ分）。"""
    text = bodies.get_body(db, item.id)[:cfg["body_max_chars"]]
    ctx = translate.Ctx(translate.entity_names(db, item.id), cfg)
    cache: dict = {}
    body_ja, n_units, n_fail = translate.translate_text(text, ctx, engine, cache) if text else ("", 0, 0)
    rec: dict = {"kind": "mt", "engine": engine.version, "body_ja": body_ja, "add_units": n_units, "add_fail": n_fail}
    cur = translate.get_row(db, item.id) or {}
    if background:
        rec["at"] = datetime.now(_UTC)
        rec["body_at"] = rec["at"]
    got = {"body_ja": body_ja, "n_units": n_units, "n_fail": n_fail}
    if not cur.get("title_ja") and translate.is_english(item.title):
        title, ok = translate.translate_short(item.title, ctx, engine, cache)
        if ok:
            rec["title_ja"] = got["title_ja"] = title
    if not item.summary_ja and not cur.get("summary_ja") and body_ja:
        lead = _lead_ja(body_ja)
        if lead:
            rec["summary_ja"], rec["summary_src"], rec["summary_state"] = lead, "body", "done"
            got["summary_ja"] = lead
    if out is not None:
        out["bodies"] += 1
    _stage(item.id, rec, cache)
    return got


def _lead_ja(body_ja: str, n: int = 3) -> str:
    """訳した本文の先頭 n 文（未訳の文・短すぎる行は飛ばす）。"""
    got: list[str] = []
    for line in body_ja.split("\n"):
        for s in re.split(r"(?<=[。！？])", line):
            s = s.strip()
            if len(s) < 15 or s.startswith(translate.UNTRANSLATED):
                continue
            got.append(s)
            if len(got) >= n:
                return "".join(got)[:SUMMARY_MAX]
    return "".join(got)[:SUMMARY_MAX]


# ---- 記事を開いたとき（API） ----

class NotReady(Exception):
    """翻訳できない理由（利用者に見せてよい文）。"""


def ondemand(item_id: int) -> dict:
    """記事を開いたときの要求。本文を取り出してあれば本文を訳し、無ければ題・概要だけ。同期（スレッドで）。{"state": ..., "mt": {...}}。"""
    cfg = settings.load()["translate"]
    if cfg["engine"] == "off" or not cfg["ondemand"]:
        raise NotReady("翻訳は切になっています（設定の「翻訳」）")
    if rest.blocks_analysis():
        raise NotReady("お休みモードの間は翻訳しません")
    engine = translate.get_engine(cfg)
    if engine is None:
        raise NotReady("翻訳のモデルが入っていません（設定の「翻訳」で取得します）")
    with news_db.session() as db:
        item = db.get(NewsItem, item_id)
        if item is None or item.kind == "event":
            raise NotReady("記事が見つかりません")
        cur = translate.get_row(db, item_id) or {}
        if cur.get("kind") == "hq" and cur.get("body_ja"):
            return {"state": "done", "row": cur}
        if cur.get("body_ja") and str(cur.get("engine", "")) == engine.version:
            return {"state": "done", "row": cur}
        if not translate.is_english(item.title):
            raise NotReady("日本語の記事は翻訳しません")
        if not translate.run_lock.acquire(timeout=60):
            raise NotReady("翻訳が混み合っています。あとでもう一度")
        try:
            translate._note(ondemand=1)      # noqa: SLF001
            if item.body_state == 1:
                translate_body(db, item, cfg, engine)
            else:
                ctx = translate.Ctx(translate.entity_names(db, item.id), cfg)
                cache: dict = {}
                rec: dict = {"kind": "mt", "engine": engine.version, "add_units": 1, "add_fail": 0}
                title, ok = translate.translate_short(item.title, ctx, engine, cache)
                if ok:
                    rec["title_ja"] = title
                else:
                    rec["add_fail"] = 1
                if not item.summary_ja:
                    _translate_summary(db, item, ctx, engine, rec, cache, {"summaries": 0})
                _stage(item.id, rec, cache)
        finally:
            translate.run_lock.release()
        return {"state": "done", "row": translate.get_row(db, item_id) or {}}
