"""ニュースの「更新・LLM・RAG」の設定（全体で 1 つ。data_dir/news/settings.json）。

2026-11-23 利用者「ニュースページ及びフィードの更新タイミング、その際に利用する LLM は個別に設定できること。このデータは基本公開情報だと思うので
OpenRouter の無料 LLM の利用を想定している。もちろんローカル LLM でも良い。検索した情報については、RAG に蓄積し…」。

- 更新: ホームのフィードの読み直しの間隔・日刊のまとめの時刻・週刊のまとめの曜日と時刻・取得しない時間帯。
  ニュースのページの取得の間隔（分）は従来どおり channels.json の interval_minutes（設定の「アーカイブ」）。
- LLM: 用途ごと（まとめ・一言要約と話題の語・記事の下ごしらえ〔概要・画像の描写・話題の語を 20 件ずつ 1 回で〕）に OpenRouter のモデルを選ぶ。
  まとめの重点（digest_focus）は利用者が書く自由記述。1 日・1 分の回数の上限（daily_limit・minute_limit）。
形は厳密に検査し、足りない項目は既定で補う。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta

from qnapnewshub import writer
from qnapnewshub.config import data_dir
from qnapnewshub.llm import DEFAULT_DAILY_LIMIT, DEFAULT_MINUTE_LIMIT, DEFAULT_MODEL, DEFAULT_GATEWAY, DEFAULT_NIGHT_FROM, DEFAULT_QUOTA, QUOTA_LABELS, default_quota
from qnapnewshub.sources import ConfigError

WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")     # 0 = 月曜（datetime.weekday と同じ）
FEED_MIN, FEED_MAX = 1, 240
ON_PRUNE = ("keep", "delete")
FOCUS_MAX = 1000       # まとめの重点（自由記述）の上限の文字数
DEFAULTS: dict = {
    "feed_refresh_minutes": 15,
    "digest_daily": {"enabled": True, "time": "06:30"},
    "digest_weekly": {"enabled": True, "weekday": 0, "time": "07:00"},
    "quiet": {"enabled": False, "start": "01:00", "end": "05:00"},
    # お休みモード（rest.py）。既定は切。休止中の動き: keep_memory / stop_all / flush_at_limit
    "rest": {"enabled": False, "start": "01:00", "end": "06:00", "mode": "keep_memory"},
    # 保存とログ（Write Coordinator・writer.py）。log_file・access_log は再起動で効く
    "storage": {"flush_minutes": 30, "max_items": 1000, "max_mb": 128, "log_file": False, "access_log": False, "compress_years": 1},
    # 本文の取り出し（bodies.py）: 重要度の高い記事から、1 日 per_day 件・1 分 per_minute 件まで
    "body": {"enabled": True, "per_day": 200, "per_minute": 6},
    # ControlDeck とのつなぎ（link.py）。allow_confidential は **つなぐとき** に読む: true でつなぐと、そのトークンは機密（confidential）の区分も読める
    # （要求のたびに confidential=true を付けたときだけ）。既定は false＝公開（public）だけ。つないだあとに変えても、つないだ相手は変わらない（つなぎ直す）
    "link": {"allow_confidential": False},
    # モデルは OpenRouter のモデル名（無料は :free で終わる）。下ごしらえは 20 件ずつ・1 日 daily_limit 回・1 分 minute_limit 回まで（llm.py）
    "llm": {"digest_model": DEFAULT_MODEL, "gist_enabled": False, "gist_model": DEFAULT_MODEL, "digest_focus": "", "prep_enabled": True,
            "prep_model": DEFAULT_MODEL, "prep_max_per_run": 100, "daily_limit": DEFAULT_DAILY_LIMIT, "minute_limit": DEFAULT_MINUTE_LIMIT,
            # 要素の取り出し（下ごしらえと同じ 1 回の呼び出しで）・次のモデル（1 番目は prep_model。空でよい）・用途ごとの割り当て・夜の後回しの仕事
            "extract_enabled": True, "fallback_models": [], "quota": dict(DEFAULT_QUOTA), "night_from": DEFAULT_NIGHT_FROM, "backfill_enabled": True,
            "gateway": dict(DEFAULT_GATEWAY)},
}
_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_lock = threading.Lock()


def _path():
    return data_dir() / "news" / "settings.json"


def _clock(value: object, label: str) -> str:
    text = str(value or "")
    if not _CLOCK.match(text):
        raise ConfigError(f"{label}は HH:MM の形です")
    return text


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key} は表です")
    return value


def _bool(table: dict, key: str, default: bool) -> bool:
    return bool(table[key]) if key in table else default


def _int(value: object, label: str, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise ConfigError(f"{label}は整数です") from None
    if not low <= number <= high:
        raise ConfigError(f"{label}は {low}〜{high} です")
    return number


def _model(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _MODEL.match(text):
        raise ConfigError(f"{label}のモデル名の形が違います")
    return text


def _models(value: object) -> list[str]:
    """次のモデルの並び（最大 4 つ。空の欄は捨て、重複は 1 つにする）。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("次のモデルは一覧です")
    out: list[str] = []
    for item in value:
        if str(item or "").strip():
            name = _model(item, "次のモデル")
            if name not in out:
                out.append(name)
    if len(out) > 4:
        raise ConfigError("次のモデルは 4 つまでです")
    return out


def _quota(value: object, daily: int) -> dict:
    """用途ごとの 1 日の割り当て。合計が 1 日の上限を超えたら断る（黙って縮めない）。無ければ既定の割合を上限に合わせる。"""
    if value is None:
        return default_quota(daily)
    if not isinstance(value, dict):
        raise ConfigError("用途ごとの割り当ては表です")
    given = dict(value)
    if "event" not in given:      # event を足す前の設定: 合計が上限に収まる範囲だけ足す（足りなければ 0。割り当てが 0 の用途も予備から借りて動く）
        given["event"] = max(0, min(DEFAULT_QUOTA["event"], daily - sum(_int(v, "割り当て", 0, 5000) for k, v in given.items() if k in DEFAULT_QUOTA)))
    out = {k: _int(given.get(k, DEFAULT_QUOTA[k]), f"{QUOTA_LABELS[k]}の割り当て", 0, 5000) for k in DEFAULT_QUOTA}
    if sum(out.values()) > daily:
        raise ConfigError(f"用途ごとの割り当ての合計（{sum(out.values())} 回）が 1 日の上限（{daily} 回）を超えています")
    return out


def gateway_url(value: object) -> str:
    """ControlDeck のゲートウェイの基準の URL（例 http://（ControlDeck の PC）:8765/api/v1/llm/v1）。http・https だけ・利用者の入力どおり（LAN の私的な IP も許す）。
    ユーザー名・パスワード・問い合わせ・# は断る。空は「未設定」。ニュースの取得の SSRF の守り（egress）とは別の道で、設定の値だけを使う。"""
    from urllib.parse import urlsplit

    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 300 or re.search(r"\s", text):
        raise ConfigError("ゲートウェイの URL の形が違います")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ConfigError("ゲートウェイの URL の形が違います") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("ゲートウェイの URL は http:// か https:// で始めてください")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("ゲートウェイの URL に、ユーザー名・パスワード・問い合わせ・# は入れられません（鍵は別の欄に入れます）")
    return text.rstrip("/") if port is None or 1 <= port <= 65535 else ""


def _gateway(value: object) -> dict:
    if value is None:
        return dict(DEFAULT_GATEWAY)
    if not isinstance(value, dict):
        raise ConfigError("ゲートウェイの設定は表です")
    d = DEFAULT_GATEWAY
    out = {"enabled": _bool(value, "enabled", d["enabled"]), "base_url": gateway_url(value.get("base_url", "")),
           "model": _model(value.get("model", d["model"]), "ゲートウェイ"), "first": _bool(value, "first", d["first"]),
           "batch": _int(value.get("batch", d["batch"]), "ゲートウェイの 1 回の件数", 1, 30),
           "timeout_seconds": _int(value.get("timeout_seconds", d["timeout_seconds"]), "ゲートウェイの時間切れ（秒）", 30, 1800),
           "minute_limit": _int(value.get("minute_limit", d["minute_limit"]), "ゲートウェイの 1 分の上限", 1, 60),
           "cooldown_minutes": _int(value.get("cooldown_minutes", d["cooldown_minutes"]), "つながらないときに飛ばす分", 1, 120)}
    if out["enabled"] and not out["base_url"]:
        raise ConfigError("ゲートウェイを使うには URL を入れてください")
    return out


def _focus(value: object) -> str:
    """まとめの重点（利用者が日本語で書く自由記述）。改行は残し、制御文字は除く。上限を超えたら黙って切らずに断る。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError("まとめの重点は文字列です")
    text = "".join(ch for ch in value.replace("\r\n", "\n").replace("\r", "\n") if ch == "\n" or (ch >= " " and ch != "\x7f")).strip()
    if len(text) > FOCUS_MAX:
        raise ConfigError(f"まとめの重点は {FOCUS_MAX} 文字までです（いま {len(text)} 文字）")
    return text


def normalize(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("設定の形が違います")
    out = json.loads(json.dumps(DEFAULTS))
    if "feed_refresh_minutes" in raw:
        try:
            minutes = int(raw["feed_refresh_minutes"])
        except (TypeError, ValueError):
            raise ConfigError("フィードの更新の間隔は整数（分）です") from None
        if not FEED_MIN <= minutes <= FEED_MAX:
            raise ConfigError(f"フィードの更新の間隔は {FEED_MIN}〜{FEED_MAX} 分です")
        out["feed_refresh_minutes"] = minutes
    daily = _table(raw, "digest_daily")
    out["digest_daily"] = {"enabled": _bool(daily, "enabled", True), "time": _clock(daily.get("time", "06:30"), "日刊の時刻")}
    weekly = _table(raw, "digest_weekly")
    try:
        weekday = int(weekly.get("weekday", 0))
    except (TypeError, ValueError):
        raise ConfigError("週刊の曜日は 0（月）〜6（日）です") from None
    if not 0 <= weekday <= 6:
        raise ConfigError("週刊の曜日は 0（月）〜6（日）です")
    out["digest_weekly"] = {"enabled": _bool(weekly, "enabled", True), "weekday": weekday, "time": _clock(weekly.get("time", "07:00"), "週刊の時刻")}
    quiet = _table(raw, "quiet")
    start, end = _clock(quiet.get("start", "01:00"), "取得しない時間帯の開始"), _clock(quiet.get("end", "05:00"), "取得しない時間帯の終わり")
    out["quiet"] = {"enabled": _bool(quiet, "enabled", False), "start": start, "end": end}
    rest_in = _table(raw, "rest")
    mode = str(rest_in.get("mode", "keep_memory"))
    if mode not in ("keep_memory", "stop_all", "flush_at_limit"):
        raise ConfigError("お休みモードの動きは keep_memory・stop_all・flush_at_limit のどれかです")
    out["rest"] = {"enabled": _bool(rest_in, "enabled", False), "start": _clock(rest_in.get("start", "01:00"), "お休みの開始"),
                   "end": _clock(rest_in.get("end", "06:00"), "お休みの終わり"), "mode": mode}
    st = _table(raw, "storage")
    out["storage"] = {"flush_minutes": _int(st.get("flush_minutes", 30), "まとめ書きの間隔（分）", 1, 1440),
                      "max_items": _int(st.get("max_items", 1000), "まとめ書きの件数の上限", 10, 100000),
                      "max_mb": _int(st.get("max_mb", 128), "まとめ書きの容量の上限（MB）", 1, 2048),
                      "log_file": _bool(st, "log_file", False), "access_log": _bool(st, "access_log", False),
                      "compress_years": _int(st.get("compress_years", 1), "古い記事の抜粋を圧縮する年数（0 は圧縮しない）", 0, 30)}
    body = _table(raw, "body")
    out["body"] = {"enabled": _bool(body, "enabled", True), "per_day": _int(body.get("per_day", 200), "本文の取り出しの 1 日の件数", 0, 5000),
                   "per_minute": _int(body.get("per_minute", 6), "本文の取り出しの 1 分の件数", 1, 60)}
    lk = _table(raw, "link")
    out["link"] = {"allow_confidential": _bool(lk, "allow_confidential", False)}
    llm = _table(raw, "llm")
    out["llm"] = {"digest_model": _model(llm.get("digest_model", DEFAULT_MODEL), "まとめ"), "gist_enabled": _bool(llm, "gist_enabled", False),
                  "gist_model": _model(llm.get("gist_model", DEFAULT_MODEL), "一言要約"), "digest_focus": _focus(llm.get("digest_focus", "")),
                  "prep_enabled": _bool(llm, "prep_enabled", True), "prep_model": _model(llm.get("prep_model", DEFAULT_MODEL), "下ごしらえ"),
                  "prep_max_per_run": _int(llm.get("prep_max_per_run", 100), "下ごしらえの 1 回の件数", 1, 200),
                  "daily_limit": _int(llm.get("daily_limit", DEFAULT_DAILY_LIMIT), "1 日の回数の上限", 1, 5000),
                  "minute_limit": _int(llm.get("minute_limit", DEFAULT_MINUTE_LIMIT), "1 分の回数の上限", 1, 100),
                  "extract_enabled": _bool(llm, "extract_enabled", True), "fallback_models": _models(llm.get("fallback_models", [])),
                  "quota": _quota(llm.get("quota"), _int(llm.get("daily_limit", DEFAULT_DAILY_LIMIT), "1 日の回数の上限", 1, 5000)),
                  "night_from": _int(llm.get("night_from", DEFAULT_NIGHT_FROM), "夜の始まりの時", 0, 23), "backfill_enabled": _bool(llm, "backfill_enabled", True),
                  "gateway": _gateway(llm.get("gateway"))}
    return out


def load() -> dict:
    try:
        return normalize(json.loads(_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return normalize({})


def save(raw: object) -> dict:
    cfg = normalize(raw)
    with _lock:
        writer.write_file(_path(), json.dumps(cfg, ensure_ascii=False, indent=1), name="settings.save")
    from qnapnewshub import rest

    rest.invalidate()
    return cfg


# ---- 時刻の判定 ----

def _minutes(clock: str) -> int:
    return int(clock[:2]) * 60 + int(clock[3:])


def in_quiet(cfg: dict, now: datetime) -> bool:
    """取得しない時間帯の中か。開始は含み、終わりは含まない。開始 > 終了は日をまたぐ（22:00〜06:00）。"""
    quiet = cfg["quiet"]
    if not quiet["enabled"]:
        return False
    start, end, current = _minutes(quiet["start"]), _minutes(quiet["end"]), now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= current < end if start < end else (current >= start or current < end)


def due_digests(cfg: dict, now: datetime) -> list[tuple[str, str]]:
    """いま作るべきまとめ [(period, day)]。直近の予定の時刻を過ぎていれば（寝ていた後でも）対象。作ったかの判定は呼ぶ側。
    日刊は今日の時刻、週刊は直近の（曜日, 時刻）。day はその予定の日付（YYYY-MM-DD）。"""
    out: list[tuple[str, str]] = []
    daily = cfg["digest_daily"]
    if daily["enabled"] and now.hour * 60 + now.minute >= _minutes(daily["time"]):
        out.append(("daily", now.strftime("%Y-%m-%d")))
    weekly = cfg["digest_weekly"]
    if weekly["enabled"]:
        day = now.date() - timedelta(days=(now.weekday() - weekly["weekday"]) % 7)
        if day == now.date() and now.hour * 60 + now.minute < _minutes(weekly["time"]):
            day -= timedelta(days=7)
        out.append(("weekly", day.isoformat()))
    return out
