"""分析（ARCHITECTURE 7・7.5）: 集計の表と、そこだけを読む API の中身。LLM は使わない。

- **集計の表**（news.sqlite3 の ana_*）: 記事の数（日ごと・時間ごと × 分類・チャンネル・取得先・言語・出どころの種類）・話題の語の数・取得先ごとの成否。
  **増えた分だけ**を、Write Coordinator のまとめ書きと同じトランザクションで足す（apply）。常に書き続けない。
  初回・移行・チャンネルや出どころの設定を変えたあとは、既存の記事から作り直す（rebuild_in）。
- **画面の API は集計の表だけを読む**（記事の表は読まない）ので、記事が 100 万件でも件数に比例して遅くならない。答えはメモリにキャッシュし、
  データが変わったとき（まとめ書きの後・作り直し）と、時間の窓が動く 1 時間ごとにだけ作り直す。
- 日・時間の区切りは地域の時刻（store.local_tz）。記事の日時は published_at。近い重複（dup_of）は n には入れるが indep には入れない。
- ホットトピック: 語ごとに、直近 24 時間の（独立した）記事の数を、その前の最大 28 日の 1 日あたりの平均と散らばりと比べる。
  点数 = (今日 − 平均) / sqrt(平均 + 散らばり + k) に、独立した出どころの数の重みを掛ける。常に多い語は平均が高いので点数が上がらない。
  過去にほぼ出なかった語は「新しい語」の別の枠に出す。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from qnapnewshub import channels as channels_mod
from qnapnewshub import db as news_db
from qnapnewshub import sourcemgr, sources as sources_mod
from qnapnewshub.config import data_dir
from qnapnewshub.db import AnaCount, AnaFetch, AnaMeta, AnaTopic, AnaTopicSrc, NewsFetchLog, NewsItem

logger = logging.getLogger("qnapnewshub.analytics")

VERSION = 1
_UTC = timezone.utc
DIMS = ("all", "kind", "channel", "source", "lang", "origin")
BY_CHOICES = ("none", "kind", "channel", "source", "lang", "origin")
RANGE_HOURS = {"24h": 24, "7d": 168, "30d": 720, "90d": 2160}
HOURLY_KEEP_DAYS = 100          # 時間ごとの数を持つ日数
TOPIC_DAILY_KEEP_DAYS = 40      # 語の日ごとの数を持つ日数
TOPIC_HOURLY_KEEP_HOURS = 72    # 語の時間ごとの数・出どころの組を持つ時間
BASELINE_DAYS = 28
BASELINE_MIN_DAYS = 3           # これより履歴が短いと、急上昇の点数は出さない（新しい語の枠だけ）
SCORE_K = 2.0
MIN_COUNT = 2                   # 24 時間に独立した記事がこれ未満の語は候補にしない
NEW_MIN_COUNT = 3
NEW_MIN_SOURCES = 2
TOP_N = 10
SERIES_TOP = 8
TERMS_PER_ARTICLE = 24
KIND_FALLBACK_ORIGIN = {"release": "official", "news": "press", "model": "other", "forum": "community", "reddit": "community", "event": "other"}
LANG_LABELS = {"ja": "日本語", "en": "英語"}

# ---- 言語・語 ----

_JA = re.compile(r"[぀-ヿ㐀-鿿]")
_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.+#-][A-Za-z0-9]+)*")
_KATAKANA = re.compile(r"[ァ-ヴー]{3,}")
_STOP_EN = frozenset("""a about above after again all also am an and any are as at be because been before being below between both but by can could did do does doing
down during each few for from further had has have having he her here hers him his how i if in into is it its itself just me more most my no nor not now of off on once
only or other our out over own same she should so some such than that the their them then there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your yours says say said new use using used via vs get gets got make makes made one two three first last next top best
now today week year years day days time times way ways like just more many much may might must need needs needed show shows showed take takes took see sees saw look looks
into over than http https www com org net html amp
url urls comments comment points point article articles read more link links released release releases ahead another every even still back around against among
announces announced announce reports report reportedly says launch launches launched first-ever plus update updates updated gets getting going goes goes
after before using based ever never always often really actually already maybe perhaps whether without within""".split())
_STOP_JA = frozenset(("ニュース", "アップデート", "アプリ", "サービス", "ソフト", "ソフトウェア", "ユーザー", "データ", "システム", "機能", "リリース", "コメント", "ページ", "サイト",
                      "ブログ", "メール", "ツール", "モデル", "バージョン", "ポイント", "ランキング", "ニュースハブ"))


def lang_of(title: str) -> str:
    """題に仮名・漢字があれば ja、無ければ en（言語の列は記事に無いので、題から決める）。"""
    return "ja" if _JA.search(title or "") else "en"


def title_terms(title: str) -> dict[str, str]:
    """見出しの語: 英語は小文字の語と 2 語の組（ストップワード除外）、日本語はカタカナの語の塊。{小文字の語: 表記}。"""
    out: dict[str, str] = {}
    tokens: list[tuple[str, str]] = []
    for m in _EN_WORD.finditer(title or ""):
        raw = m.group(0).rstrip(".+#-")
        low = raw.lower()
        if len(low) < 2 or low in _STOP_EN or low.isdigit():
            tokens.append(("", ""))      # 語の切れ目（2 語の組を作らない）
            continue
        tokens.append((low, raw))
    for low, raw in tokens[:40]:
        if low and len(low) >= 3:
            out.setdefault(low, low if raw.islower() or raw.istitle() else raw)
    for (a, _), (b, _) in zip(tokens[:40], tokens[1:41]):
        if a and b and len(a) >= 3 and len(b) >= 3:
            out.setdefault(f"{a} {b}", f"{a} {b}")
    for m in _KATAKANA.finditer(title or ""):
        token = m.group(0)
        if token not in _STOP_JA:
            out.setdefault(token.lower(), token)
    return out


def terms_of(title: str, topics: list[str]) -> dict[str, str]:
    """記事の語（小文字 → 表記）。今ある topics（辞書の語・固有名詞らしい語）が表記で先、見出しの語が続く。最大 TERMS_PER_ARTICLE。"""
    out: dict[str, str] = {}
    for t in topics:
        if isinstance(t, str) and t.strip() and len(t) <= 60 and t.strip().lower() not in _STOP_EN:
            out.setdefault(t.strip().lower(), t.strip())
    for low, label in title_terms(title).items():
        if len(low) <= 60:
            out.setdefault(low, label)
    return dict(list(out.items())[:TERMS_PER_ARTICLE])


# ---- 時刻 ----

def local_tz():
    return datetime.now().astimezone().tzinfo


def _as_local(value: datetime, tz=None) -> datetime:
    value = value.replace(tzinfo=_UTC) if value.tzinfo is None else value
    return value.astimezone(tz or local_tz())


def day_key(local: datetime) -> str:
    return local.strftime("%Y-%m-%d")


def hour_key(local: datetime) -> str:
    return local.strftime("%Y-%m-%d %H")


# ---- 設定から作る判定（チャンネル・出どころ） ----

class Resolver:
    """記事 1 件のチャンネル・出どころの種類を決める（設定を 1 回だけ読む）。"""

    def __init__(self) -> None:
        self.channels = channels_mod.load()["channels"]
        cfg = sources_mod.load_config()
        entries = sourcemgr.entries(cfg)
        self.origins = {e["key"]: e["origin"] for e in entries}
        self.names = {e["key"]: e["name"] for e in entries}
        self.hidden = sources_mod.HIDDEN_SOURCE_PREFIXES
        self.tz = local_tz()

    def sig(self) -> str:
        body = json.dumps({"v": VERSION, "ch": [[c["id"], c["include"], c["exclude"], c["kinds"]] for c in self.channels], "origins": sorted(self.origins.items())},
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]

    def channel(self, kind: str, source_key: str, title: str, summary: str) -> str:
        for c in self.channels:
            if channels_mod.matches(c, kind=kind, source_key=source_key, title=title, summary=summary or ""):
                return c["id"]
        return ""

    def origin(self, kind: str, source_key: str) -> str:
        return self.origins.get(source_key) or KIND_FALLBACK_ORIGIN.get(kind, "other")


def _facts(res: Resolver, *, kind: str, source_key: str, title: str, summary: str, topics_json: str, published_at: datetime, image_url, dup_of, user_id) -> dict | None:
    """記事 1 件が集計に入れるもの。出来事（利用者宛て）・隠すソースは入れない（None）。"""
    if kind == "event" or user_id is not None or (res.hidden and source_key.startswith(res.hidden)):
        return None
    try:
        topics = json.loads(topics_json or "[]")
    except ValueError:
        topics = []
    local = _as_local(published_at, res.tz)
    return {"kind": kind, "lang": lang_of(title), "source": source_key, "channel": res.channel(kind, source_key, title, summary), "origin": res.origin(kind, source_key),
            "indep": 0 if dup_of else 1, "img": 1 if image_url else 0, "d": day_key(local), "h": hour_key(local),
            "terms": terms_of(title, topics if isinstance(topics, list) else [])}


class _Acc:
    """集計の途中の数。"""

    def __init__(self) -> None:
        self.count: Counter = Counter()          # (grain, dim, t, kind, lang, key, col) -> 数
        self.topic: Counter = Counter()          # (grain, t, term, kind, lang, channel, col) -> 数
        self.label: dict[tuple, str] = {}
        self.tsrc: set[tuple] = set()
        self.fetch: Counter = Counter()          # (day, source_key, col) -> 数

    def add(self, f: dict, *, hourly_from: str, topic_day_from: str, topic_hour_from: str, only_img: bool = False) -> None:
        keys = {"all": "", "kind": f["kind"], "channel": f["channel"], "source": f["source"], "lang": f["lang"], "origin": f["origin"]}
        grains = [("d", f["d"])] + ([("h", f["h"])] if f["h"] >= hourly_from else [])
        for grain, t in grains:
            for dim in DIMS:
                if dim == "channel" and not f["channel"]:
                    key = "-"                      # どのチャンネルにも入らない記事
                else:
                    key = keys[dim]
                base = (grain, dim, t, f["kind"], f["lang"], key)
                if only_img:
                    self.count[base + ("img",)] += f["img"]
                    continue
                self.count[base + ("n",)] += 1
                self.count[base + ("indep",)] += f["indep"]
                self.count[base + ("img",)] += f["img"]
        if only_img or not f["terms"]:
            return
        tgrains = []
        if f["d"] >= topic_day_from:
            tgrains.append(("d", f["d"]))
        if f["h"] >= topic_hour_from:
            tgrains.append(("h", f["h"]))
        for grain, t in tgrains:
            for term, label in f["terms"].items():
                base = (grain, t, term, f["kind"], f["lang"], f["channel"])
                self.topic[base + ("n",)] += 1
                self.topic[base + ("indep",)] += f["indep"]
                self.label[base] = label
                if grain == "h":
                    self.tsrc.add((t, term, f["source"][:200], f["kind"], f["lang"]))

    def add_fetch(self, row: dict, tz) -> None:
        if row.get("skipped"):
            return
        day = day_key(_as_local(row["at"], tz))
        key = (day, str(row["source_key"])[:200])
        self.fetch[key + ("runs",)] += 1
        self.fetch[key + ("ok",)] += 1 if row.get("ok") else 0
        self.fetch[key + ("s304",)] += 1 if row.get("status") == 304 else 0
        self.fetch[key + ("new",)] += int(row.get("new", 0) or 0)


def _cutoffs(now: datetime, tz) -> tuple[str, str, str]:
    local = now.astimezone(tz)
    return (day_key(local - timedelta(days=HOURLY_KEEP_DAYS)) + " 00", day_key(local - timedelta(days=TOPIC_DAILY_KEEP_DAYS)),
            hour_key(local - timedelta(hours=TOPIC_HOURLY_KEEP_HOURS)))


# ---- 書く（Write Coordinator のまとめ書きから・書き手のスレッドで） ----

def _flush_acc(db: Session, acc: _Acc, *, replace: bool) -> None:
    """集計の途中の数を表へ。replace=False は足し込み（ON CONFLICT で加算）、True は空の表への挿入。"""
    counts: dict[tuple, dict] = {}
    for (grain, dim, t, kind, lang, key, col), v in acc.count.items():
        row = counts.setdefault((grain, dim, t, kind, lang, key), {"grain": grain, "dim": dim, "t": t, "kind": kind, "lang": lang, "key": key, "n": 0, "indep": 0, "img": 0})
        row[col] += v
    topics: dict[tuple, dict] = {}
    for (grain, t, term, kind, lang, channel, col), v in acc.topic.items():
        row = topics.setdefault((grain, t, term, kind, lang, channel), {"grain": grain, "t": t, "term": term, "kind": kind, "lang": lang, "channel": channel, "n": 0, "indep": 0,
                                                                       "label": acc.label.get((grain, t, term, kind, lang, channel), term)})
        row[col] += v
    fetch: dict[tuple, dict] = {}
    for (day, key, col), v in acc.fetch.items():
        row = fetch.setdefault((day, key), {"day": day, "source_key": key, "runs": 0, "ok": 0, "s304": 0, "new": 0})
        row[col] += v
    _upsert(db, AnaCount, list(counts.values()), ("n", "indep", "img"), replace)
    _upsert(db, AnaTopic, list(topics.values()), ("n", "indep"), replace, also=("label",))
    _upsert(db, AnaFetch, list(fetch.values()), ("runs", "ok", "s304", "new"), replace)
    if acc.tsrc:
        rows = [{"t": t, "term": term, "source_key": s, "kind": k, "lang": l} for t, term, s, k, l in acc.tsrc]
        for i in range(0, len(rows), 5000):
            db.execute(sqlite_insert(AnaTopicSrc.__table__).prefix_with("OR IGNORE"), rows[i:i + 5000])


def _upsert(db: Session, model, rows: list[dict], add_cols: tuple[str, ...], replace: bool, also: tuple[str, ...] = ()) -> None:
    if not rows:
        return
    table = model.__table__
    for i in range(0, len(rows), 5000):
        chunk = rows[i:i + 5000]
        if replace:
            db.execute(table.insert(), chunk)
            continue
        stmt = sqlite_insert(table)
        set_ = {c: table.c[c] + stmt.excluded[c] for c in add_cols}
        set_.update({c: stmt.excluded[c] for c in also})
        stmt = stmt.on_conflict_do_update(index_elements=[c.name for c in table.primary_key.columns], set_=set_)
        db.execute(stmt, chunk)


_last_prune = {"at": 0.0}


def _prune(db: Session, now: datetime, tz, *, force: bool = False) -> None:
    """持つ期間を過ぎた時間ごと・語の行を消す（1 時間に 1 回まで。同じトランザクション）。"""
    if not force and time.monotonic() - _last_prune["at"] < 3600:
        return
    _last_prune["at"] = time.monotonic()
    hourly_from, topic_day_from, topic_hour_from = _cutoffs(now, tz)
    db.execute(delete(AnaCount).where(AnaCount.grain == "h", AnaCount.t < hourly_from))
    db.execute(delete(AnaTopic).where(AnaTopic.grain == "d", AnaTopic.t < topic_day_from))
    db.execute(delete(AnaTopic).where(AnaTopic.grain == "h", AnaTopic.t < topic_hour_from))
    db.execute(delete(AnaTopicSrc).where(AnaTopicSrc.t < topic_hour_from))


def _meta(db: Session, key: str) -> str | None:
    row = db.get(AnaMeta, key)
    return row.value if row else None


def _set_meta(db: Session, key: str, value: str) -> None:
    row = db.get(AnaMeta, key)
    if row is None:
        db.add(AnaMeta(key=key, value=value))
    else:
        row.value = value


def apply(db: Session, new_rows: list, image_gain: list, fetch_rows: list[dict]) -> None:
    """まとめ書きと同じトランザクションで、増えた分だけを足す。new_rows は新しく保存した記事、image_gain は保存の後で画像が付いた既存の記事、
    fetch_rows は取得の履歴。集計がまだ無い・設定の指紋が変わったときは、既存の記事から作り直す（この中に新しい記事も含まれる）。"""
    res = Resolver()
    if _meta(db, "sig") != res.sig():
        rebuild_in(db, res)
        return
    now = datetime.now(_UTC)
    hourly_from, topic_day_from, topic_hour_from = _cutoffs(now, res.tz)
    acc = _Acc()
    for r in new_rows:
        f = _facts(res, kind=r.kind, source_key=r.source_key, title=r.title, summary=r.summary or "", topics_json=r.topics, published_at=r.published_at,
                   image_url=r.image_url, dup_of=r.dup_of, user_id=r.user_id)
        if f:
            acc.add(f, hourly_from=hourly_from, topic_day_from=topic_day_from, topic_hour_from=topic_hour_from)
    for r in image_gain:
        f = _facts(res, kind=r.kind, source_key=r.source_key, title=r.title, summary=r.summary or "", topics_json=r.topics, published_at=r.published_at,
                   image_url=r.image_url, dup_of=r.dup_of, user_id=r.user_id)
        if f:
            acc.add(f, hourly_from=hourly_from, topic_day_from=topic_day_from, topic_hour_from=topic_hour_from, only_img=True)
    for row in fetch_rows:
        acc.add_fetch(row, res.tz)
    _flush_acc(db, acc, replace=False)
    _prune(db, now, res.tz)


def rebuild_in(db: Session, res: Resolver | None = None) -> dict:
    """既存の記事・取得の履歴から集計の表を作り直す（呼ぶ側のトランザクションの中。コミットは呼ぶ側）。"""
    started = time.monotonic()
    res = res or Resolver()
    now = datetime.now(_UTC)
    hourly_from, topic_day_from, topic_hour_from = _cutoffs(now, res.tz)
    for model in (AnaCount, AnaTopic, AnaTopicSrc, AnaFetch):
        db.execute(delete(model))
    acc = _Acc()
    n = 0
    q = select(NewsItem.kind, NewsItem.source_key, NewsItem.title, NewsItem.summary, NewsItem.topics, NewsItem.published_at, NewsItem.image_url, NewsItem.dup_of,
               NewsItem.user_id).execution_options(yield_per=5000)
    for r in db.execute(q):
        f = _facts(res, kind=r.kind, source_key=r.source_key, title=r.title, summary=(r.summary or "")[:400], topics_json=r.topics, published_at=r.published_at,
                   image_url=r.image_url, dup_of=r.dup_of, user_id=r.user_id)
        if f:
            acc.add(f, hourly_from=hourly_from, topic_day_from=topic_day_from, topic_hour_from=topic_hour_from)
            n += 1
    for r in db.execute(select(NewsFetchLog.source_key, NewsFetchLog.at, NewsFetchLog.ok, NewsFetchLog.status, NewsFetchLog.new, NewsFetchLog.skipped)):
        acc.add_fetch({"source_key": r.source_key, "at": r.at, "ok": r.ok, "status": r.status, "new": r.new, "skipped": r.skipped}, res.tz)
    _flush_acc(db, acc, replace=True)
    _set_meta(db, "sig", res.sig())
    _set_meta(db, "built_at", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _last_prune["at"] = time.monotonic()
    return {"articles": n, "ms": int((time.monotonic() - started) * 1000)}


# ---- キャッシュと作り直しの入口 ----

_rev: dict[str, int] = {}
_cache: dict[tuple, tuple[int, object]] = {}
_clock = threading.Lock()
_ready: dict[str, float] = {}
_rebuild_lock = threading.RLock()
SIG_RECHECK_SECONDS = 30


def _root() -> str:
    return str(data_dir())


def bump() -> None:
    """集計の表が変わった（コミットの後に呼ぶ）。キャッシュを無効にする。"""
    with _clock:
        _rev[_root()] = _rev.get(_root(), 0) + 1
        _ready.pop(_root(), None)


def cached(key: tuple, build):
    """データが変わったときと、時間の窓が動く 1 時間ごとにだけ作り直す。"""
    root = _root()
    stamp = hour_key(datetime.now().astimezone())
    full = (root, stamp, key)
    with _clock:
        rev = _rev.get(root, 0)
        hit = _cache.get(full)
        if hit is not None and hit[0] == rev:
            return hit[1]
    value = build()
    with _clock:
        if len(_cache) > 400:
            _cache.clear()
        _cache[full] = (rev, value)
    return value


def rebuild(reason: str = "manual") -> dict:
    """書き手のスレッドで作り直す（利用者の操作・初回）。"""
    from qnapnewshub import writer

    def do() -> dict:
        with news_db.session() as db:
            info = rebuild_in(db)
            db.commit()
        return info

    with _rebuild_lock:
        info = writer.run(f"news.analytics:{reason}", do)
    bump()
    logger.info("集計の表を作り直しました: %s", info)
    return info


def ensure() -> None:
    """集計の表が無い・設定の指紋が違うなら作り直す（API の読みの前に。確かめるのは 30 秒に 1 回まで）。休止中は作らない。"""
    from qnapnewshub import rest

    root = _root()
    if time.monotonic() - _ready.get(root, -1e9) < SIG_RECHECK_SECONDS:
        return
    sig = Resolver().sig()
    with news_db.session() as db:
        have = _meta(db, "sig")
    if have != sig and not rest.active():
        with _rebuild_lock:
            with news_db.session() as db:
                have = _meta(db, "sig")
            if have != sig:
                rebuild("auto")
    _ready[root] = time.monotonic()


def unsaved_count() -> int:
    from qnapnewshub import writer

    return len(writer.get().pending_items())


# ---- 読む（集計の表だけ） ----

def parse_filter(kind: str, lang: str) -> tuple[list[str], str]:
    kinds = [k for k in (kind or "").split(",") if k in sources_mod.KINDS]
    return kinds, lang if lang in LANG_LABELS else ""


def _conds(model, kinds: list[str], lang: str) -> list:
    out = []
    if kinds:
        out.append(model.kind.in_(kinds))
    if lang:
        out.append(model.lang == lang)
    return out


def _names(res: Resolver) -> dict[str, dict[str, str]]:
    kind_labels = dict(sources_mod.KIND_LABELS)
    return {"kind": kind_labels, "origin": dict(sources_mod.ORIGIN_LABELS), "lang": dict(LANG_LABELS), "source": res.names,
            "channel": {c["id"]: c["name"] for c in res.channels} | {"-": "どれにも入らない"}, "all": {"": "記事"}}


def _now_local(now: datetime | None = None) -> datetime:
    return (now or datetime.now(_UTC)).astimezone(local_tz())


def _sum(db: Session, model, col: str, *where) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(getattr(model, col)), 0)).where(*where)) or 0)


def overview(db: Session, kind: str = "", lang: str = "", now: datetime | None = None) -> dict:
    kinds, lang = parse_filter(kind, lang)

    def build() -> dict:
        local = _now_local(now)
        today = day_key(local)
        base = [AnaCount.grain == "d", AnaCount.dim == "all", *_conds(AnaCount, kinds, lang)]
        prev_from, prev_to = day_key(local - timedelta(days=7)), day_key(local - timedelta(days=1))
        prev_total = _sum(db, AnaCount, "n", *base, AnaCount.t >= prev_from, AnaCount.t <= prev_to)
        prev_days = int(db.scalar(select(func.count(func.distinct(AnaCount.t))).where(AnaCount.grain == "d", AnaCount.dim == "all", AnaCount.t >= prev_from, AnaCount.t <= prev_to)) or 0)
        avg = prev_total / prev_days if prev_days else None
        h24_from = hour_key(local - timedelta(hours=23))
        h = [AnaCount.grain == "h", AnaCount.dim == "all", AnaCount.t >= h24_from, *_conds(AnaCount, kinds, lang)]
        last24 = _sum(db, AnaCount, "n", *h)
        today_n = _sum(db, AnaCount, "n", *base, AnaCount.t == today)
        src_today = int(db.scalar(select(func.count(func.distinct(AnaCount.key))).where(AnaCount.grain == "d", AnaCount.dim == "source", AnaCount.t == today, *_conds(AnaCount, kinds, lang))) or 0)
        src_total = int(db.scalar(select(func.count(func.distinct(AnaCount.key))).where(AnaCount.grain == "d", AnaCount.dim == "source", AnaCount.t >= prev_from, *_conds(AnaCount, kinds, lang))) or 0)
        return {"today": today_n, "today_indep": _sum(db, AnaCount, "indep", *base, AnaCount.t == today),
                "last24h": last24, "last24h_indep": _sum(db, AnaCount, "indep", *h),
                "sources_today": src_today, "sources_8d": src_total,
                "prev7_daily_avg": round(avg, 1) if avg is not None else None, "prev7_days": prev_days,
                "ratio_24h": round(last24 / avg, 2) if avg else None, "ratio_today": round(today_n / avg, 2) if avg else None,
                "day": today, "built_at": _meta(db, "built_at") or ""}
    out = dict(cached(("overview", tuple(kinds), lang), build))
    out["unsaved"] = unsaved_count()
    return out


def _grid(range_: str, grain: str, now: datetime | None) -> list[str]:
    local = _now_local(now)
    hours = RANGE_HOURS[range_]
    if grain == "hour":
        end = local.replace(minute=0, second=0, microsecond=0)
        return [hour_key(end - timedelta(hours=i)) for i in range(min(hours, 720) - 1, -1, -1)]
    days = max(1, hours // 24)
    return [day_key(local - timedelta(days=i)) for i in range(days - 1, -1, -1)]


def series(db: Session, *, range_: str = "7d", grain: str = "", by: str = "none", kind: str = "", lang: str = "", metric: str = "all", now: datetime | None = None) -> dict:
    range_ = range_ if range_ in RANGE_HOURS else "7d"
    grain = grain if grain in ("hour", "day") else ("hour" if range_ in ("24h", "7d") else "day")
    if grain == "hour" and range_ == "90d":
        grain = "day"
    by = by if by in BY_CHOICES else "none"
    metric = "indep" if metric == "indep" else "all"
    kinds, lang = parse_filter(kind, lang)

    def build() -> dict:
        res = Resolver()
        buckets = _grid(range_, grain, now)
        pos = {t: i for i, t in enumerate(buckets)}
        col = AnaCount.indep if metric == "indep" else AnaCount.n
        dim = "all" if by == "none" else by
        rows = db.execute(select(AnaCount.t, AnaCount.key, func.sum(col)).where(
            AnaCount.grain == ("h" if grain == "hour" else "d"), AnaCount.dim == dim, AnaCount.t >= buckets[0], AnaCount.t <= buckets[-1] + "~",
            *_conds(AnaCount, kinds, lang)).group_by(AnaCount.t, AnaCount.key)).all()
        per: dict[str, list[int]] = defaultdict(lambda: [0] * len(buckets))
        for t, key, v in rows:
            if t in pos:
                per[key][pos[t]] += int(v or 0)
        totals = {k: sum(v) for k, v in per.items()}
        order = sorted(per, key=lambda k: (-totals[k], k))
        names = _names(res)[dim]
        colors = {c["id"]: c["color"] for c in res.channels}
        shown = order[:SERIES_TOP]
        out = [{"key": k, "label": names.get(k, k) if dim != "source" else (names.get(k) or k), "counts": per[k], "total": totals[k], **({"color": colors[k]} if dim == "channel" and k in colors else {})} for k in shown]
        rest = order[SERIES_TOP:]
        if rest:
            out.append({"key": "_other", "label": "その他", "counts": [sum(per[k][i] for k in rest) for i in range(len(buckets))], "total": sum(totals[k] for k in rest), "other_n": len(rest)})
        return {"range": range_, "grain": grain, "by": by, "metric": metric, "buckets": buckets, "series": out, "total": sum(totals.values())}
    return cached(("series", range_, grain, by, tuple(kinds), lang, metric, hour_key(_now_local(now))), build)


def _topic_dict(term: str, label: str, score: float | None, cur: int, cur_all: int, mean: float, sd: float, sources: int) -> dict:
    return {"term": term, "label": label, "score": round(score, 2) if score is not None else None, "count": cur, "count_all": cur_all, "baseline_mean": round(mean, 2),
            "baseline_sd": round(sd, 2), "growth_pct": round((cur - mean) / max(mean, 1.0) * 100) if mean is not None else None, "sources": sources}


def _drop_subsumed(rows: list[dict], ratio: float = 0.8) -> list[dict]:
    """2 語の組でほぼ説明できる 1 語を外す（「jensen」「huang」「jensen huang」が並ぶのを 1 つに）。
    語 A の語の並びが、もっと長い語 B の中に含まれ、B の記事の数が A の ratio 以上なら A を外す。
    2026-11-24 の実データの撮影で、同じ出来事の語が上位を埋めていたため。"""
    toks = {r["term"]: str(r["term"]).lower().split() for r in rows}
    keep = []
    for r in rows:
        a = toks[r["term"]]
        covered = False
        for other in rows:
            b = toks[other["term"]]
            if other is r or len(b) <= len(a):
                continue
            if any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1)) and other["count"] >= ratio * r["count"]:
                covered = True
                break
        if not covered:
            keep.append(r)
    return keep


def hot_topics(db: Session, kind: str = "", lang: str = "", now: datetime | None = None) -> dict:
    kinds, lang = parse_filter(kind, lang)

    def build() -> dict:
        res = Resolver()
        local = _now_local(now)
        h_from = hour_key(local - timedelta(hours=23))
        d_to = day_key(local - timedelta(days=2))
        d_from = day_key(local - timedelta(days=1 + BASELINE_DAYS))
        cur: dict[tuple[str, str], list] = {}      # (term, channel) -> [indep, n, label]
        for term, ch, indep, n, label in db.execute(select(AnaTopic.term, AnaTopic.channel, func.sum(AnaTopic.indep), func.sum(AnaTopic.n), func.max(AnaTopic.label)).where(
                AnaTopic.grain == "h", AnaTopic.t >= h_from, *_conds(AnaTopic, kinds, lang)).group_by(AnaTopic.term, AnaTopic.channel)):
            cur[(term, ch)] = [int(indep or 0), int(n or 0), label]
        cand_terms = {}
        for (term, _ch), (indep, n, label) in cur.items():
            c = cand_terms.setdefault(term, [0, 0, label])
            c[0] += indep
            c[1] += n
        cand_terms = {t: v for t, v in cand_terms.items() if v[0] >= MIN_COUNT}
        days = int(db.scalar(select(func.count(func.distinct(AnaCount.t))).where(AnaCount.grain == "d", AnaCount.dim == "all", AnaCount.t >= d_from, AnaCount.t <= d_to)) or 0)
        # 基準: 語 × チャンネル × 日 の独立した記事の数（候補の語だけ）
        base: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
        if cand_terms:
            for term, ch, t, indep in db.execute(select(AnaTopic.term, AnaTopic.channel, AnaTopic.t, func.sum(AnaTopic.indep)).where(
                    AnaTopic.grain == "d", AnaTopic.t >= d_from, AnaTopic.t <= d_to, *_conds(AnaTopic, kinds, lang)).group_by(AnaTopic.term, AnaTopic.channel, AnaTopic.t)):
                if term in cand_terms:
                    base[(term, ch)][t] = int(indep or 0)
        srcs: dict[str, int] = {}
        srcs_ch: dict[tuple[str, str], int] = {}
        if cand_terms:
            for term, c in db.execute(select(AnaTopicSrc.term, func.count(func.distinct(AnaTopicSrc.source_key))).where(
                    AnaTopicSrc.t >= h_from, *_conds(AnaTopicSrc, kinds, lang)).group_by(AnaTopicSrc.term)):
                srcs[term] = int(c)

        def stats(parts: list[tuple[str, str]]) -> tuple[float, float, int]:
            per_day: Counter = Counter()
            for p in parts:
                per_day.update(base.get(p, {}))
            total = sum(per_day.values())
            if days <= 0:
                return 0.0, 0.0, total
            mean = total / days
            var = max(0.0, sum(v * v for v in per_day.values()) / days - mean * mean)
            return mean, math.sqrt(var), total

        def score(cur_n: int, mean: float, sd: float, nsrc: int) -> float:
            z = (cur_n - mean) / math.sqrt(mean + sd + SCORE_K)
            return z * math.sqrt(min(max(nsrc, 1), 4) / 4)

        enough = days >= BASELINE_MIN_DAYS
        rows, new = [], []
        by_channel: dict[str, list] = defaultdict(list)
        parts_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for (term, ch) in cur:
            parts_of[term].append((term, ch))
        for term, (indep, n, label) in cand_terms.items():
            mean, sd, total = stats(parts_of[term])
            nsrc = srcs.get(term, 1)
            if enough and total <= 1 and indep >= NEW_MIN_COUNT and nsrc >= NEW_MIN_SOURCES:
                new.append(_topic_dict(term, label, None, indep, n, mean, sd, nsrc))
            elif enough and indep >= MIN_COUNT:
                s = score(indep, mean, sd, nsrc)
                if s > 0:
                    rows.append(_topic_dict(term, label, s, indep, n, mean, sd, nsrc))
        # 分野（チャンネル）ごと: 語の集計はチャンネルの中で
        if enough:
            for (term, ch), (indep, n, label) in cur.items():
                if term not in cand_terms or not ch or indep < MIN_COUNT:
                    continue
                mean, sd, total = stats([(term, ch)])
                if total <= 1 and indep >= NEW_MIN_COUNT:
                    continue
                s = score(indep, mean, sd, srcs.get(term, 1))
                if s > 0:
                    by_channel[ch].append(_topic_dict(term, label, s, indep, n, mean, sd, srcs.get(term, 1)))
        rows = _drop_subsumed(rows)
        new = _drop_subsumed(new)
        rows.sort(key=lambda r: (-r["score"], -r["count"], r["term"]))
        new.sort(key=lambda r: (-r["count"], -r["sources"], r["term"]))
        top = rows[:TOP_N]
        fields = []
        for c in res.channels:
            items = sorted(by_channel.get(c["id"], []), key=lambda r: (-r["score"], -r["count"], r["term"]))[:3]
            if items:
                fields.append({"channel": c["id"], "name": c["name"], "color": c["color"], "top": items})
        # 小さな推移（日ごと・直近 8 日）
        want = {r["term"] for r in top} | {r["term"] for r in new[:TOP_N]} | {r["term"] for f in fields for r in f["top"]}
        labels = [day_key(local - timedelta(days=i)) for i in range(7, -1, -1)]
        spark: dict[str, list[int]] = {t: [0] * len(labels) for t in want}
        if want:
            pos = {t: i for i, t in enumerate(labels)}
            for term, t, indep in db.execute(select(AnaTopic.term, AnaTopic.t, func.sum(AnaTopic.indep)).where(
                    AnaTopic.grain == "d", AnaTopic.t >= labels[0], AnaTopic.term.in_(sorted(want)), *_conds(AnaTopic, kinds, lang)).group_by(AnaTopic.term, AnaTopic.t)):
                if t in pos:
                    spark[term][pos[t]] = int(indep or 0)
        for r in [*top, *new[:TOP_N], *[x for f in fields for x in f["top"]]]:
            r["spark"] = spark.get(r["term"], [])
        return {"window_hours": 24, "baseline_days": days, "enough_history": enough, "spark_days": labels, "top": top, "new": new[:TOP_N], "fields": fields,
                "method": "（直近 24 時間の独立した記事の数 − 過去の 1 日の平均）÷ √(平均 + 散らばり + 2)。独立した出どころの数で重みを付ける。"}
    return cached(("hot", tuple(kinds), lang, hour_key(_now_local(now))), build)


def sources_analysis(db: Session, *, range_: str = "7d", kind: str = "", lang: str = "", now: datetime | None = None) -> dict:
    range_ = range_ if range_ in RANGE_HOURS else "7d"
    kinds, lang = parse_filter(kind, lang)

    def build() -> dict:
        res = Resolver()
        local = _now_local(now)
        days = max(1, RANGE_HOURS[range_] // 24)
        labels = [day_key(local - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        pos = {t: i for i, t in enumerate(labels)}
        agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "indep": 0, "img": 0, "runs": 0, "ok": 0, "s304": 0, "new": 0, "daily": [0] * len(labels)})
        for t, key, n, indep, img in db.execute(select(AnaCount.t, AnaCount.key, func.sum(AnaCount.n), func.sum(AnaCount.indep), func.sum(AnaCount.img)).where(
                AnaCount.grain == "d", AnaCount.dim == "source", AnaCount.t >= labels[0], AnaCount.t <= labels[-1], *_conds(AnaCount, kinds, lang)).group_by(AnaCount.t, AnaCount.key)):
            a = agg[key]
            a["n"] += int(n or 0)
            a["indep"] += int(indep or 0)
            a["img"] += int(img or 0)
            if t in pos:
                a["daily"][pos[t]] += int(n or 0)
        for key, runs, ok, s304, new in db.execute(select(AnaFetch.source_key, func.sum(AnaFetch.runs), func.sum(AnaFetch.ok), func.sum(AnaFetch.s304), func.sum(AnaFetch.new)).where(
                AnaFetch.day >= labels[0], AnaFetch.day <= labels[-1]).group_by(AnaFetch.source_key)):
            a = agg[key]
            a["runs"], a["ok"], a["s304"], a["new"] = int(runs or 0), int(ok or 0), int(s304 or 0), int(new or 0)
        rows = []
        for key, a in agg.items():
            rows.append({"key": key, "name": res.names.get(key) or key, "origin": res.origin("", key), "articles": a["n"], "independent": a["indep"], "runs": a["runs"],
                         "success_pct": round(a["ok"] * 100 / a["runs"], 1) if a["runs"] else None,
                         "not_modified_pct": round(a["s304"] * 100 / a["ok"], 1) if a["ok"] else None,
                         "image_pct": round(a["img"] * 100 / a["n"], 1) if a["n"] else None,
                         "dup_pct": round((a["n"] - a["indep"]) * 100 / a["n"], 1) if a["n"] else None, "new": a["new"], "daily": a["daily"]})
        rows.sort(key=lambda r: (-r["articles"], -r["runs"], r["key"]))
        return {"range": range_, "days": labels, "sources": rows[:200], "total_sources": len(rows)}
    return cached(("sources", range_, tuple(kinds), lang, hour_key(_now_local(now))), build)


def compare(db: Session, *, by: str = "kind", kind: str = "", lang: str = "", now: datetime | None = None) -> dict:
    by = by if by in ("kind", "channel", "origin", "lang", "source") else "kind"
    kinds, lang = parse_filter(kind, lang)

    def build() -> dict:
        res = Resolver()
        local = _now_local(now)
        a_from, a_to = day_key(local - timedelta(days=6)), day_key(local)
        b_from, b_to = day_key(local - timedelta(days=13)), day_key(local - timedelta(days=7))
        data: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])      # [今週 n, 今週 indep, 先週 n, 先週 indep]
        for t, key, n, indep in db.execute(select(AnaCount.t, AnaCount.key, func.sum(AnaCount.n), func.sum(AnaCount.indep)).where(
                AnaCount.grain == "d", AnaCount.dim == by, AnaCount.t >= b_from, AnaCount.t <= a_to, *_conds(AnaCount, kinds, lang)).group_by(AnaCount.t, AnaCount.key)):
            slot = 0 if t >= a_from else 2
            data[key][slot] += int(n or 0)
            data[key][slot + 1] += int(indep or 0)
        names = _names(res)[by]
        rows = [{"key": k, "label": names.get(k, k), "this": v[0], "prev": v[2], "this_indep": v[1], "prev_indep": v[3], "delta": v[0] - v[2],
                 "pct": round((v[0] - v[2]) * 100 / v[2]) if v[2] else None} for k, v in data.items()]
        rows.sort(key=lambda r: (-(r["this"] + r["prev"]), r["key"]))
        return {"by": by, "this_week": [a_from, a_to], "prev_week": [b_from, b_to], "rows": rows[:30],
                "total_this": sum(r["this"] for r in rows), "total_prev": sum(r["prev"] for r in rows)}
    return cached(("compare", by, tuple(kinds), lang, hour_key(_now_local(now))), build)


def filters_info() -> dict:
    res = Resolver()
    return {"kinds": [{"id": k, "label": v} for k, v in sources_mod.KIND_LABELS.items() if k != "event"], "langs": [{"id": k, "label": v} for k, v in LANG_LABELS.items()],
            "channels": [{"id": c["id"], "name": c["name"], "color": c["color"]} for c in res.channels]}
