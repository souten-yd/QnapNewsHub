"""Write Coordinator: SQLite・設定ファイルへの永続化を、1 つの係（単一の書き手のスレッド）に通す。ARCHITECTURE 9 章。

- 「大量で取り直せる」書き込み（記事・取得の履歴・下ごしらえ/画像の結果・LLM の回数・API トークンの最終使用）はメモリのバッファに積み、
  既定 30 分ごと（または 1,000 件 / 128MB を超えたとき）に 1 トランザクションでまとめて書く。
- 「利用者の操作」（設定・認証・トークン・好み・メモ・記事の操作）は run() で即時に書く（ただし必ずこの係のスレッドで）。
- WAL の自動 checkpoint は切（wal_autocheckpoint=0）。まとめ書きの直後にだけ checkpoint（PASSIVE→WAL が大きければ TRUNCATE）。
- 未保存のデータを保存済みとして扱わない: 読みはバッファの分も合わせて見せてよいが、effective()/pending_items() の行には _unsaved の印が付き、
  API は persisted:false を返す。ControlDeck への差分は DB にある分だけ（store.changes_persisted）。
- 停止（SIGTERM→lifespan）で shutdown() がバッファを書いてから終わる。
- お休みモード（rest.py）中は、動きに応じて保存を止める。休止中に書いた処理名は rest_writes に残る（診断）。
- バッファ → inflight（保存中）→ DB の順。保存に失敗したら inflight をバッファへ戻し、次の機会に再び書く（消さない）。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import queue
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path

from qnapnewshub.config import data_dir

logger = logging.getLogger("qnapnewshub.writer")

PREP_DONE = 100
TOPICS_MAX = 12
HARD_FACTOR_ITEMS = 5          # 休止中のメモリ保持: 件数の上限の何倍で集めるのを止めるか
HARD_FACTOR_BYTES = 2
WAL_TRUNCATE_BYTES = 32 * 1024 * 1024
DB_WRITE_PREFIXES = ("news.", "auth.", "digest.", "fetchlog.")      # 即時の書き込みのうち、DB（WAL）を動かす処理名
_STOP = object()
_UTC = timezone.utc


class _Buf:
    """まとめ書きを待っているもの。"""

    def __init__(self) -> None:
        self.new: dict[int, object] = {}               # id -> NewsItem（DB に入れていない新しい記事。transient）
        self.updates: dict[str, dict] = {}             # url -> 列の更新（すでに DB にある記事）
        self.update_ids: dict[str, int] = {}
        self.patches: dict[int, dict] = {}             # id -> 部分の更新（下ごしらえ・画像・一言要約）
        self.fetchlog: list[dict] = []
        self.states: dict[str, Callable[[], None]] = {}  # 名前 -> 書く処理（LLM の回数など。後の指定が勝つ）
        self.token_used: dict[int, str] = {}
        self.extractions: dict[int, dict] = {}         # 記事の id -> 要素の取り出し（物・fact。entities.apply_extractions が書く）
        self.event_summaries: dict[int, dict] = {}     # イベントの id -> 要約（events.apply_summaries が書く）。{"tries": 1} は失敗の記録
        self.trend_notes: list[dict] = []              # 「今日の傾向」の解説（trend.save_note が書く）
        self.bodies: dict[int, dict] = {}              # 記事の id -> 本文の取り出しの結果（bodies.apply が書く。state 1 は圧縮した本文・2 は取れない理由）
        self.bytes = 0

    def empty(self) -> bool:
        return not (self.new or self.updates or self.patches or self.fetchlog or self.states or self.token_used or self.extractions or self.event_summaries or self.trend_notes or self.bodies)

    def count(self) -> int:
        return len(self.new) + len(self.updates) + len(self.patches) + len(self.fetchlog) + len(self.states) + len(self.token_used) + len(self.extractions) + len(self.event_summaries) + len(self.trend_notes or self.bodies)


def merge_patch(old: dict | None, new: dict) -> dict:
    out = copy.deepcopy(old) if old else {}
    if new.get("set"):
        out.setdefault("set", {}).update(new["set"])
    if new.get("topics"):
        out["topics"] = list(dict.fromkeys([*out.get("topics", []), *new["topics"]]))
    if new.get("tries"):
        out["tries"] = int(out.get("tries", 0)) + int(new["tries"])
    return out


def apply_patch(obj, patch: dict) -> None:
    """NewsItem（ORM でも transient でも）に部分の更新を当てる。"""
    sets = patch.get("set") or {}
    for key, value in sets.items():
        setattr(obj, key, value)
    if patch.get("topics"):
        try:
            have = [t for t in json.loads(obj.topics or "[]") if isinstance(t, str)]
        except ValueError:
            have = []
        obj.topics = json.dumps(list(dict.fromkeys(have + list(patch["topics"])))[:TOPICS_MAX], ensure_ascii=False)
    if patch.get("tries") and "prep_tries" not in sets and (obj.prep_tries or 0) < PREP_DONE:
        obj.prep_tries = (obj.prep_tries or 0) + int(patch["tries"])


def cols(item) -> dict:
    from qnapnewshub.db import NewsItem

    return {c.key: getattr(item, c.key) for c in NewsItem.__table__.columns}


def clone(item):
    from qnapnewshub.db import NewsItem

    return NewsItem(**cols(item))


def _read_io() -> dict:
    out: dict = {}
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            key, _, value = line.partition(":")
            out[key.strip()] = int(value)
    except (OSError, ValueError):
        pass
    return out


class Coordinator:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._tlock = threading.Lock()
        self.lock = threading.RLock()
        self.buf = _Buf()
        self.inflight = _Buf()
        self._since: float | None = None          # バッファに最初の物が入った時刻（monotonic）
        self._dirty_wal = False
        self._was_rest = False
        self._last_assigned = 0
        self.last_flush_at: datetime | None = None
        self.last_flush_mono: float | None = None
        self.last_flush_info: dict = {}
        self.flush_history: deque = deque(maxlen=60)
        self.rest_writes: deque = deque(maxlen=100)
        self.writes: Counter = Counter()
        self.failures = 0
        self.last_error = ""
        self.stopped = False

    # ---- スレッド ----

    def _ensure(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._tlock:
            if self._thread is None or not self._thread.is_alive():
                self.stopped = False
                self._thread = threading.Thread(target=self._loop, name="newshub-writer", daemon=True)
                self._thread.start()

    def on_writer(self) -> bool:
        return threading.current_thread() is self._thread

    def run(self, name: str, fn: Callable, *, user: bool = True):
        """fn を書き手のスレッドで実行して結果を返す（即時の書き込み）。書き手の中から呼ばれたらそのまま実行。"""
        if self.on_writer():
            return self._exec(name, fn, user)
        self._ensure()
        fut: Future = Future()
        self._q.put((name, fn, user, fut))
        return fut.result()

    def _exec(self, name: str, fn: Callable, user: bool):
        from qnapnewshub import rest

        self.writes[name] += 1
        if rest.active():
            self.rest_writes.append({"at": datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "name": name, "user": bool(user)})
        result = fn()
        if name.startswith(DB_WRITE_PREFIXES) and not self.stopped:      # DB を書いた処理だけ、WAL を checkpoint する機会（次の間隔）を作る
            if self._since is None:
                self._since = time.monotonic()
            self._dirty_wal = True
        return result

    def _poke(self) -> None:
        self._ensure()
        self._q.put(("poke", None, True, None))

    def _timeout(self) -> float | None:
        from qnapnewshub import rest

        cfg = self.config()
        waits: list[float] = []
        if self._since is not None and (not self.buf.empty() or self._dirty_wal):
            waits.append(max(0.5, self._since + cfg["flush_seconds"] - time.monotonic()))
        if self._was_rest or rest.active():
            end = rest.next_end(rest.config(), rest.now())
            if end is not None:
                waits.append(max(0.5, (end - rest.now()).total_seconds() + 1))
        return min(waits) if waits else None

    def _loop(self) -> None:
        while True:
            try:
                job = self._q.get(timeout=self._timeout())
            except queue.Empty:
                job = None
            if job is _STOP:
                return
            if job is not None and job[3] is not None:
                name, fn, user, fut = job
                try:
                    fut.set_result(self._exec(name, fn, user))
                except BaseException as exc:  # noqa: BLE001 - 呼んだ側へ返す
                    fut.set_exception(exc)
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("まとめ書きの確認に失敗しました")

    # ---- 設定 ----

    def config(self) -> dict:
        try:
            from qnapnewshub import settings

            s = settings.load().get("storage", {})
        except Exception:  # noqa: BLE001
            s = {}
        return {"flush_seconds": int(s.get("flush_minutes", 30)) * 60, "max_items": int(s.get("max_items", 1000)),
                "max_bytes": int(s.get("max_mb", 128)) * 1024 * 1024}

    # ---- まとめ書きの判定 ----

    def over_limit(self) -> bool:
        cfg = self.config()
        return self.buf.count() >= cfg["max_items"] or self.buf.bytes >= cfg["max_bytes"]

    def memory_full(self) -> bool:
        """休止中のメモリ保持で、安全の上限（件数・容量の数倍）に達したか。達したら集めるのを止める。"""
        cfg = self.config()
        return self.buf.count() >= cfg["max_items"] * HARD_FACTOR_ITEMS or self.buf.bytes >= cfg["max_bytes"] * HARD_FACTOR_BYTES

    def _tick(self) -> None:
        from qnapnewshub import rest

        st = rest.state()
        if self._was_rest and not st["active"]:
            self._was_rest = False
            if not self.buf.empty() or self._dirty_wal:
                self._flush("rest_end")
            return
        self._was_rest = st["active"]
        if self.buf.empty() and not self._dirty_wal:
            return
        cfg = self.config()
        limit = self.over_limit()
        if st["active"]:
            if st["mode"] == "flush_at_limit" and limit:
                self._flush("limit")
            return          # keep_memory・stop_all は休止が終わるまで書かない
        if limit:
            self._flush("limit")
        elif self._since is not None and time.monotonic() - self._since >= cfg["flush_seconds"]:
            self._flush("interval")

    # ---- 積む（メモリだけ。ここでは書かない） ----

    def _touch(self, nbytes: int = 0) -> None:
        with self.lock:
            self.buf.bytes += nbytes
            if self._since is None:
                self._since = time.monotonic()
        self._poke()

    def stage_fetchlog(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self.lock:
            self.buf.fetchlog.extend(rows)
        self._touch(200 * len(rows))

    def stage_state(self, key: str, fn: Callable[[], None]) -> None:
        with self.lock:
            self.buf.states[key] = fn
        self._touch(100)

    def stage_extraction(self, article_id: int, data: dict) -> None:
        """要素の取り出し（entities・facts）を積む。同じ記事は後の分が勝つ。"""
        with self.lock:
            self.buf.extractions[article_id] = data
        self._touch(300 + 150 * (len(data.get("entities", [])) + len(data.get("facts", []))))

    def stage_event_summary(self, event_id: int, data: dict) -> None:
        """イベントの要約（または失敗の記録 {"tries": 1}）を積む。成功した要約は、失敗の記録より後でも先でも残る。"""
        with self.lock:
            old = self.buf.event_summaries.get(event_id) or {}
            merged = {**old, **data}
            if "tries" in old and "tries" in data:
                merged["tries"] = int(old["tries"]) + int(data["tries"])
            self.buf.event_summaries[event_id] = merged
        self._touch(300 + len(str(data.get("summary", ""))))

    def pending_event_summary(self, event_id: int) -> dict | None:
        with self.lock:
            return self.buf.event_summaries.get(event_id) or self.inflight.event_summaries.get(event_id)

    def stage_trend_note(self, note: dict) -> None:
        with self.lock:
            self.buf.trend_notes.append(note)
        self._touch(500 + len(str(note)))

    def pending_trend_notes(self) -> list[dict]:
        with self.lock:
            return [*self.inflight.trend_notes, *self.buf.trend_notes]

    def stage_body(self, item_id: int, rec: dict) -> None:
        """本文の取り出しの結果を積む（{"state": 1|2, "body": 圧縮した本文, "raw_len", "reason", "at"}）。記事の body_state の印も一緒に積む。"""
        with self.lock:
            self.buf.bodies[item_id] = rec
        self.patch_item(item_id, set={"body_state": rec["state"]})
        self._touch(len(rec.get("body") or b"") + 100)

    def has_unsaved_body(self, item_id: int) -> bool:
        with self.lock:
            return any(item_id in b.bodies for b in (self.inflight, self.buf))

    def bodies_today(self, since: datetime) -> int:
        """まだ書いていない本文の試行（成功・失敗とも）のうち、since 以降の数（1 日の上限の数え）。"""
        with self.lock:
            return sum(1 for b in (self.inflight, self.buf) for r in b.bodies.values() if r["at"] >= since)

    def stage_token_used(self, token_id: int, at: str) -> None:
        with self.lock:
            self.buf.token_used[token_id] = at
        self._touch(50)

    def patch_item(self, item_id: int, *, set: dict | None = None, topics: list[str] | None = None, tries: int = 0) -> None:  # noqa: A002
        patch = {"set": dict(set or {}), "topics": list(topics or []), "tries": tries}
        with self.lock:
            rec = self.buf.new.get(item_id)
            if rec is not None:
                apply_patch(rec, patch)
            else:
                self.buf.patches[item_id] = merge_patch(self.buf.patches.get(item_id), patch)
        self._touch(200 + sum(len(str(v)) for v in patch["set"].values()))

    # ---- 読み（バッファの分を重ねる） ----

    def pending_items(self) -> list:
        with self.lock:
            rows = [*self.inflight.new.values(), *self.buf.new.values()]
        out = []
        for r in rows:
            c = clone(r)
            c._unsaved = True
            for col in ("published_at", "fetched_at"):      # DB から読んだ行と同じ（時刻は UTC の naive）に揃える
                v = getattr(c, col)
                if v is not None and v.tzinfo is not None:
                    setattr(c, col, v.astimezone(_UTC).replace(tzinfo=None))
            out.append(c)
        for c in out:
            self._overlay(c, skip_new=True)
        return out

    def _overlay(self, item, *, skip_new: bool = False) -> bool:
        """DB の行（のコピー）に、まだ書いていない更新・部分の更新を重ねる。重ねたら True。"""
        hit = False
        with self.lock:
            for b in (self.inflight, self.buf):
                if item.url in b.updates:
                    for k, v in b.updates[item.url].items():
                        setattr(item, k, v)
                    hit = True
                if item.id in b.patches:
                    apply_patch(item, b.patches[item.id])
                    hit = True
        return hit

    def effective(self, row):
        """DB から読んだ行。未保存の更新があれば、重ねたコピー（_unsaved）を返す。無ければそのまま。"""
        with self.lock:
            touched = any(row.url in b.updates or row.id in b.patches for b in (self.inflight, self.buf))
        if not touched:
            return row
        c = clone(row)
        self._overlay(c)
        c._unsaved = True
        return c

    def has_pending_id(self, item_id: int) -> bool:
        with self.lock:
            return item_id in self.buf.new or item_id in self.inflight.new

    def pending_fetchlog(self) -> list[dict]:
        with self.lock:
            return [*self.inflight.fetchlog, *self.buf.fetchlog]

    def pending_by_source(self) -> Counter:
        c: Counter = Counter()
        for r in self.pending_items():
            c[r.source_key] += 1
        return c

    def counts(self) -> dict:
        with self.lock:
            b = self.buf
            return {"items": len(b.new), "updates": len(b.updates), "patches": len(b.patches), "fetchlog": len(b.fetchlog),
                    "states": len(b.states), "extractions": len(b.extractions), "bodies": len(b.bodies), "total": b.count(), "bytes": b.bytes, "inflight": self.inflight.count()}

    def unsaved_total(self) -> int:
        with self.lock:
            return self.buf.count() + self.inflight.count()

    def has_unsaved(self, item_id: int) -> bool:
        with self.lock:
            return any(item_id in b.new or item_id in b.patches for b in (self.inflight, self.buf))

    def allocate_id(self) -> int:
        from sqlalchemy import func, select

        from qnapnewshub import db as news_db
        from qnapnewshub.db import NewsItem

        with news_db.session() as db:
            top = db.scalar(select(func.max(NewsItem.id))) or 0
        with self.lock:
            self._last_assigned = max(self._last_assigned, top) + 1
            return self._last_assigned

    # ---- 保存 ----

    def flush(self, reason: str = "manual") -> dict:
        """いまあるバッファをすぐ書く（手動・停止・利用者の操作）。休止中でも書く（呼んだ処理名は記録される）。"""
        return self.run(f"flush:{reason}", lambda: self._flush(reason), user=reason in ("manual", "action", "shutdown"))

    def _flush(self, reason: str) -> dict:
        from qnapnewshub import auth, db as news_db, fetchlog, store

        started = time.monotonic()
        io0 = _read_io()
        with self.lock:
            snap, self.buf = self.buf, _Buf()
            # 2 回続けて失敗したときのために、inflight に前の分が残っていれば足す
            if not self.inflight.empty():
                self._absorb(snap, self.inflight)
            self.inflight = snap
            self._since = None
        info = {"at": datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason, "items": len(snap.new), "updates": len(snap.updates),
                "patches": len(snap.patches), "fetchlog": len(snap.fetchlog), "ok": True, "error": ""}
        if snap.empty():
            with self.lock:
                self.inflight = _Buf()
        else:
            try:
                with news_db.session() as db:
                    gained = store.apply_staged(db, list(snap.new.values()), snap.updates, snap.patches)
                    if snap.extractions:
                        from qnapnewshub import entities

                        entities.apply_extractions(db, snap.extractions)
                    if snap.bodies:
                        from qnapnewshub import bodies

                        bodies.apply(db, snap.bodies)
                    if snap.fetchlog:
                        fetchlog.record(db, snap.fetchlog, commit=False)
                    if snap.event_summaries:
                        from qnapnewshub import events as events_mod

                        events_mod.apply_summaries(db, snap.event_summaries)
                    if snap.trend_notes:
                        from qnapnewshub import trend

                        trend.save_notes(db, snap.trend_notes)
                    if snap.new or gained or snap.fetchlog:      # 分析の集計の表へ、増えた分だけを同じトランザクションで足す
                        from qnapnewshub import analytics

                        analytics.apply(db, list(snap.new.values()), gained, snap.fetchlog)
                    from qnapnewshub import events as events_mod

                    touched = set(snap.new) | set(snap.extractions)
                    if touched:      # 新しい記事・物の取り出しが保存された記事を、前後 48 時間の記事と見比べてイベントへ（同じトランザクション）
                        events_mod.update(db, touched)
                    if snap.new or snap.updates:
                        from qnapnewshub import channels

                        store.prune(db, channels.load()["archive_keep"], commit=False)
                        events_mod.cleanup(db)
                    db.commit()
                from qnapnewshub import analytics

                analytics.bump()
                for fn in snap.states.values():
                    fn()
                if snap.token_used:
                    auth.apply_token_used(snap.token_used)
                with self.lock:
                    self.inflight = _Buf()
            except Exception as exc:  # noqa: BLE001 - 消さずに戻して、次の機会に書く
                logger.exception("まとめ書きに失敗しました（バッファは残します）")
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                info.update(ok=False, error=self.last_error)
                with self.lock:
                    back, self.inflight = self.inflight, _Buf()
                    self._absorb(self.buf, back)
                    self._since = time.monotonic()
        info["checkpoint"] = self._checkpoint() if info["ok"] else {}
        self._dirty_wal = False
        io1 = _read_io()
        info["ms"] = int((time.monotonic() - started) * 1000)
        info["write_bytes"] = max(0, io1.get("write_bytes", 0) - io0.get("write_bytes", 0))
        if info["ok"]:
            self.last_flush_at = datetime.now(_UTC)
            self.last_flush_mono = time.monotonic()
        self.last_flush_info = info
        self.flush_history.append(info)
        from qnapnewshub import metrics

        metrics.note_flush(info)
        return info

    def _absorb(self, into: _Buf, older: _Buf) -> None:
        """older（先に積んだ分）を into（あとの分）の下に入れる。同じ物はあとの分が勝つ。"""
        for k, v in older.new.items():
            into.new.setdefault(k, v)
        for url, f in older.updates.items():
            into.updates[url] = {**f, **into.updates.get(url, {})}
            into.update_ids.setdefault(url, older.update_ids.get(url, 0))
        for i, p in older.patches.items():
            into.patches[i] = merge_patch(p, into.patches[i]) if i in into.patches else p
        into.fetchlog[:0] = older.fetchlog
        for k, v in older.states.items():
            into.states.setdefault(k, v)
        for k, v in older.extractions.items():
            into.extractions.setdefault(k, v)
        for k, v in older.event_summaries.items():
            into.event_summaries.setdefault(k, v)
        into.trend_notes[:0] = older.trend_notes
        for k, v in older.bodies.items():
            into.bodies.setdefault(k, v)
        for k, v in older.token_used.items():
            into.token_used.setdefault(k, v)
        into.bytes += older.bytes

    def _checkpoint(self) -> dict:
        """PASSIVE。WAL がまだ大きければ TRUNCATE。"""
        from sqlalchemy import text

        from qnapnewshub import auth, db as news_db

        out: dict = {}
        try:
            wal = Path(str(news_db.path()) + "-wal")
            if not wal.exists():
                return {"skipped": "wal なし"}
            with news_db.engine().connect() as conn:
                row = conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                out["passive"] = list(row) if row else []
            size = wal.stat().st_size if wal.exists() else 0
            if size > WAL_TRUNCATE_BYTES or (out["passive"] and out["passive"][0] == 1):
                with news_db.engine().connect() as conn:
                    row = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    out["truncate"] = list(row) if row else []
            del text
        except Exception:  # noqa: BLE001
            logger.exception("checkpoint に失敗しました")
        try:
            auth.checkpoint()
        except Exception:  # noqa: BLE001
            logger.exception("認証 DB の checkpoint に失敗しました")
        return out

    def checkpoint_if_large(self) -> None:
        """WAL が大きいとき（大量の作り直しの途中）だけ checkpoint（書き手のスレッドから）。ふだんのまとめ書きの checkpoint はそのまま。"""
        try:
            from qnapnewshub import db as news_db

            wal = Path(str(news_db.path()) + "-wal")
            if wal.exists() and wal.stat().st_size > WAL_TRUNCATE_BYTES:
                self._checkpoint()
        except OSError:
            pass

    def shutdown(self) -> dict:
        """停止: バッファを書いて、WAL を空にして、スレッドを止める。"""
        info: dict = {}
        if self._thread is not None and self._thread.is_alive():
            try:
                info = self.run("flush:shutdown", lambda: self._final(), user=True)
            except Exception:  # noqa: BLE001
                logger.exception("停止時の保存に失敗しました")
            self._q.put(_STOP)
            self._thread.join(timeout=10)
        elif not self.buf.empty():
            info = self._final()
        self.stopped = True
        return info

    def _final(self) -> dict:
        info = self._flush("shutdown")
        try:
            from sqlalchemy import text  # noqa: F401

            from qnapnewshub import db as news_db

            if Path(str(news_db.path()) + "-wal").exists():
                with news_db.engine().connect() as conn:
                    conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            logger.exception("停止時の checkpoint に失敗しました")
        return info


# ---- ファイルの書き込み（設定・秘密など。書き手のスレッドで） ----

def write_file(path: Path, data: str | bytes, *, name: str = "file", mode: int = 0o600, user: bool = True) -> None:
    """tmp に書いて置き換える（途中で止まっても半端なファイルを残さない）。"""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    path = Path(path)

    def do() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(tmp, path)

    run(name, do, user=user)


# ---- 現在の data_dir の係 ----

_coords: dict[str, Coordinator] = {}
_clock = threading.Lock()


def get() -> Coordinator:
    key = str(data_dir())
    with _clock:
        found = _coords.get(key)
        if found is None:
            found = _coords[key] = Coordinator(Path(key))
        return found


def run(name: str, fn: Callable, *, user: bool = True):
    return get().run(name, fn, user=user)


def flush(reason: str = "manual") -> dict:
    return get().flush(reason)


def shutdown() -> dict:
    return get().shutdown()


def forget_all() -> None:
    """試験の後始末: 書き手のスレッドを止め、バッファは捨てる。"""
    with _clock:
        coords = list(_coords.values())
        _coords.clear()
    for c in coords:
        if c._thread is not None and c._thread.is_alive():  # noqa: SLF001
            c._q.put(_STOP)  # noqa: SLF001
            c._thread.join(timeout=5)  # noqa: SLF001
