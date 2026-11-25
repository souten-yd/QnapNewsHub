"""収集の connector の枠組み（v0.7 前半。ControlDeck の PLAN-integrated-knowledge 8 章・ARCHITECTURE 10 章の「実装と違った点」）。

RSS は収集の手段の 1 つ。取得の種類（feed・api・historical・change・discovery・market）ごとに、次を持つ:
- **取得の頻度**（interval_minutes）・**1 日の件数**（daily_items）・**1 日の通信量**（daily_mb）。超えたら止めて診断に出す（その日の終わり＝UTC の 0 時に戻る）。
- **鍵（任意）**: secretbox で暗号化（名前 `connector.<id>.key`）。応答・ログ・診断には出さない。無ければ低いレートで取る。
- **利用条件の記録**（connector_terms.py。ライセンス・保存と再配布の可否・レート・公式の利用規約の URL・確かめた日）。**未確認（unconfirmed）の connector は既定で切**で、入れるには利用者の明示（terms_ack）が要る。
- 入・切（enabled）と、connector ごとの設定（settings）。

**既存の取得の載せ替え**: RSS・GitHub の Release・arXiv・HN・HF・Steam は書き直さず、`sources.build_sources` のソースが `for_source` でどの connector に属すかを引かれ、
`service._fetch_one` が取得の前に `gate`（入・切・予算）を通し、後で `note_usage`（件数・通信量）を数える。
新しい connector（発見・API・Sitemap・変化の検出）は `conn_impl.py` の関数を `run_one` が呼び、結果を **Write Coordinator のまとめ書き**（`writer.stage_connector` → `docstore.apply`）へ積む。
お休みモード・取得しない時間帯では動かない。1 つの失敗で他を止めない。

状態（その日の使用・最後の結果・change の ETag と hash）は news/connector-state.json（メモリが正。まとめ書きのとき書く）。設定は news/connectors.json（利用者の操作で即時に書く）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from qnapnewshub import net, secretbox, writer
from qnapnewshub.config import data_dir
from qnapnewshub.connector_terms import TERMS

logger = logging.getLogger("qnapnewshub.connectors")

KINDS = {"feed": "フィード", "api": "API", "historical": "過去・一覧", "change": "変化の検出", "discovery": "発見", "market": "数値（市場）", "stats": "数値（統計）"}
RUN_TIMEOUT = 150
MAX_ITEMS_LIMIT = 100_000
MAX_MB_LIMIT = 4096
INTERVAL_MIN, INTERVAL_MAX = 5, 10080
MAX_LIST = 50
DEFAULT_PROMOTION = {"channel": True, "hot": True, "watch": True, "surge": True, "derivative": True, "surge_stars": 100}


@dataclass(frozen=True)
class Spec:
    id: str
    kind: str
    label: str
    desc: str
    interval: int = 1440
    daily_items: int = 500
    daily_mb: int = 20
    adapted: bool = False          # 既存の取得を載せ替えたもの（sources.py の取得。ここでは入・切・予算・使用の数えだけ）
    scheduled: bool = True         # False は、利用者の操作で 1 回ずつ使う道具（HN の検索）
    settings: dict = field(default_factory=dict)
    has_key: bool = False          # 鍵（任意）を受け付ける
    run: str = ""                  # conn_impl の関数名（adapted は空）
    min_gap: float = 1.0           # 同じ connector の通信の間隔（秒）
    key_required: bool = False     # 鍵が無いと動かない（鍵があるときだけ入にできる。既定は切）


SPECS: dict[str, Spec] = {s.id: s for s in [
    Spec("feed", "feed", "RSS・一覧・Steam・フォーラムなど", "既存の取得（RSS/Atom・一覧の巡回・Steam・Discourse・Codeberg・検索の RSS）。取得先ごとの間隔は取得先の管理で。", adapted=True, daily_items=20000, daily_mb=500),
    Spec("github", "feed", "GitHub の Release", "監視のリポジトリの Release の差分（既存の取得を載せ替え）。", adapted=True, daily_items=2000, daily_mb=50, has_key=True),
    Spec("huggingface", "feed", "Hugging Face の話題の GGUF", "話題の GGUF のモデル（既存の取得を載せ替え）。", adapted=True, daily_items=2000, daily_mb=50, has_key=True),
    Spec("arxiv", "feed", "arXiv", "arXiv の RSS（既存）。論文の文書（L1・arXiv ID で同定）も作る。", adapted=True, daily_items=3000, daily_mb=50),
    Spec("hn", "feed", "Hacker News", "トップページとチャンネルの語の検索（既存の取得を載せ替え）。", adapted=True, daily_items=3000, daily_mb=50),
    Spec("github_discover", "discovery", "GitHub の発見・スターの急増", "検索で新しいリポジトリを見つけ（直近の日数・スター数・トピック）、監視のリポジトリのスター数の日ごとの差を Metric に。",
         interval=1440, daily_items=300, daily_mb=10, has_key=True, run="github_discover", min_gap=7.0,
         settings={"topics": ["llm", "inference", "rocm", "gguf"], "window_days": 7, "min_stars": 50, "per_topic": 15, "watch_stars": True}),
    Spec("hf_models", "discovery", "Hugging Face の新しいモデル・派生版", "新しいモデル（作者・タグ・pipeline で絞る）と、監視のモデルの派生版（base_model の一致・量子化の種類）。",
         interval=720, daily_items=300, daily_mb=10, has_key=True, run="hf_models", min_gap=1.0,
         settings={"authors": [], "tags": ["gguf"], "pipelines": [], "watch_models": [], "limit": 30, "relations": ["quantized", "finetune", "adapter", "merge"]}),
    Spec("openalex", "api", "OpenAlex（論文の書誌・引用数）", "分野で絞った新しい論文の書誌と引用数。論文の文書（L1・DOI／arXiv ID で同定）に。",
         interval=1440, daily_items=300, daily_mb=10, has_key=True, run="openalex", min_gap=0.3,
         settings={"filter": "primary_topic.field.id:17", "search": "", "days": 7, "per_page": 25}),
    Spec("crossref", "api", "Crossref（DOI の解決）", "DOI だけ分かっている論文（L0）の書誌を 1 件ずつ解決して L1 へ。",
         interval=360, daily_items=200, daily_mb=5, run="crossref", min_gap=0.4, settings={"batch": 20}),
    Spec("semanticscholar", "discovery", "Semantic Scholar（引用・関連）", "本文まで進んだ論文（L2 以上）の引用・参考文献を 1〜2 段だけ（予算つき）。辺は文書どうしの引用（cites）。",
         interval=720, daily_items=300, daily_mb=5, has_key=True, run="semanticscholar", min_gap=1.5,
         settings={"depth": 1, "seeds": 3, "per_paper": 15, "second_level": 3}),
    Spec("hn_search", "api", "HN Algolia の検索（道具）", "問いに応じて過去の議論を 1 回ずつ検索する道具（常に全部は取らない）。結果は L0 で保存できる。",
         interval=1440, daily_items=100, daily_mb=2, scheduled=False, run="hn_search", min_gap=1.0, settings={"per_query": 20}),
    Spec("sitemap", "historical", "Sitemap（記事の URL）", "指定したサイトの sitemap から記事の URL を L0（URL・題・日付）で。robots.txt を守る。",
         interval=1440, daily_items=1000, daily_mb=20, run="sitemap", min_gap=1.0, settings={"sites": [], "max_urls": 200}),
    Spec("change", "change", "ページの変化の検出", "指定したページの変化を ETag・hash で見つける（本文は保存しない）。robots.txt を守る。",
         interval=360, daily_items=200, daily_mb=20, run="change", min_gap=1.0, settings={"pages": []}),
    # ---- v0.8 ----
    Spec("boj", "stats", "日本銀行の時系列統計", "指定した系列（DB:系列コード）の数値を Metric の時系列に（単位・時点・系列名つき）。鍵は要りません。",
         interval=1440, daily_items=600, daily_mb=5, run="boj", min_gap=3.0, settings={"series": ["IR01:MADR1Z@D", "FM01:STRDCLUCON"], "last_points": 24}),
    Spec("estat", "stats", "e-Stat（政府統計の統計表）", "統計表 ID を指定した統計表の数値を Metric の時系列に。appId（利用登録で取得）が要る。",
         interval=1440, daily_items=600, daily_mb=10, has_key=True, key_required=True, run="estat", min_gap=2.0, settings={"tables": [], "limit": 200}),
    Spec("kokkai", "api", "国会会議録（発言）", "語で検索した国会の発言（話者・院・会議・日付つき）を record の文書（L1）に。鍵は要りません。",
         interval=1440, daily_items=300, daily_mb=10, run="kokkai", min_gap=4.0, settings={"terms": ["人工知能"], "days": 30, "per_term": 20, "house": ""}),
    Spec("egov_law", "api", "e-Gov 法令（版・改正の履歴）", "指定した法令の版・改正の履歴を law の文書に（公布日・施行日・段〔提案・成立・施行〕）。鍵は要りません。",
         interval=1440, daily_items=200, daily_mb=5, run="egov_law", min_gap=2.0, settings={"laws": ["令和七年法律第五十三号"], "max_revisions": 30}),
    Spec("edinet", "api", "EDINET（提出書類の知らせ）", "有価証券報告書などの提出の知らせを filing の文書に。鍵（Subscription-Key）が要る。",
         interval=720, daily_items=300, daily_mb=10, has_key=True, key_required=True, run="edinet", min_gap=2.0, settings={"days": 1, "sec_codes": [], "limit": 100}),
    Spec("defillama", "market", "DefiLlama（TVL・ステーブルコイン・RWA）", "チェーンの TVL・ステーブルコインの供給量・RWA（category が RWA のもの）の公開の数値を Metric に。無料の範囲だけ。鍵は要りません。",
         interval=1440, daily_items=300, daily_mb=30, run="defillama", min_gap=3.0, settings={"chains": ["Ethereum"], "protocols": [], "stablecoins_top": 10, "rwa_top": 10}),
    Spec("coingecko", "market", "CoinGecko（価格）", "指定した銘柄の価格を Metric に。無料の Demo の鍵があるときだけ動く。",
         interval=1440, daily_items=100, daily_mb=2, has_key=True, key_required=True, run="coingecko", min_gap=3.0, settings={"coins": ["bitcoin", "ethereum"], "vs": ["usd", "jpy"]}),
    Spec("gdelt", "discovery", "GDELT（語の検索）", "語で検索した報道を L0 の文書に（機械の抽出で「未確認」の印つき）。語の検索だけ。鍵は要りません。",
         interval=1440, daily_items=200, daily_mb=5, run="gdelt", min_gap=6.0, settings={"queries": ["ROCm"], "timespan": "1d", "max_records": 50}),
    Spec("pypi", "feed", "PyPI のリリース", "監視するパッケージの新しい版を release の文書に。",
         interval=720, daily_items=100, daily_mb=10, run="pypi", min_gap=1.0, settings={"packages": ["transformers", "vllm"], "per_package": 3}),
    Spec("npm", "feed", "npm のリリース", "監視するパッケージの版（dist-tags）を release の文書に。",
         interval=720, daily_items=100, daily_mb=10, run="npm", min_gap=1.0, settings={"packages": ["vite"]}),
    Spec("osv", "feed", "OSV（脆弱性）", "監視するパッケージ（エコシステム:名前）の脆弱性を advisory の文書に。",
         interval=720, daily_items=200, daily_mb=10, run="osv", min_gap=1.0, settings={"packages": ["PyPI:transformers"], "per_package": 20}),
]}

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_TOPIC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,49}$")
_FILTER = re.compile(r"^[A-Za-z0-9_.:,|<>=!\-/]{1,200}$")
_SEARCH = re.compile(r"^[^\x00-\x1f<>]{0,100}$")
_PIPE = re.compile(r"^[a-z0-9\-]{1,50}$")
_AUTHOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_REL = ("quantized", "finetune", "adapter", "merge")
_BOJ_SERIES = re.compile(r"^[A-Z][A-Z0-9_]{0,19}:[A-Za-z0-9_@.\-]{1,60}$")
_ESTAT_TABLE = re.compile(r"^\d{6,12}$")
_LAW_REF = re.compile(r"^[^\x00-\x1f/?#%<>\\\"'`;]{3,80}$")
_FREE_TERM = re.compile(r"^[^\x00-\x1f<>]{1,100}$")
_SEC_CODE = re.compile(r"^\d{4,5}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$")
_NPM = re.compile(r"^(?:@[a-z0-9][a-z0-9._\-]{0,99}/)?[a-z0-9][a-z0-9._\-]{0,99}$")
_OSV_PKG = re.compile(r"^(PyPI|npm|Go|crates\.io|Maven|RubyGems|NuGet|Packagist):[A-Za-z0-9@][A-Za-z0-9@._:/\-]{0,150}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._\-]{0,79}$")
_CHAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,40}$")
_VS = re.compile(r"^[a-z]{2,10}$")
_TIMESPAN = re.compile(r"^\d{1,3}(min|h|d|w|m)$")
_HOUSES = ("", "衆議院", "参議院", "両院", "両院協議会")


class ConnectorError(ValueError):
    pass


# ---- 設定 ----

def _path() -> Path:
    return data_dir() / "news" / "connectors.json"


def _site_list(value: object, label: str, key: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 20:
        raise ConnectorError(f"{label}は 20 件までの一覧です")
    out = []
    for row in value:
        if not isinstance(row, dict):
            raise ConnectorError(f"{label}の各項目は {{name, url}} です")
        from qnapnewshub import sources

        try:
            url = sources.https_url(row.get("url"))
        except sources.ConfigError as exc:
            raise ConnectorError(str(exc)) from exc
        out.append({"name": (str(row.get("name") or "").strip() or urlsplit(url).hostname or "サイト")[:60], "url": url})
    return out


def _strs(value: object, label: str, check: re.Pattern, limit: int = MAX_LIST) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ConnectorError(f"{label}は {limit} 件までの一覧です")
    out = []
    for v in value:
        text = str(v).strip()
        if not check.match(text):
            raise ConnectorError(f"{label}に使えない値があります: {text[:40]}")
        out.append(text)
    return list(dict.fromkeys(out))


def _int(value: object, label: str, low: int, high: int) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConnectorError(f"{label}は整数です") from None
    if not low <= n <= high:
        raise ConnectorError(f"{label}は {low}〜{high} です")
    return n


V08_IDS = ("boj", "estat", "kokkai", "egov_law", "edinet", "defillama", "coingecko", "gdelt", "pypi", "npm", "osv")


def _validate_v08(cid: str, k: str, v: object):
    """v0.8 の connector の設定 1 項目の検査（入力は URL・検索語・問いの本文に入るので形を絞る）。"""
    if cid == "boj":
        return _strs(v, "系列", _BOJ_SERIES, 30) if k == "series" else _int(v, "取り込む点の数", 1, 400)
    if cid == "estat":
        return _strs(v, "統計表 ID", _ESTAT_TABLE, 20) if k == "tables" else _int(v, "1 回の件数", 1, 2000)
    if cid == "kokkai":
        if k == "terms":
            return _strs(v, "語", _FREE_TERM, 10)
        if k == "house":
            if v not in _HOUSES:
                raise ConnectorError("院は 衆議院・参議院・両院・両院協議会 のどれかです（空なら全部）")
            return v
        return _int(v, "日数" if k == "days" else "1 語の件数", 1, 3650 if k == "days" else 100)
    if cid == "egov_law":
        return _strs(v, "法令（法令 ID か法令番号）", _LAW_REF, 20) if k == "laws" else _int(v, "取り込む版の数", 1, 200)
    if cid == "edinet":
        return _strs(v, "証券コード", _SEC_CODE, 50) if k == "sec_codes" else _int(v, "日数" if k == "days" else "件数", 1, 7 if k == "days" else 1000)
    if cid == "defillama":
        if k == "chains":
            return _strs(v, "チェーン", _CHAIN, 20)
        if k == "protocols":
            return _strs(v, "プロトコル（slug）", _SLUG, 30)
        return _int(v, "件数", 0, 100)
    if cid == "coingecko":
        return _strs(v, "銘柄の id" if k == "coins" else "通貨", _SLUG if k == "coins" else _VS, 30 if k == "coins" else 5)
    if cid == "gdelt":
        if k == "queries":
            return _strs(v, "検索語", _FREE_TERM, 10)
        if k == "timespan":
            if not _TIMESPAN.match(str(v)):
                raise ConnectorError("期間の形が違います（例: 1d・6h・2w）")
            return str(v)
        return _int(v, "1 問いの件数", 1, 250)
    if cid == "pypi":
        return _strs(v, "パッケージ", _NAME, 30) if k == "packages" else _int(v, "1 パッケージの版の数", 1, 20)
    if cid == "npm":
        return _strs(v, "パッケージ", _NPM, 30)
    if cid == "osv":
        return _strs(v, "パッケージ（エコシステム:名前）", _OSV_PKG, 30) if k == "packages" else _int(v, "1 パッケージの件数", 1, 100)
    raise ConnectorError("知らない設定です")


def normalize_settings(cid: str, raw: object) -> dict:
    """connector ごとの設定を検査して整える（足りない項目は既定。知らない項目は断る）。"""
    spec = SPECS[cid]
    base = json.loads(json.dumps(spec.settings))
    if raw in (None, {}):
        return base
    if not isinstance(raw, dict):
        raise ConnectorError("設定の形が違います")
    unknown = set(raw) - set(base)
    if unknown:
        raise ConnectorError(f"知らない設定です: {sorted(unknown)[0][:30]}")
    out = dict(base)
    for k, v in raw.items():
        if cid in V08_IDS:
            out[k] = _validate_v08(cid, k, v)
        elif cid == "github_discover":
            out[k] = (_strs(v, "トピック", _TOPIC, 12) if k == "topics" else _int(v, "直近の日数", 1, 90) if k == "window_days" else _int(v, "最低のスター数", 0, 1_000_000) if k == "min_stars"
                      else _int(v, "トピックごとの件数", 1, 50) if k == "per_topic" else bool(v))
        elif cid == "hf_models":
            if k == "authors":
                out[k] = _strs(v, "作者", _AUTHOR, 20)
            elif k == "tags":
                out[k] = _strs(v, "タグ", re.compile(r"^[A-Za-z0-9_.:+\-/]{1,80}$"), 10)
            elif k == "pipelines":
                out[k] = _strs(v, "pipeline", _PIPE, 10)
            elif k == "watch_models":
                out[k] = _strs(v, "監視のモデル", _REPO, 10)
            elif k == "relations":
                out[k] = [r for r in _strs(v, "派生の種類", re.compile(r"^[a-z]{1,20}$"), 8) if r in _REL]
            else:
                out[k] = _int(v, "件数", 1, 100)
        elif cid == "openalex":
            if k == "filter":
                if not _FILTER.match(str(v)):
                    raise ConnectorError("filter の形が違います")
                out[k] = str(v)
            elif k == "search":
                if not _SEARCH.match(str(v)):
                    raise ConnectorError("検索語に使えない文字があります")
                out[k] = str(v)
            else:
                out[k] = _int(v, "値", 1, 90 if k == "days" else 100)
        elif cid in ("crossref", "semanticscholar", "hn_search"):
            out[k] = _int(v, k, 1, 5 if k in ("depth", "second_level") else 100)
            if k == "depth" and out[k] > 2:
                raise ConnectorError("引用をたどる段は 1〜2 です")
        elif cid == "sitemap":
            out[k] = _site_list(v, "サイト", k) if k == "sites" else _int(v, "1 サイトの件数", 1, 2000)
        elif cid == "change":
            out[k] = _site_list(v, "ページ", k)
    return out


def default_enabled(spec: Spec) -> bool:
    """既存の取得を載せ替えたものは入（今までの動きを変えない）。新しい connector は切（利用者が入れる）。"""
    return spec.adapted


def normalize(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ConnectorError("設定の形が違います")
    rows = raw.get("connectors", {})
    if not isinstance(rows, dict):
        raise ConnectorError("connectors は表です")
    out: dict = {"connectors": {}, "promotion": dict(DEFAULT_PROMOTION)}
    for cid, spec in SPECS.items():
        row = rows.get(cid) if isinstance(rows.get(cid), dict) else {}
        out["connectors"][cid] = {
            "enabled": bool(row.get("enabled", default_enabled(spec))),
            "interval_minutes": _int(row.get("interval_minutes", spec.interval), "取得の間隔（分）", INTERVAL_MIN, INTERVAL_MAX),
            "daily_items": _int(row.get("daily_items", spec.daily_items), "1 日の件数", 0, MAX_ITEMS_LIMIT),
            "daily_mb": _int(row.get("daily_mb", spec.daily_mb), "1 日の通信量（MB）", 0, MAX_MB_LIMIT),
            "terms_ack": bool(row.get("terms_ack", False)),
            "settings": normalize_settings(cid, row.get("settings")),
        }
    promo = raw.get("promotion", {})
    if isinstance(promo, dict):
        for k, default in DEFAULT_PROMOTION.items():
            if k in promo:
                out["promotion"][k] = _int(promo[k], "スター数", 1, 1_000_000) if k == "surge_stars" else bool(promo[k])
    return out


_cfg_cache: dict[str, tuple[float, dict]] = {}


def load() -> dict:
    """設定（無い・壊れていれば既定）。短く覚える（取得のたびに読むので）。save が捨てる。"""
    key = str(_path())
    hit = _cfg_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < 2.0:
        return hit[1]
    try:
        cfg = normalize(json.loads(_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):      # ConnectorError も ValueError
        cfg = normalize({})
    _cfg_cache[key] = (now, cfg)
    return cfg


def save(cfg: object, *, user: bool = True) -> dict:
    out = normalize(cfg)
    writer.write_file(_path(), json.dumps(out, ensure_ascii=False, indent=1), name="connectors.save", mode=0o644, user=user)
    _cfg_cache.clear()
    return out


def promotion_config() -> dict:
    return dict(load()["promotion"])


def conf(cid: str) -> dict:
    return load()["connectors"][cid]


def watched_models() -> list[str]:
    return list(conf("hf_models")["settings"].get("watch_models", []))


def watched_terms() -> list[str]:
    """利用者が足した監視の語（いまは Release の監視のリポジトリと、監視のモデルだけ。語の追加は次の段）。"""
    return []


# ---- 利用条件・入・切 ----

def terms_of(cid: str) -> dict:
    return TERMS[cid]


KEY_NEEDED = "鍵が要ります（取得先の管理 → connector で鍵を入れてください）"


def needs_ack(cid: str) -> bool:
    """利用条件が未確認で、既存の取得でもない（新しく増える取得）。入れるには利用者の明示が要る。"""
    return TERMS[cid]["status"] == "unconfirmed" and not SPECS[cid].adapted


def set_enabled(cid: str, enabled: bool, terms_ack: bool = False) -> dict:
    if cid not in SPECS:
        raise ConnectorError("知らない connector です")
    cfg = load()
    row = dict(cfg["connectors"][cid])
    if enabled and SPECS[cid].key_required and not has_key(cid):
        raise ConnectorError(KEY_NEEDED)
    if enabled and needs_ack(cid) and not (terms_ack or row.get("terms_ack")):
        raise ConnectorError("利用条件が未確認の connector です。確かめたうえで、未確認と分かって入れる場合は確認のチェックを入れてください")
    row["enabled"] = enabled
    if enabled and terms_ack:
        row["terms_ack"] = True
    cfg["connectors"][cid] = row
    return save(cfg)


# ---- 状態（その日の使用・最後の結果） ----

_mem: dict[str, dict[str, dict]] = {}
_state_lock = None


def _spath() -> Path:
    return data_dir() / "news" / "connector-state.json"


def _states() -> dict[str, dict]:
    key = str(_spath())
    found = _mem.get(key)
    if found is None:
        try:
            raw = json.loads(_spath().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        found = _mem[key] = {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    return found


def reset_memory() -> None:
    _mem.clear()
    _cfg_cache.clear()


def _stage_state() -> None:
    snapshot = json.dumps(_states(), ensure_ascii=False)
    path = _spath()

    def do() -> None:
        writer.write_file(path, snapshot, name="connector.state", user=False)

    writer.get().stage_state("connector-state", do)


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def state(cid: str) -> dict:
    return _states().setdefault(cid, {})


def usage(cid: str) -> dict:
    """その日の使用 {items, bytes, requests}。日が変わっていれば 0（予算の停止も解く）。"""
    st = state(cid)
    u = st.get("usage") or {}
    if u.get("day") != today():
        u = {"day": today(), "items": 0, "bytes": 0, "requests": 0}
        st["usage"] = u
        st.pop("budget_stop", None)
    return u


def budget_left(cid: str) -> dict:
    c, u = conf(cid), usage(cid)
    return {"items": max(0, c["daily_items"] - u["items"]), "bytes": max(0, c["daily_mb"] * 1024 * 1024 - u["bytes"])}


def _budget_reason(cid: str) -> str:
    c, u = conf(cid), usage(cid)
    if u["items"] >= c["daily_items"]:
        return f"1 日の件数の予算（{c['daily_items']} 件）に達したため止めています"
    if u["bytes"] >= c["daily_mb"] * 1024 * 1024:
        return f"1 日の通信量の予算（{c['daily_mb']} MB）に達したため止めています"
    return ""


def gate(cid: str, *, manual: bool = False) -> tuple[bool, str]:
    """この connector がいま取ってよいか（入・切・利用条件・予算）。取れないときの理由は利用者に見せてよい文。"""
    from qnapnewshub import rest

    c = conf(cid)
    if not c["enabled"]:
        return False, "connector が切になっています（設定 → connector）"
    if needs_ack(cid) and not c["terms_ack"]:
        return False, "利用条件が未確認のため取りません"
    if SPECS[cid].key_required and not has_key(cid):
        return False, KEY_NEEDED
    if rest.blocks_fetch():
        return False, "お休みモードのため取りません"
    why = _budget_reason(cid)
    if why:
        st = state(cid)
        if not st.get("budget_stop"):
            st["budget_stop"] = {"at": _iso_now(), "reason": why}
            _stage_state()
        return False, why
    return True, ""


def note_usage(cid: str, *, items: int = 0, nbytes: int = 0, requests: int = 0) -> dict:
    u = usage(cid)
    u["items"] += max(0, items)
    u["bytes"] += max(0, nbytes)
    u["requests"] += max(0, requests)
    _stage_state()
    return u


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_result(cid: str, *, ok: bool, count: int = 0, error: str = "", skipped: str = "", ms: int = 0, extra: dict | None = None) -> None:
    st = state(cid)
    st["last"] = {"at": _iso_now(), "ok": ok, "count": count, "error": error[:300], "skipped": skipped[:300], "ms": ms, **(extra or {})}
    if ok and not skipped:
        st["last_ok_at"] = st["last"]["at"]
    _stage_state()


# ---- 既存のソースを connector へ ----

def for_source(source_key: str) -> str:
    """取得先の source_key が属する connector の id。"""
    if source_key.startswith("github:"):
        return "github"
    if source_key == "hf":
        return "huggingface"
    if source_key == "hn" or source_key.endswith(":hn"):
        return "hn"
    if source_key.startswith("rss:"):
        host = (urlsplit(source_key[4:]).hostname or "").lower()
        if host == "github.com":
            return "github"
        if host == "rss.arxiv.org" or host == "export.arxiv.org":
            return "arxiv"
    return "feed"


def begin_count() -> list[int]:
    """この取得の受信量を数える入れ物を、いまの文脈に入れる（net._read が足す）。"""
    box = [0]
    net.BYTES.set(box)
    return box


def auth_headers(cid: str) -> dict:
    """鍵（任意）があれば認証のヘッダー。無ければ空。鍵の値はここから外へ出さない。"""
    try:
        key = secretbox.get_secret(f"connector.{cid}.key") if cid in SPECS and SPECS[cid].has_key else None
    except secretbox.SecretError:
        key = None
    if not key:
        return {}
    if cid == "semanticscholar":
        return {"x-api-key": key}
    if cid == "coingecko":
        return {"x-cg-demo-api-key": key}
    if cid in ("estat", "edinet"):
        return {}      # 鍵は query で渡す（key_param）
    return {"Authorization": f"Bearer {key}"}


def key_param(cid: str) -> dict:
    """鍵を query で渡す connector（OpenAlex）。"""
    try:
        key = secretbox.get_secret(f"connector.{cid}.key") if SPECS[cid].has_key else None
    except secretbox.SecretError:
        key = None
    if not key:
        return {}
    return {"api_key": key} if cid == "openalex" else {"appId": key} if cid == "estat" else {"Subscription-Key": key} if cid == "edinet" else {}


def has_key(cid: str) -> bool:
    try:
        return SPECS[cid].has_key and secretbox.has_secret(f"connector.{cid}.key")
    except secretbox.SecretError:
        return False


def set_key(cid: str, value: str) -> None:
    if cid not in SPECS or not SPECS[cid].has_key:
        raise ConnectorError("この connector は鍵を使いません")
    value = value.strip()
    if not 8 <= len(value) <= 300 or re.search(r"\s", value):
        raise ConnectorError("鍵の形が違います")
    secretbox.set_secret(f"connector.{cid}.key", value)


def delete_key(cid: str) -> bool:
    return secretbox.delete_secret(f"connector.{cid}.key") if cid in SPECS else False


# ---- 実行（新しい connector） ----

class BudgetStop(Exception):
    """途中で予算の上限に達した（取れた分は保存する）。"""


@dataclass
class Ctx:
    """connector の取得 1 回の道具。通信は必ずここ（予算を数える・間をあける・鍵は自動で付く・内部のアドレスへは行かない）。"""

    cid: str
    settings: dict
    manual: bool = False
    requests: int = 0
    nbytes: int = 0
    notes: list[str] = field(default_factory=list)
    partial: "Result | None" = None
    _last: float = 0.0

    def begin(self) -> "Result":
        """結果の入れ物（途中で予算・失敗で止まっても、ここまでの分は保存する）。"""
        self.partial = Result()
        return self.partial

    def over_budget(self) -> bool:
        left = budget_left(self.cid)
        return left["bytes"] - self.nbytes <= 0

    async def get(self, url: str, *, params: dict | None = None, headers: dict | None = None, accept: str = "application/json", max_bytes: int = net.MAX_BYTES,
                  with_key: bool = True) -> tuple[int, dict, bytes]:
        if self.over_budget():
            raise BudgetStop()
        gap = SPECS[self.cid].min_gap - (time.monotonic() - self._last)
        if gap > 0 and self._last:
            await _sleep(gap)
        hdr = dict(headers or {})
        par = dict(params or {})
        if with_key:
            hdr.update(auth_headers(self.cid))
            par.update(key_param(self.cid))
        self._last = time.monotonic()
        self.requests += 1
        status, head, body = await _http(url, params=par or None, headers=hdr or None, accept=accept, max_bytes=max_bytes)
        self.nbytes += len(body)
        return status, head, body

    async def post_json(self, url: str, payload: dict, *, max_bytes: int = net.MAX_BYTES):
        """JSON の POST（OSV の問い）。予算・間隔・内部のアドレスの拒否は get と同じ。"""
        if self.over_budget():
            raise BudgetStop()
        gap = SPECS[self.cid].min_gap - (time.monotonic() - self._last)
        if gap > 0 and self._last:
            await _sleep(gap)
        self._last = time.monotonic()
        self.requests += 1
        status, head, body = await _http_post(url, payload=payload, headers=auth_headers(self.cid) or None, accept="application/json", max_bytes=max_bytes)
        self.nbytes += len(body)
        if status >= 400:
            raise net.HttpError(status, net.parse_retry_after(head.get("retry-after")))
        try:
            return json.loads(body)
        except ValueError as exc:
            raise net.NetError("JSON として読めません") from exc

    async def json(self, url: str, **kwargs):
        status, head, body = await self.get(url, **kwargs)
        if status >= 400:
            raise net.HttpError(status, net.parse_retry_after(head.get("retry-after")))
        try:
            return json.loads(body)
        except ValueError as exc:
            raise net.NetError("JSON として読めません") from exc


async def _sleep(seconds: float) -> None:
    """間をあける（試験が差し替える）。"""
    await asyncio.sleep(seconds)


async def _http(url: str, *, params: dict | None, headers: dict | None, accept: str, max_bytes: int) -> tuple[int, dict, bytes]:
    """1 回の GET（リダイレクトは各段を検査して追う）。試験はここを差し替える。"""
    return await net.get(url, params=params, headers=headers, accept=accept, max_bytes=max_bytes)


async def _http_post(url: str, *, payload: dict, headers: dict | None, accept: str, max_bytes: int) -> tuple[int, dict, bytes]:
    """1 回の POST（JSON）。試験はここを差し替える。"""
    return await net.post(url, payload=payload, headers=headers, accept=accept, max_bytes=max_bytes)


@dataclass
class Result:
    docs: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)
    note: str = ""

    def count(self) -> int:
        return len(self.docs) + len(self.metrics) + len(self.links)


def _trim(result: Result, left_items: int) -> tuple[Result, bool]:
    """1 日の件数の残りに収める（文書を優先して残す）。切ったら True。"""
    if result.count() <= left_items:
        return result, False
    docs = result.docs[:left_items]
    rest_ = left_items - len(docs)
    result.docs, result.links, result.metrics = docs, result.links[:rest_], result.metrics[: max(0, rest_ - len(result.links[:rest_]))]
    return result, True


def _impl(name: str):
    from qnapnewshub import conn_impl, conn_v08

    return getattr(conn_v08 if name in conn_v08.RUNNERS else conn_impl, name)


async def run_one(cid: str, *, manual: bool = False, **kwargs) -> dict:
    """1 つの connector を 1 回走らせる。{ran, reason, count, error}。予算を超えたら止める。結果はまとめ書きへ積む（ここでは書かない）。"""
    spec = SPECS[cid]
    if spec.adapted or not spec.run:
        return {"ran": False, "reason": "既存の取得は取得先の管理から取り直します", "count": 0}
    ok, why = gate(cid, manual=manual)
    if not ok:
        record_result(cid, ok=True, skipped=why)
        return {"ran": False, "reason": why, "count": 0}
    started = time.monotonic()
    ctx = Ctx(cid, load()["connectors"][cid]["settings"], manual=manual)
    result = Result()
    error, stopped = "", False
    try:
        result = await asyncio.wait_for(_impl(spec.run)(ctx, **kwargs), RUN_TIMEOUT)
    except BudgetStop:
        stopped = True
    except Exception as exc:  # noqa: BLE001 - 1 つの失敗で他を止めない
        logger.info("connector の取得に失敗しました (%s): %s", cid, type(exc).__name__)
        error = f"{type(exc).__name__}: {str(exc)[:120]}".rstrip(": ")
    partial = getattr(ctx, "partial", None)
    if (stopped or error) and isinstance(partial, Result):
        result = partial
    result, cut = _trim(result, budget_left(cid)["items"])
    if result.count():
        writer.get().stage_connector({"connector": cid, "docs": result.docs, "links": result.links, "metrics": result.metrics})
    note_usage(cid, items=result.count(), nbytes=ctx.nbytes, requests=ctx.requests)
    if stopped or cut:
        st = state(cid)
        st["budget_stop"] = {"at": _iso_now(), "reason": _budget_reason(cid) or "予算の上限に達したため途中で止めました"}
    record_result(cid, ok=not error, count=result.count(), error=error, ms=int((time.monotonic() - started) * 1000),
                  extra={"stopped": bool(stopped or cut), "note": result.note, "requests": ctx.requests})
    state(cid)["last_run_at"] = time.time()
    return {"ran": True, "reason": "", "count": result.count(), "error": error, "stopped": bool(stopped or cut), "note": result.note}


def due(cid: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    last = state(cid).get("last_run_at")
    return last is None or now - float(last) >= conf(cid)["interval_minutes"] * 60


async def run_due() -> list[str]:
    """間隔の来た・入の・予算の残っている connector を順に 1 回ずつ（同時には走らせない）。走らせた id を返す。"""
    ran = []
    for cid, spec in SPECS.items():
        if spec.adapted or not spec.scheduled or not conf(cid)["enabled"] or not due(cid):
            continue
        res = await run_one(cid)
        if res["ran"]:
            ran.append(cid)
        elif not conf(cid)["enabled"] or "予算" in res["reason"]:
            state(cid)["last_run_at"] = time.time()      # 予算で止めている間は、間隔ごとにしか調べない
    return ran


# ---- 画面・診断用 ----

def describe(cid: str) -> dict:
    spec, c = SPECS[cid], conf(cid)
    u = usage(cid)
    st = state(cid)
    t = TERMS[cid]
    return {"id": cid, "kind": spec.kind, "kind_label": KINDS[spec.kind], "label": spec.label, "desc": spec.desc, "adapted": spec.adapted, "scheduled": spec.scheduled,
            "enabled": c["enabled"], "interval_minutes": c["interval_minutes"], "daily_items": c["daily_items"], "daily_mb": c["daily_mb"], "settings": c["settings"],
            "has_key_option": spec.has_key, "has_key": has_key(cid), "key_required": spec.key_required, "terms_ack": c["terms_ack"], "needs_ack": needs_ack(cid), "terms": t,
            "usage": {"day": u["day"], "items": u["items"], "mb": round(u["bytes"] / 1024 / 1024, 2), "requests": u["requests"]},
            "budget_stop": st.get("budget_stop"), "last": st.get("last"), "last_ok_at": st.get("last_ok_at", "")}


def describe_all() -> list[dict]:
    return [describe(cid) for cid in SPECS]


def diagnostics() -> dict:
    """診断の節: connector ごとの予算の停止・最後の失敗。読むだけ。"""
    rows = [describe(cid) for cid in SPECS]
    return {"enabled": sum(1 for r in rows if r["enabled"]), "budget_stops": [{"id": r["id"], "label": r["label"], **r["budget_stop"]} for r in rows if r["budget_stop"] and r["enabled"]],
            "failing": [{"id": r["id"], "label": r["label"], "error": r["last"]["error"]} for r in rows if r["enabled"] and r["last"] and r["last"].get("error")],
            "unconfirmed_on": [{"id": r["id"], "label": r["label"]} for r in rows if r["enabled"] and r["terms"]["status"] == "unconfirmed"]}
