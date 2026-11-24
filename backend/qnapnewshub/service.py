"""取得の実行（同時に 1 回だけ・ソースごとに時間切れと失敗を記録して他を止めない）と、見張り（取得の間隔・日刊・週刊のまとめの時刻）。"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from qnapnewshub import channels, connectors, dedup, event_llm, fetchlog, kg_job, gist, images, metrics, polling, prep, rest, settings, sources, store, trend, writer
from qnapnewshub import db as news_db
from qnapnewshub import net
from qnapnewshub.db import NewsItem

logger = logging.getLogger("qnapnewshub.service")

FIRST_DELAY_SECONDS = 60
SOURCE_TIMEOUT_SECONDS = 45
MIN_GAP_SECONDS = 30          # 「取り直す」の連打を抑える
CONCURRENCY = 4

_running = threading.Lock()
_status: dict[str, dict] = {}
_last_fetch: dict[str, float] = {}      # source_key -> 最後に取りに行った時刻（time.monotonic。見張りが間隔の来たものを選ぶ）
_last: dict = {"started": 0.0, "finished": None, "new": 0, "gist": None, "prep": None}


def status() -> dict:
    """ソースごとの直近の結果（{source_key: {label, kind, ok, skipped, error, count, at}}）と、最後の実行。"""
    return {"sources": [dict(v, source_key=k) for k, v in _status.items()], "last": dict(_last), "running": _running.locked()}


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"{SOURCE_TIMEOUT_SECONDS} 秒以内に取れませんでした"
    return f"{type(exc).__name__}: {str(exc)[:120]}".rstrip(": ")


async def _fetch_one(source: sources.Source, gate: asyncio.Semaphore) -> tuple[sources.Source, list[dict], dict]:
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with gate:
        started = time.monotonic()
        etag, last_modified = polling.conditional_headers(source.key)
        meta = sources.FetchMeta(etag=etag, last_modified=last_modified)
        sources.FETCH_META.set(meta)      # gather の中の自分のタスクだけに効く。fetch_rss が条件付きの取得に使う

        def ms() -> int:
            return int((time.monotonic() - started) * 1000)
        cid = connectors.for_source(source.key)
        box = None
        try:
            ok, why = connectors.gate(cid)      # connector の入・切・利用条件・1 日の予算（既存の取得もこの枠組みの下）
            if not ok:
                raise sources.SkippedSource(why)
            box = connectors.begin_count()
            items = await asyncio.wait_for(source.fetch(), SOURCE_TIMEOUT_SECONDS)
            connectors.note_usage(cid, items=len(items), nbytes=box[0], requests=1)
            return source, items, {"label": source.label, "kind": source.kind, "ok": True, "skipped": "", "error": "", "count": len(items), "at": at, "ms": ms(),
                                   "status": 304 if meta.not_modified else 200, "not_modified": meta.not_modified, "conditional": meta.conditional,
                                   "etag": meta.new_etag, "last_modified": meta.new_last_modified}
        except sources.SkippedSource as exc:
            return source, [], {"label": source.label, "kind": source.kind, "ok": True, "skipped": str(exc), "error": "", "count": 0, "at": at, "ms": ms(), "status": None}
        except Exception as exc:  # noqa: BLE001 - 1 つのソースの失敗で他を止めない
            if box is not None:      # 失敗しても受け取った量は予算に数える
                connectors.note_usage(cid, nbytes=box[0], requests=1)
            logger.info("ニュースの取得に失敗しました (%s): %s", source.key, exc)
            text = _error_text(exc)
            return source, [], {"label": source.label, "kind": source.kind, "ok": False, "skipped": "", "error": text, "count": 0, "at": at, "ms": ms(),
                                "status": meta.status or fetchlog.status_of(text), "retry_after": meta.retry_after}


def _save(items: list[dict]) -> dict:
    """記事をバッファへ積む（DB へは Write Coordinator が後でまとめて書く。古い記事の削除も、そのまとめ書きの中）。"""
    items, result_dedup = dedup.prepare(items)
    result = store.stage_items(items) if items else {"new": 0, "updated": 0, "changed": [], "new_ids": [], "new_urls": []}
    result["pruned"] = 0
    result["dedup"] = result_dedup
    return result


def _log_results(results: list, new_urls: list[str]) -> None:
    """取得の履歴（news_fetch_log）へ 1 回ぶんを書く。書けなくても取得は成功（ログだけ）。"""
    fresh = set(new_urls)
    rows = []
    for source, got, info in results:
        at = datetime.strptime(info["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        rows.append({"source_key": source.key, "at": at, "ok": info["ok"], "status": info.get("status"), "count": info["count"],
                     "new": sum(1 for i in got if i["url"] in fresh), "ms": info.get("ms", 0), "error": info["error"], "skipped": info["skipped"]})
    try:
        writer.get().stage_fetchlog(rows)
    except Exception:  # noqa: BLE001
        logger.exception("取得の履歴を積めませんでした")


def _record_polling(results: list, new_urls: list[str], cfg: dict) -> None:
    """取得の結果を、取得先ごとの状態（ETag・次の間隔・バックオフ）へ。見送り（Reddit の鍵なしなど）は残さない。"""
    fresh = set(new_urls)
    default = channels.load()["interval_minutes"]
    ivs, fixed = cfg.get("intervals", {}), set(cfg.get("fixed", []))
    for source, got, info in results:
        if info.get("skipped"):
            continue
        base = float(ivs.get(source.key) or source.interval or default)
        try:
            polling.record(source.key, base=base, fixed=source.key in fixed, ok=info["ok"], new=sum(1 for i in got if i["url"] in fresh),
                           not_modified=bool(info.get("not_modified")), conditional=bool(info.get("conditional")), etag=info.get("etag", ""),
                           last_modified=info.get("last_modified", ""), status=info.get("status"), retry_after=info.get("retry_after"))
        except Exception:  # noqa: BLE001 - 状態を残せなくても取得は成功
            logger.exception("取得の状態を残せませんでした")


def _reload(ids: list[int]) -> list[dict]:
    from sqlalchemy import select

    with news_db.session() as db:
        return [store.item_dict(r) for r in db.scalars(select(NewsItem).where(NewsItem.id.in_(ids or [0])))]


async def _after_save(saved: dict) -> dict:
    """取った後の仕事（一言要約・下ごしらえ）。失敗しても取得の結果は変えない（理由だけ返す）。"""
    info: dict = {"gist": {"done": 0, "error": ""}, "prep": {"done": 0, "calls": 0, "error": ""}}
    changed = saved["changed"]
    try:
        info["gist"] = await gist.enrich(changed)
        prepared = await prep.run(changed)
        info["prep"] = prepared
        late = await prep.run_backfill()      # 夜だけ: 取り出しが済んでいない記事の要素を、使い残した枠で
        info["backfill"] = late
        info["events"] = await event_llm.run()      # 記事 3 件以上・出どころ 2 以上のイベントの要約（10 件ずつ・1 回きり）
        info["knowledge"] = await kg_job.run()      # 夜: 文書の対応・「一緒に出る」の集計・知識カードの概要（変化のあった物だけ）
        info["trend"] = await trend.run_due()       # 1 日 1〜2 回の「今日の傾向」
        if prepared["error"] and not info["gist"]["error"]:
            info["gist"]["error"] = prepared["error"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("一言要約・下ごしらえに失敗しました: %s", exc)
        info["gist"]["error"] = _error_text(exc)
    return info


async def refresh(*, force: bool = False, channel_ids: list[str] | None = None, keys: list[str] | None = None) -> dict:
    """全ソースを取り直す。実行中・直前に取ったばかりなら取らない（force で間隔だけ無視）。
    channel_ids を渡すと、そのチャンネルの検索（source_key が search:<id>:）だけを取り直す（チャンネルの語を変えた直後。ほかのソースの状態は触らない）。
    keys を渡すと、その source_key のソースだけ取る（見張りが、間隔の来たものだけ取るとき。ほかのソースの状態は触らない）。
    {"started": bool, "reason": ..., "new": 件数, "failed": 失敗したソース数}。"""
    if rest.blocks_fetch():
        return {"started": False, "reason": "rest"}
    if writer.get().memory_full():
        return {"started": False, "reason": "memory_full"}
    if not _running.acquire(blocking=False):
        return {"started": False, "reason": "running"}
    try:
        if not force and time.monotonic() - _last["started"] < MIN_GAP_SECONDS:
            return {"started": False, "reason": "recent"}
        _last["started"] = time.monotonic()
        cfg = sources.load_config()
        listing = sources.build_sources(cfg, channels.load()["channels"])
        if channel_ids is not None:
            prefixes = tuple(f"search:{cid}:" for cid in channel_ids)
            listing = [s for s in listing if s.key.startswith(prefixes)]
        if keys is not None:
            wanted = set(keys)
            listing = [s for s in listing if s.key in wanted]
        gate = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(*[_fetch_one(s, gate) for s in listing])
        items = [item for _, got, _ in results for item in got]
        arxiv_items = [i for source, got, _ in results if connectors.for_source(source.key) == "arxiv" for i in got]
        if arxiv_items:      # arXiv の RSS から論文の文書（L1・arXiv ID で同定）も作る（書誌だけ。まとめ書きに乗る）
            try:
                from qnapnewshub import conn_impl

                connectors.note_usage("arxiv", items=conn_impl.stage_arxiv_papers(arxiv_items))
            except Exception:  # noqa: BLE001 - 論文の文書を作れなくても記事の取得は成功
                logger.exception("arXiv の論文の文書を作れませんでした")
        saved = await asyncio.to_thread(_save, items)
        resting = rest.blocks_analysis()      # お休み中は、画像・一言要約・下ごしらえをしない（書き込みも通信も増やさない）
        try:     # 画像の無い新着へ、記事のページの og:image を付ける（失敗しても取得は成功）
            saved["images"] = {"tried": 0, "found": 0, "logos": 0} if resting else await images.fill(saved.get("new_ids", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("記事の画像の取得に失敗しました: %s", exc)
        # 設定から外したソースの状態は残さない
        if channel_ids is None and keys is None:
            keep = {s.key for s in listing}
            for key in list(_status):
                if key not in keep:
                    _status.pop(key)
        stamp = time.monotonic()
        for source, _, _ in results:
            _last_fetch[source.key] = stamp
        cfg_now = cfg
        _record_polling(results, saved.get("new_urls", []), cfg_now)
        await asyncio.to_thread(_log_results, results, saved.get("new_urls", []))
        for source, _, info in results:
            # 最後に取れた時刻は、失敗しても前回の値を残す（画面の「最後に取れた」「最後の失敗」）
            info["ok_at"] = info["at"] if info["ok"] and not info["skipped"] else (_status.get(source.key) or {}).get("ok_at", "")
            info["last_error"] = (_status.get(source.key) or {}).get("last_error", "") if info["ok"] else info["error"]
            _status[source.key] = info
        _last.update(finished=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), new=saved["new"])
        metrics.note_fetch(results)
        after = {"gist": {"done": 0, "error": ""}, "prep": {"done": 0, "calls": 0, "error": ""}} if resting else await _after_save(saved)
        _last.update(gist=after["gist"], prep=after["prep"])
        return {"started": True, "reason": "", "new": saved["new"], "updated": saved["updated"], "pruned": saved["pruned"], "after": after,
                "failed": sum(1 for _, _, i in results if not i["ok"])}
    finally:
        _running.release()


TICK_SECONDS = 60


def _effective_interval(source: sources.Source, cfg: dict, default: int) -> float:
    """いまの取得の間隔（分）: 基準（取得先ごとの設定か既定）を、Adaptive Polling とバックオフで伸ばし縮めた値。固定の取得先は基準のまま。"""
    base = float(source.interval or default)
    return polling.effective_minutes(polling.get(source.key), base, source.key in set(cfg.get("fixed", [])))


def _seed_last_fetch(listing: list[sources.Source]) -> None:
    """再起動の直後は、前回の取得の時刻（fetch-state.json）から続ける（起動のたびに全部を取りに行かない）。"""
    wall, mono = time.time(), time.monotonic()
    for s in listing:
        if s.key not in _last_fetch:
            last = polling.get(s.key).get("last_at")
            if last:
                _last_fetch[s.key] = mono - max(0.0, wall - float(last))


def due_keys(listing: list[sources.Source], last_fetch: dict[str, float], now: float, default_minutes: int, limit: int = sources.MAX_PER_RUN) -> list[str]:
    """間隔の来たソースの source_key（最後に取ってからの経過が、そのソースの間隔以上。まだ取っていないものも含む）。
    間隔が来てから長く待たされているものを先に、1 回に limit 件まで。"""
    due = []
    for s in listing:
        every = (s.interval or default_minutes) * 60
        last = last_fetch.get(s.key)
        if last is None or now - last >= every:
            due.append((float("-inf") if last is None else last - every, s.key))
    due.sort()
    return [key for _, key in due[:limit]]


async def news_loop() -> None:
    """起動の少し後から、1 分ごとに見て、取得の間隔（ソースごと。無ければ設定の既定）が来たソースだけ取り（1 回 MAX_PER_RUN 件まで・同時 CONCURRENCY 件）（取得しない時間帯は除く）、日刊・週刊のまとめの時刻が来ていれば作る。
    テストでは動かさない（NEWSHUB_AUTO=0）。"""
    from qnapnewshub import digest

    await asyncio.sleep(FIRST_DELAY_SECONDS)
    while True:
        if os.environ.get("NEWSHUB_AUTO") != "0":
            try:
                conf = settings.load()
                if not settings.in_quiet(conf, datetime.now()) and not rest.blocks_fetch():
                    listing = sources.build_sources(sources.load_config(), channels.load()["channels"])
                    default = channels.load()["interval_minutes"]
                    cfg = sources.load_config()
                    listing = [replace(s, interval=_effective_interval(s, cfg, default)) for s in listing]
                    _seed_last_fetch(listing)
                    due = due_keys(listing, _last_fetch, time.monotonic(), default)
                    if due:
                        result = await refresh(force=True, keys=due)
                        if result.get("started"):
                            logger.info("ニュースを取得しました: %s 件のソース・新着 %s 件・失敗 %s 件", len(due), result["new"], result["failed"])
            except Exception:  # noqa: BLE001 - 1 回の失敗で見張りを止めない
                logger.exception("ニュースの取得に失敗しました")
            try:      # 新しい connector（発見・API・sitemap・変化の検出）。間隔・予算・入切は connectors が見る
                if not settings.in_quiet(settings.load(), datetime.now()) and not rest.blocks_fetch():
                    for cid in await connectors.run_due():
                        logger.info("connector を実行しました: %s", cid)
            except Exception:  # noqa: BLE001
                logger.exception("connector の実行に失敗しました")
            try:
                for row in ([] if rest.blocks_analysis() else await digest.run_due()):
                    logger.info("ニュースのまとめを作りました: %s %s", row.get("period"), row.get("day"))
            except Exception:  # noqa: BLE001
                logger.exception("ニュースのまとめを作れませんでした")
        await asyncio.sleep(TICK_SECONDS)
