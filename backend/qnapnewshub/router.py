"""ニュースの専門ページの API（/api/v1/news）。ログインした利用者。event はその人宛てだけ。設定の変更は settings.manage。"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qnapnewshub import channels as channels_mod
from qnapnewshub import polling, portability, rest, writer, feedgen, fetchlog, probe, sourcemgr, digest, home, llm, llm_models, prefs, prep, presets, profile, service, sources, store
from qnapnewshub.auth import AuthUser as User
from qnapnewshub.auth import current_user as get_current_user
from qnapnewshub.auth import require_admin
from qnapnewshub.db import get_news_db
from qnapnewshub import settings as news_settings
from qnapnewshub.db import NewsItem

router = APIRouter(prefix="/news", tags=["news"])


def _forget(keys: list[str]) -> None:
    from qnapnewshub import db as news_db

    with news_db.session() as session:
        fetchlog.forget(session, keys)


class ActionBody(BaseModel):
    action: str = Field(pattern="^(open|like|dislike|hide|star)$")
    on: bool = True


class MuteBody(BaseModel):
    topic: str = Field(min_length=1, max_length=60)
    muted: bool = True


def _kinds(kind: str) -> list[str] | None:
    wanted = [k for k in (kind or "").split(",") if k]
    bad = [k for k in wanted if k not in sources.KINDS]
    if bad:
        raise HTTPException(status_code=422, detail=f"知らない種類です: {bad[0]}")
    return wanted or None


def _day(value: str) -> str:
    if value:
        try:
            store.day_range(value)
        except ValueError:
            raise HTTPException(status_code=422, detail="date は YYYY-MM-DD の形です") from None
    return value


def _channel(channel_id: str) -> dict | None:
    if not channel_id:
        return None
    found = next((c for c in channels_mod.load()["channels"] if c["id"] == channel_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="そのチャンネルはありません")
    return found


@router.get("/items")
def items(view: str = Query("recommended", pattern="^(recommended|latest|starred)$"), kind: str = "", source: str = "", topic: str = "", channel: str = "",
          q: str = Query("", max_length=200), date: str = "", before: str = "", limit: int = Query(store.PAGE_DEFAULT, ge=1, le=store.PAGE_MAX),
          user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    result = store.list_items(db, user.id, view=view, kinds=_kinds(kind), source=source[:200], topic=topic[:60], q=q, day=_day(date),
                              before=before, limit=limit, channel=_channel(channel))
    # ホームのカードが色の印を付けられるよう、チャンネルの指定が無い一覧にも所属のチャンネルを付ける
    channels = channels_mod.numbered(channels_mod.load()["channels"])
    for item in result["items"]:
        if "channel" not in item:
            item["channel"] = channels_mod.first_channel(channels, SimpleNamespace(kind=item["kind"], source_key=item.get("source_key", ""), title=item["title"], summary=item.get("summary") or ""))
    return result


@router.get("/archive")
def archive(kind: str = "", user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    return store.archive_days(db, user.id, kinds=_kinds(kind))


@router.get("/trends")
def trends(days: int = Query(14, ge=1, le=90), user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    return store.trends(db, user.id, days)


@router.get("/profile")
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    return profile.summary(db, user.id)


@router.put("/profile/mute")
def mute_topic(body: MuteBody, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """「あなたの興味」から語を外す（muted=false で戻す）。"""
    profile.set_muted(user.id, body.topic, body.muted)
    return profile.summary(db, user.id)


@router.post("/items/{item_id}/action")
def item_action(item_id: int, body: ActionBody, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    state = store.set_action_now(user.id, item_id, body.action, body.on)
    if state is None:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    return {"state": state}


@router.post("/refresh")
async def refresh(user: User = Depends(get_current_user)):
    return await service.refresh()


@router.get("/sources")
def get_sources(user: User = Depends(get_current_user)):
    return {"config": sources.load_config(), "kinds": sources.KIND_LABELS, "reddit_key": False,
            "limit": sources.MAX_PER_LIST, **service.status()}


@router.get("/presets")
def get_presets(user: User = Depends(get_current_user)):
    """設定 → ニュース → ソースの「プリセット」。分野ごとの無料のフィード・Release・フォーラム（確かめた日つき）。"""
    return presets.listing()


@router.put("/sources")
def put_sources(body: dict, user: User = Depends(require_admin)):
    try:
        return {"config": sources.save_config(body)}
    except sources.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class DiscoverBody(BaseModel):
    url: str = Field(min_length=8, max_length=800)


class CheckBody(BaseModel):
    kind: str = Field(pattern="^(rss|page|github|codeberg|discourse|reddit|hackernews|huggingface)$")
    value: str = Field(default="", max_length=800)
    name: str = Field(default="", max_length=60)


@router.post("/sources/discover")
async def discover_site(body: DiscoverBody, user: User = Depends(require_admin)):
    """「サイトを追加」: URL が RSS ならそのまま、HTML なら RSS の候補（link rel・よくある場所）と、記事の一覧の巡回の可否を返す。
    外への取得は既存の安全な取得（内部アドレスの遮断・大きさと時間の上限）。保存はしない。"""
    return await probe.discover(body.url.strip())


@router.post("/sources/check")
async def check_source(body: CheckBody, user: User = Depends(require_admin)):
    """通信確認: そのソースを今この場で 1 回取り、件数・最新の見出し 3 件・かかった時間・HTTP の状態・失敗の理由を返す。保存はしない。"""
    return await probe.check(body.kind, body.value.strip(), body.name.strip())


class GenerateBody(BaseModel):
    type: str = Field(pattern="^(keyword|github|steam|arxiv)$")
    keyword: str = Field(default="", max_length=200)
    lang: str = Field(default="ja", max_length=4)
    target: str = Field(default="", max_length=300)
    what: str = Field(default="releases", max_length=12)
    branch: str = Field(default="main", max_length=100)
    cats: list[str] = Field(default_factory=list, max_length=20)


@router.post("/sources/generate")
def generate_feed(body: GenerateBody, user: User = Depends(require_admin)):
    """「フィードを作る」: キーワード・GitHub・Steam・arXiv の入力から取得先の URL を組み立てる（通信はしない。確認は /sources/check）。"""
    try:
        return feedgen.generate(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _iso_epoch(value: float | None) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def _manage_rows(db: Session) -> list[dict]:
    cfg = sources.load_config()
    stats, bars, totals = fetchlog.summary(db), fetchlog.week_bars(db), fetchlog.article_totals(db)
    mem = {s["source_key"]: s for s in service.status()["sources"]}
    default_min = channels_mod.load()["interval_minutes"]
    rows = []
    for e in sourcemgr.entries(cfg):
        st = stats.get(e["key"]) or {}
        live = mem.get(e["key"]) or {}
        snap = polling.snapshot(e["key"], float(e["interval"] or default_min), e["fixed"])
        rows.append({**e, "poll": {**snap, "next_at": _iso_epoch(snap["next_at"]) if e["enabled"] else ""}, "last_at": st.get("last_at", "") or live.get("at", ""), "last_ok_at": st.get("last_ok_at", "") or live.get("ok_at", ""),
                     "last_ok": st.get("last_ok"), "last_error": st.get("last_error", ""), "last_count": st.get("last_count", 0), "last_status": st.get("last_status"),
                     "last_skipped": st.get("last_skipped", ""), "week": bars.get(e["key"], [None] * 7), "total": totals.get(e["key"], 0)})
    return rows


@router.get("/sources/manage")
def manage_list(user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """「取得先の管理」の一覧: 設定にある全ソース（止めているものも）と、取得の実績（最終取得・最終成功・直近の結果・7 日の成功率・記事の累計）。"""
    cfg = sources.load_config()
    return {"items": _manage_rows(db), "groups": presets.listing()["groups"], "default_interval": channels_mod.load()["interval_minutes"],
            "reddit_key": False, "limit": sources.MAX_PER_LIST, "interval_range": [sources.INTERVAL_MIN, sources.INTERVAL_MAX],
            "formats": sourcemgr.FORMAT_LABELS, "origins": sources.ORIGIN_LABELS, "running": service.status()["running"], "enabled": cfg["enabled"]}


@router.get("/sources/manage/detail")
def manage_detail(key: str = Query(min_length=1, max_length=600), limit: int = Query(default=fetchlog.HISTORY_DEFAULT, ge=1, le=fetchlog.KEEP),
                  user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """1 件の詳細: 取得の履歴（直近 limit 回）と、そのソースから入った最近の記事。"""
    row = next((r for r in _manage_rows(db) if r["key"] == key), None)
    if row is None:
        raise HTTPException(status_code=404, detail="そのソースはありません")
    return {"item": row, "history": fetchlog.history(db, key, limit), "articles": fetchlog.recent_articles(db, key)}


class BulkBody(BaseModel):
    action: str = Field(pattern="^(enable|disable|delete|interval|check|fixed|auto|origin)$")
    keys: list[str] = Field(min_length=1, max_length=300)
    interval: int | None = Field(default=None, ge=0, le=sources.INTERVAL_MAX)
    origin: str | None = Field(default=None, max_length=20)


@router.post("/sources/manage/bulk")
async def manage_bulk(body: BulkBody, user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    """選んだソースへの一括の操作。enable・disable・delete・interval（0 は既定に戻す）は設定へ保存、check は通信確認（保存しない・同時 4・40 件まで）。"""
    cfg = sources.load_config()
    if body.action == "check":
        by_key = {e["key"]: e for e in sourcemgr.entries(cfg)}
        wanted = [by_key[k] for k in body.keys if k in by_key][:40]
        gate = asyncio.Semaphore(4)

        async def one(e: dict) -> dict:
            async with gate:
                return {"key": e["key"], **await probe.check(e["kind"], e["value"], e["name"])}
        return {"results": await asyncio.gather(*[one(e) for e in wanted])}
    try:
        sourcemgr.apply(cfg, body.action, body.keys, body.interval, body.origin)
        saved = sources.save_config(cfg)
    except sources.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.action == "delete":
        writer.run("fetchlog.forget", lambda: _forget(body.keys))
        polling.forget(body.keys)
    return {"count": len(body.keys), "items": _manage_rows(db), "enabled": saved["enabled"]}


class AddItem(BaseModel):
    kind: str = Field(pattern="^(rss|page|github|codeberg|discourse|reddit|hackernews|huggingface)$")
    value: str = Field(default="", max_length=800)
    name: str = Field(default="", max_length=60)
    interval: int | None = Field(default=None, ge=0, le=sources.INTERVAL_MAX)
    origin: str | None = Field(default=None, max_length=20)
    category: str = Field(default="", max_length=40)
    enabled: bool = True


class AddBody(BaseModel):
    items: list[AddItem] = Field(min_length=1, max_length=400)


@router.post("/sources/manage/add")
def manage_add(body: AddBody, user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    """取得先を足す（目録からのまとめ・フィードを作る手伝い・サイトを追加）。すでにあるものは足さず、間隔だけ付ける。"""
    cfg = sources.load_config()
    try:
        added = sourcemgr.add(cfg, [i.model_dump() for i in body.items])
        sources.save_config(cfg)
    except sources.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"added": added, "items": _manage_rows(db)}


class ImportPreviewBody(BaseModel):
    data: str = Field(min_length=1, max_length=3_000_000, description="ファイルの中身（base64）")
    format: str = Field(default="auto", pattern="^(auto|csv|opml)$")


@router.post("/sources/import/preview")
def import_preview(body: ImportPreviewBody, user: User = Depends(require_admin)):
    """CSV・OPML の取り込みの「確認の表」。何も保存しない・通信しない。行ごとに new・duplicate・invalid と理由。"""
    try:
        return portability.preview(sources.load_config(), portability.decode_b64(body.data), body.format)
    except portability.ImportError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ImportRow(BaseModel):
    url: str = Field(max_length=800)
    name: str = Field(default="", max_length=60)
    category: str = Field(default="", max_length=40)
    origin: str = Field(default="", max_length=20)
    kind: str = Field(default="rss", pattern="^(rss|page)$")
    interval: int = Field(default=0, ge=0, le=sources.INTERVAL_MAX)
    enabled: bool = True


class ImportCommitBody(BaseModel):
    rows: list[ImportRow] = Field(min_length=1, max_length=100)
    mode: str = Field(pattern="^(check|direct)$")


@router.post("/sources/import/commit")
async def import_commit(body: ImportCommitBody, user: User = Depends(require_admin), db: Session = Depends(get_news_db)):
    """確認の表で選んだ行を足す。mode=check は 1 件ずつ通信確認し、取れたものだけ有効で足す（取れなかったものは足さず理由を返す）。
    mode=direct は通信せず、すべて無効で足す（あとで有効にする）。重複（同じ URL）は足さず数える。サーバーで改めて検査する（表の値を信じない）。"""
    cfg = sources.load_config()
    pre = portability.build_preview(cfg, [{"line": i + 1, "name": r.name, "url": r.url, "category": r.category, "origin": r.origin, "type": r.kind,
                                           "interval": r.interval, "enabled": r.enabled} for i, r in enumerate(body.rows)])
    todo = [r for r in pre["rows"] if r["status"] == "new"]
    room = max(0, sources.MAX_PER_LIST - len(cfg["rss"]) - len(cfg.get("pages", [])))
    over = todo[room:]
    todo = todo[:room]
    failed: list[dict] = [{"url": r["url"], "name": r["name"], "reason": f"取得先の上限（{sources.MAX_PER_LIST} 件）を超えます"} for r in over]
    if body.mode == "check":
        gate = asyncio.Semaphore(4)

        async def one(r: dict) -> tuple[dict, dict]:
            async with gate:
                return r, await probe.check(r["kind"], r["url"], r["name"])
        good = []
        for r, res in await asyncio.gather(*[one(r) for r in todo]):
            if res.get("ok"):
                good.append(r)
            else:
                failed.append({"url": r["url"], "name": r["name"], "reason": (res.get("error") or {}).get("message") or "取れませんでした"})
        todo, enabled_all = good, None
    else:
        enabled_all = False
    items = [{"kind": r["kind"], "value": r["url"], "name": r["name"], "interval": r["interval"] or None, "origin": r["origin"] or None,
              "category": r["category"], "enabled": r["enabled"] if enabled_all is None else enabled_all} for r in todo]
    added = 0
    if items:
        try:
            added = sourcemgr.add(cfg, items)
            sources.save_config(cfg)
        except sources.ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"added": added, "duplicate": pre["counts"]["duplicate"], "invalid": pre["counts"]["invalid"], "failed": failed, "mode": body.mode, "items": _manage_rows(db)}


@router.get("/sources/export")
def export_sources(format: str = Query(default="opml", pattern="^(opml|csv)$"), user: User = Depends(get_current_user)):
    """取得先の書き出し（OPML・CSV）。無効のものも含む。書き出したファイルは、そのまま取り込める。"""
    cfg = sources.load_config()
    if format == "csv":
        return Response(portability.export_csv(cfg), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="newshub-sources.csv"'})
    return Response(portability.export_opml(cfg), media_type="text/x-opml; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="newshub-sources.opml"'})


@router.get("/home")
def get_home(user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """放送局の画面が 1 回で読み込む内容（ON AIR・テロップ・NOW/TODAY/THIS WEEK・チャンネルごと・興味・数字）。"""
    return home.build(db, user.id)


@router.get("/channels")
def get_channels(user: User = Depends(get_current_user)):
    return {**channels_mod.load(), "kinds": sources.KIND_LABELS, "reddit_key": False,
            "limits": {"channels": channels_mod.MAX_CHANNELS, "terms": channels_mod.MAX_TERMS, "palette": list(channels_mod.PALETTE)}}


_background: set[asyncio.Task] = set()


def changed_search_channels(old: dict[str, dict], new: list[dict]) -> list[str]:
    """検索をやり直すチャンネルの id: 検索が有効で、新しい・含める語・言語・検索の有無が変わったもの。"""
    out = []
    for c in new:
        if not c["search"]:
            continue
        before = old.get(c["id"])
        if before is None or any(c[k] != before[k] for k in ("include", "lang", "search")):
            out.append(c["id"])
    return out


@router.put("/channels")
async def put_channels(body: dict, user: User = Depends(require_admin)):
    """チャンネルを保存する。語・言語・検索の有無が変わった（新しい）チャンネルは、その検索を裏でやり直す
    （古い語で取った記事は、所属の判定が新しい語で外すので、新しい語の記事で埋まる）。"""
    old = {c["id"]: c for c in channels_mod.load()["channels"]}
    try:
        saved = await asyncio.to_thread(channels_mod.save, body)
    except sources.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    changed = changed_search_channels(old, saved["channels"])
    if changed and os.environ.get("NEWSHUB_AUTO") != "0":      # 試験では外へ通信しない
        task = asyncio.create_task(service.refresh(force=True, channel_ids=changed))
        _background.add(task)
        task.add_done_callback(_background.discard)
    return saved


@router.get("/prefs")
def get_prefs(user: User = Depends(get_current_user)):
    return prefs.load(user.id)


@router.put("/prefs")
def put_prefs(body: dict, user: User = Depends(get_current_user)):
    """表示のデザインと好み。利用者ごと（その人自身の設定なので settings.manage は要らない）。"""
    try:
        saved = prefs.save(user.id, body)
    except sources.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return saved


@router.get("/export")
def export(format: str = Query("json", pattern="^(json|csv)$"), channel: str = "", user: User = Depends(get_current_user),
           db: Session = Depends(get_news_db)):
    """アーカイブの書き出し（出来事は除く。チャンネルを指定すればその分だけ）。"""
    rows = home.export_rows(db, user.id, _channel(channel))
    if format == "csv":
        return Response(home.to_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="news-archive.csv"'})
    return Response(home.to_json(rows), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="news-archive.json"'})


# ---- 更新・LLM の設定 / OpenRouter の鍵 / まとめ ----

@router.get("/settings")
def get_settings(user: User = Depends(get_current_user)):
    """更新のタイミング・LLM の設定と、今日使った回数・失敗（設定の画面に出す）。鍵の本文は返さない（入っているかだけ）。"""
    settings = news_settings.load()
    cfg = settings["llm"]
    return {"settings": settings, "weekdays": list(news_settings.WEEKDAYS), "interval_minutes": channels_mod.load()["interval_minutes"],
            "prep_calls_today": prep.calls_today(), "llm_stats": llm.stats(cfg["daily_limit"], cfg["minute_limit"]), "prep_batch": prep.BATCH,
            "rest_state": rest.state(), "rest_modes": rest.MODE_LABELS,
            "gateway_key_set": llm.gateway_key_is_set(), "quota_labels": llm.QUOTA_LABELS, "model_chain": llm_models.status()}


@router.put("/settings")
async def put_settings(body: dict, user: User = Depends(require_admin)):
    try:
        return {"settings": await asyncio.to_thread(news_settings.save, body)}
    except sources.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class KeyBody(BaseModel):
    key: str = Field(min_length=1, max_length=400, repr=False)


@router.get("/openrouter-key")
def get_openrouter_key(user: User = Depends(require_admin)):
    """OpenRouter の鍵が入っているか（本文は返さない）。"""
    return {"set": llm.key_is_set()}


@router.put("/openrouter-key")
async def put_openrouter_key(body: KeyBody, user: User = Depends(require_admin)):
    """鍵を入れる（暗号化して置き場へ。応答・ログに鍵は出さない）。"""
    try:
        await asyncio.to_thread(llm.set_key, body.key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"set": True}


@router.delete("/openrouter-key")
async def delete_openrouter_key(user: User = Depends(require_admin)):
    await asyncio.to_thread(llm.delete_key)
    return {"set": False}


class GatewayKeyBody(BaseModel):
    key: str = Field(min_length=1, max_length=400, repr=False)


@router.get("/gateway-key")
def get_gateway_key(user: User = Depends(require_admin)):
    """ゲートウェイの鍵が入っているか（本文は返さない）。"""
    return {"set": llm.gateway_key_is_set()}


@router.put("/gateway-key")
async def put_gateway_key(body: GatewayKeyBody, user: User = Depends(require_admin)):
    """ゲートウェイの鍵を入れる（OpenRouter の鍵と同じく暗号化して置き場へ。応答・ログに鍵は出さない）。"""
    try:
        await asyncio.to_thread(llm.set_gateway_key, body.key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"set": True}


@router.delete("/gateway-key")
async def delete_gateway_key(user: User = Depends(require_admin)):
    await asyncio.to_thread(llm.delete_gateway_key)
    return {"set": False}


class GatewayTestBody(BaseModel):
    base_url: str = Field(default="", max_length=300)


@router.post("/gateway/test")
async def test_gateway(body: GatewayTestBody, user: User = Depends(require_admin)):
    """「接続を試す」: {base}/models を読む（入力中の URL か、保存済みの URL）。{"ok", "models": [名前], "ms", "error"}。鍵は応答にも文にも出さない。管理者だけ。"""
    import json as _json
    import time as _time

    from qnapnewshub import secretbox

    try:
        base = news_settings.gateway_url(body.base_url) or news_settings.load()["llm"]["gateway"]["base_url"]
    except sources.ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not base:
        raise HTTPException(status_code=422, detail="URL を入れてください")
    key = await asyncio.to_thread(secretbox.get_secret, llm.GATEWAY_KEY_NAME) or ""
    started = _time.monotonic()
    try:
        status, text = await llm.get_gateway_models(base, key)
    except llm.LlmUnavailable as exc:
        return {"ok": False, "models": [], "ms": int((_time.monotonic() - started) * 1000), "error": str(exc)}
    ms = int((_time.monotonic() - started) * 1000)
    if status >= 400:
        return {"ok": False, "models": [], "ms": ms, "error": llm._error_message(status, text).replace(key, "[鍵]") if key else llm._error_message(status, text)}  # noqa: SLF001
    try:
        data = _json.loads(text)
        rows = data["data"] if isinstance(data, dict) else data
        names = sorted({str(r["id"])[:200] for r in rows if isinstance(r, dict) and r.get("id")})[:300]
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "models": [], "ms": ms, "error": "モデルの一覧の形が違います（OpenAI 互換の /models ではありません）"}
    return {"ok": True, "models": names, "ms": ms, "error": ""}


@router.get("/digests")
def get_digests(user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    """/news の上のカード（日刊・週刊の最新）と、アーカイブ用の一覧。"""
    return {"latest": digest.latest(db), "digests": digest.list_digests(db)}


@router.get("/digests/{digest_id}")
def get_digest(digest_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_news_db)):
    found = digest.get_digest(db, digest_id)
    if found is None:
        raise HTTPException(status_code=404, detail="まとめが見つかりません")
    return found


class DigestBody(BaseModel):
    period: str = Field(pattern="^(daily|weekly)$")


@router.post("/digests/run")
async def run_digest(body: DigestBody, user: User = Depends(require_admin)):
    """「今すぐまとめを作る」。今日（週刊は直近の予定の日）の分を作り直す。"""
    from datetime import datetime

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    if body.period == "weekly":      # 直近の予定の日（今日が曜日なら今日）
        weekly = {**news_settings.load()["digest_weekly"], "enabled": True, "time": "00:00"}
        day = next(d for p, d in news_settings.due_digests({**news_settings.DEFAULTS, "digest_daily": {"enabled": False, "time": "00:00"}, "digest_weekly": weekly}, now) if p == "weekly")
    return await digest.generate(body.period, day)
