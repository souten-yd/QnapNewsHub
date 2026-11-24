"""診断（ARCHITECTURE 9.5）。GET /api/v1/diagnostics・自己点検・診断の束・ホームの一行の知らせの元。

- 管理者だけ（router 側）。鍵・トークン・パスワード・URL の秘密の部分（問い合わせ・利用者情報）は出さない（redact_url・logbuf.redact）。
- 数値はメモリ（metrics）から。DB へは読みに行くだけで、診断が書き込みを起こさない。
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select, text

from qnapnewshub import channels, fetchlog, llm, logbuf, metrics, polling, prep, rest, service, settings, sourcemgr, sources, store, updater, writer
from qnapnewshub import db as news_db
from qnapnewshub.config import data_dir
from qnapnewshub.db import NewsFetchLog, NewsItem

logger = logging.getLogger("qnapnewshub.diagnostics")

FAIL_STREAK = 3
STALE_DAYS = 7
MIN_FREE_BYTES = 1 << 30
RSS_TARGET = 512 * 1024 * 1024
_UTC = timezone.utc
_REASONS = (("robots", re.compile(r"robots", re.I)), ("timeout", re.compile(r"時間|timeout|timed out", re.I)), ("http", re.compile(r"HTTP|40\d|50\d|429", re.I)),
            ("xml", re.compile(r"xml|parse|解析|形", re.I)))
_notice_cache: dict = {"at": 0.0, "value": None}


def redact_url(value: str) -> str:
    """URL から、利用者情報・問い合わせ・断片を落とす（秘密が URL に入っていることがある）。"""
    try:
        p = urlsplit(value)
    except ValueError:
        return "（読めない URL）"
    if not p.scheme or not p.hostname:
        return value[:80]
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://{p.hostname}{port}{p.path}" + ("?…" if p.query else "")


def redact_key(key: str) -> str:
    head, _, rest_ = key.partition(":")
    return f"{head}:{redact_url(rest_)}" if rest_.startswith("http") else key[:120]


def reason_of(error: str) -> str:
    for name, rx in _REASONS:
        if rx.search(error or ""):
            return name
    return "other" if error else ""


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return (value if value.tzinfo else value.replace(tzinfo=_UTC)).astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---- 区分 ----

def fetch_section(db) -> dict:
    cfg = sources.load_config()
    stats = fetchlog.summary(db)
    default_min = channels.load()["interval_minutes"]
    mem = {s["source_key"]: s for s in service.status()["sources"]}
    since = datetime.now(_UTC) - timedelta(days=STALE_DAYS)
    since_n = since.replace(tzinfo=None)
    news = {k: int(n or 0) for k, n in db.execute(select(NewsFetchLog.source_key, func.sum(NewsFetchLog.new)).where(NewsFetchLog.at >= since_n).group_by(NewsFetchLog.source_key))}
    runs = {k: int(n or 0) for k, n in db.execute(select(NewsFetchLog.source_key, func.count(NewsFetchLog.id)).where(NewsFetchLog.at >= since_n).group_by(NewsFetchLog.source_key))}
    streaks = _streaks(db)
    rows, stalled, hits, conds = [], 0, 0, 0
    for e in sourcemgr.entries(cfg):
        if not e["enabled"]:
            continue
        st, live = stats.get(e["key"]) or {}, mem.get(e["key"]) or {}
        snap = polling.snapshot(e["key"], float(e["interval"] or default_min), e["fixed"])
        every = snap["interval"] * 60      # いまの間隔（自動で伸びていれば、その値。伸びた分を「止まった」と見なさない）
        hits += snap["hits"]
        conds += snap["conditional"]
        last_ok = st.get("last_ok_at") or live.get("ok_at") or ""
        error = st.get("last_error") or live.get("error") or ""
        problems = []
        streak = streaks.get(e["key"], 0)
        if streak >= FAIL_STREAK:
            problems.append(f"失敗が {streak} 回続いています")
        if runs.get(e["key"], 0) >= 3 and news.get(e["key"], 0) == 0 and not streak:
            problems.append(f"{STALE_DAYS} 日新着がありません")
        if last_ok:
            try:
                age = (datetime.now(_UTC) - datetime.strptime(last_ok, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC)).total_seconds()
                if age > max(every * 6, 6 * 3600):
                    problems.append("最後に成功してからだいぶ経っています")
            except ValueError:
                pass
        stalled += bool(problems)
        rows.append({"key": redact_key(e["key"]), "name": e["name"], "kind": e["kind"], "ok": st.get("last_ok", live.get("ok")), "reason": reason_of(error),
                     "error": logbuf.redact(error)[:160], "last_ok_at": last_ok, "last_at": st.get("last_at") or live.get("at", ""), "interval_min": snap["interval"], "interval_auto": snap["auto"], "backoff": snap["backoff"], "hit_rate": snap["hit_rate"], "hits_304": snap["hits"],
                     "conditional": snap["conditional"],
                     "new_7d": news.get(e["key"], 0), "fail_streak": streak, "stalled": bool(problems), "problems": problems,
                     "fix": "取得先の管理で URL を確かめるか、無効にしてください" if problems else ""})
    rows.sort(key=lambda r: (not r["stalled"], r["name"]))
    return {"total": len(rows), "stalled": stalled, "hit_rate": round(hits * 100 / conds) if conds else None, "hits_304": hits, "conditional": conds,
            "backoff": sum(1 for r in rows if r["backoff"]), "sources": rows[:300]}


def _streaks(db) -> dict[str, int]:
    """ソースごとの、いまの失敗の連続回数（最新から数える）。"""
    log = NewsFetchLog
    out: dict[str, int] = {}
    seen: set[str] = set()
    for key, ok in db.execute(select(log.source_key, log.ok).order_by(log.id.desc()).limit(20000)):
        if key in seen:
            continue
        if ok:
            seen.add(key)
        else:
            out[key] = out.get(key, 0) + 1
    return out


def llm_section(db) -> dict:
    cfg = settings.load()["llm"]
    st = llm.stats(cfg["daily_limit"], cfg["minute_limit"])
    models = []
    for name, row in sorted(llm.MODEL_STATS.items()):
        total = row["ok"] + row["fail"]
        models.append({"model": name, "ok": row["ok"], "fail": row["fail"], "success_pct": round(row["ok"] * 100 / total) if total else None,
                       "rate_limited": row["rate_limited"], "avg_ms": int(row["ms_total"] / total) if total else 0})
    since = (datetime.now(_UTC) - timedelta(days=prep.RETRY_WINDOW_DAYS)).replace(tzinfo=None)
    waiting = db.scalar(select(func.count(NewsItem.id)).where(NewsItem.prep_tries < prep.MAX_TRIES, NewsItem.kind != "event", NewsItem.published_at >= since)) or 0
    waiting += sum(1 for p in writer.get().pending_items() if (p.prep_tries or 0) < prep.MAX_TRIES and p.kind != "event")
    return {"day": st["day"], "calls": st["calls"], "total": st["total"], "daily_limit": st["daily_limit"], "minute_limit": st["minute_limit"], "failed": st["failed"],
            "last_error": logbuf.redact(st["last_error"]), "key_set": st["key_set"], "models": models, "rate_limited": sum(m["rate_limited"] for m in models),
            "unprocessed": int(waiting), "prep_enabled": cfg["prep_enabled"], "prep_model": cfg["prep_model"]}


def storage_section() -> dict:
    coord = writer.get()
    io = metrics.read_io()
    cfg = coord.config()
    nxt = None
    if coord._since is not None:  # noqa: SLF001
        left = max(0, coord._since + cfg["flush_seconds"] - time.monotonic())  # noqa: SLF001
        nxt = (datetime.now(_UTC) + timedelta(seconds=left)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = news_db.path()
    free = shutil.disk_usage(data_dir())
    st = rest.state()
    app_rest = [w for w in coord.rest_writes if not w["user"]]
    return {"io": {k: io.get(k, 0) for k in ("rchar", "wchar", "read_bytes", "write_bytes", "syscw")}, "series": metrics.series(),
            "last_flush": {**coord.last_flush_info} if coord.last_flush_info else None, "last_flush_at": _iso(coord.last_flush_at), "next_flush_at": nxt,
            "flush_minutes": cfg["flush_seconds"] // 60, "limits": {"items": cfg["max_items"], "mb": cfg["max_bytes"] // (1024 * 1024)},
            "unsaved": coord.counts(), "flush_failures": coord.failures, "last_error": logbuf.redact(coord.last_error),
            "files": {"db": _size(path), "wal": _size(Path(str(path) + "-wal")), "auth_db": _size(data_dir() / "auth.sqlite3"),
                      "auth_wal": _size(data_dir() / "auth.sqlite3-wal")},
            "disk": {"free": free.free, "total": free.total}, "rest": st, "rest_writes": list(coord.rest_writes)[-30:],
            "rest_app_writes": len(app_rest), "writes_by_name": dict(coord.writes.most_common(20)), "flush_history": list(coord.flush_history)[-10:],
            "daily": metrics.daily_history()}


def problems(section: dict) -> list[dict]:
    """自動の判定（level: error / warn）。ホームの一行の知らせもここから。"""
    out: list[dict] = []
    f, l, s, m = section["fetch"], section["llm"], section["storage"], section["machine"]
    if f["stalled"]:
        out.append({"area": "fetch", "level": "warn", "text": f"取得が止まった可能性のある取得先が {f['stalled']} 件あります"})
    if l["prep_enabled"] and not l["key_set"] and l["unprocessed"]:
        out.append({"area": "llm", "level": "warn", "text": "OpenRouter の鍵が未設定のため、下ごしらえが進みません"})
    if l["total"] >= l["daily_limit"]:
        out.append({"area": "llm", "level": "warn", "text": "LLM の 1 日の回数の上限に達しました"})
    for mod in l["models"]:
        if mod["success_pct"] is not None and mod["ok"] + mod["fail"] >= 5 and mod["success_pct"] < 50:
            out.append({"area": "llm", "level": "warn", "text": f"{mod['model']} の成功率が {mod['success_pct']}% です。別のモデルへの切り替えを検討してください"})
    if s["rest_app_writes"]:
        out.append({"area": "storage", "level": "error", "text": f"お休み中にアプリが原因の書き込みがありました（{s['rest_app_writes']} 回）"})
    if s["disk"]["free"] < MIN_FREE_BYTES:
        out.append({"area": "storage", "level": "error", "text": "ディスクの空きが 1GB を切りました"})
    if s["flush_failures"]:
        out.append({"area": "storage", "level": "error", "text": "まとめ書きに失敗しています（メモリに残しています）"})
    up = section.get("update") or {}
    if up.get("last") and not up["last"]["ok"]:
        out.append({"area": "update", "level": "warn", "text": up["last"]["message"]})
    if m["rss"] > RSS_TARGET:
        out.append({"area": "machine", "level": "warn", "text": "常駐のメモリが目標の 512MB を超えています"})
    return out


def build(db) -> dict:
    section = {"generated_at": _iso(datetime.now(_UTC)), "fetch": fetch_section(db), "llm": llm_section(db), "storage": storage_section(), "machine": metrics.machine(), "update": updater.summary()}
    section["problems"] = problems(section)
    return section


def notice(db) -> dict:
    """ホームの上に出す一行（問題のあるときだけ）。60 秒覚える（ホームを開くたびに調べない）。"""
    now = time.monotonic()
    if _notice_cache["value"] is not None and now - _notice_cache["at"] < 60 and _notice_cache.get("key") == str(data_dir()):
        return _notice_cache["value"]
    items = build(db)["problems"]
    items.sort(key=lambda p: p["level"] != "error")
    value = {"message": items[0]["text"] if items else None, "level": items[0]["level"] if items else None, "count": len(items)}
    _notice_cache.update(at=now, value=value, key=str(data_dir()))
    return value


# ---- 自己点検 ----

def selfcheck() -> dict:
    """ボタンで: DB の整合・FTS の整合・LLM の鍵・時計。外へは通信しない。読むだけ（書き込みを起こさない）。"""
    checks = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix})

    path = news_db.path()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            res = con.execute("PRAGMA quick_check").fetchall()
            add("DB の整合（PRAGMA quick_check）", res == [("ok",)], "ok" if res == [("ok",)] else "; ".join(str(r[0]) for r in res[:3]),
                "バックアップから戻すか、記事を書き出して作り直してください")
        finally:
            con.close()
    except sqlite3.Error as exc:
        add("DB の整合（PRAGMA quick_check）", False, f"開けません: {type(exc).__name__}", "データの置き場とディスクを確かめてください")
    try:
        with news_db.engine().connect() as conn:
            if conn.execute(text("SELECT 1 FROM sqlite_master WHERE name='news_items_fts'")).first() is None:
                add("全文検索（FTS）の整合", True, "FTS5 は使っていません（LIKE で検索します）")
            else:
                n_items = conn.execute(text("SELECT count(*) FROM news_items")).scalar() or 0
                n_fts = conn.execute(text("SELECT count(*) FROM news_items_fts_docsize")).scalar() or 0
                add("全文検索（FTS）の整合", n_items == n_fts, f"記事 {n_items} 件・索引 {n_fts} 件", "索引の作り直しが要ります（設定の保存と I/O から）")
    except Exception as exc:  # noqa: BLE001
        add("全文検索（FTS）の整合", False, f"確かめられません: {type(exc).__name__}", "DB の整合の結果を先に直してください")
    add("LLM の鍵", llm.key_is_set(), "入っています" if llm.key_is_set() else "未設定です", "設定の AI の欄で OpenRouter の鍵を入れてください")
    newest = None
    try:
        with news_db.session() as db:
            newest = db.scalar(select(func.max(NewsItem.fetched_at)))
    except Exception:  # noqa: BLE001
        pass
    now = datetime.now(_UTC)
    year_ok = now.year >= 2024
    future = False
    if newest is not None:
        future = (newest if newest.tzinfo else newest.replace(tzinfo=_UTC)) > now + timedelta(minutes=5)
    drift = abs((time.time() - metrics.STARTED) - (time.monotonic() - _MONO0))
    add("時計", year_ok and not future and drift < 120,
        f"{now.strftime('%Y-%m-%d %H:%M')} UTC・起動後のずれ {drift:.1f} 秒" + ("・取得の記録が未来の時刻" if future else ""),
        "本体の時計（NTP）を合わせてください")
    return {"at": _iso(now), "ok": all(c["ok"] for c in checks), "checks": checks}


_MONO0 = time.monotonic()


def bundle(db) -> dict:
    """診断の束（不具合の相談用）。秘密を除いた JSON。"""
    from qnapnewshub import __version__

    data = build(db)
    data["bundle"] = {"version": __version__, "settings": _safe_settings(), "logs": [{**r, "t": datetime.fromtimestamp(r["t"], _UTC).strftime("%H:%M:%S")} for r in logbuf.recent(200)],
                      "selfcheck": selfcheck()}
    return data


def _safe_settings() -> dict:
    s = settings.load()
    s["llm"] = {k: v for k, v in s["llm"].items() if k != "digest_focus"}      # 利用者が書いた自由記述は出さない
    return s
