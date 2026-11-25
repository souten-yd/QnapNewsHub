"""Wikidata の問い合わせと、候補の絞り込み（名寄せの段 2。ARCHITECTURE 6 章・docs/DESIGN.md の「名寄せの自動化」）。

使う API（Wikidata の Action API。どちらも読むだけ・認証なし）:
- `wbsearchentities`（名前 → 候補。`language`＝名前の言語・`uselang`＝説明の言語・`type=item`・`limit=5`）。返りに label・description・match（label か alias のどれに当たったか）。
- `wbgetentities`（QID → 正式名・説明・別名。`ids` は `|` で最大 50 まとめて 1 回・`props=labels|descriptions|aliases`・`languages=ja|en`。claims は重いので取らない）。
- 利用の決まり（MediaWiki の API:Etiquette・Wikimedia の User-Agent policy・Wikidata:Data access で確かめた〔2026-11-26〕）: 読み取りに固い上限は無いが、**並列にせず 1 件ずつ**・
  まとめられるものは `|` でまとめる・非対話の仕事には `maxlag` を付ける・`429` と `Retry-After` が来たら**その回は止める**・説明のある User-Agent を送る。
  ここでは 1.5 秒に 1 回（PACE。1.1 秒では十数回で 429 が出た）・429 の待ちが短ければ待って続ける・1 回の取りまとめの上限・1 晩の上限（auto_resolve）・結果の覚え（wikidata_cache。見つからなかった結果も覚える）で守る。
  User-Agent は一般的な名前だけ（利用者の名前・連絡先は入れない。公開のリポジトリのため）。
- 通信は net.get（egress の検査・大きさ・時間の上限）。試験は `_fetch` を差し替えて模擬する。

照合の規則（`pick`）: 検索の返りのうち、(1) 問いの名前が label か alias と**正規化して一致**する（`entity_merge.rule_key`）・(2) 説明が**物の種類に合う**（`type_ok`）・
(3) 曖昧さ回避ページ・カテゴリ・人名などは必ず除く、を満たす候補が **ちょうど 1 つ**のときだけ QID を付ける。複数残れば ambiguous（付けない）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

from qnapnewshub import __version__, net
from qnapnewshub.entity_merge import rule_key

API = "https://www.wikidata.org/w/api.php"
UA = f"QnapNewsHub/{__version__} (open-source self-hosted news reader; low-volume entity lookup)"
BUSY_WAITS = (5.0, 20.0)      # 忙しいと言われたら、この秒数あけて最大 2 回やり直す（やり直しも問い合わせ回数に数える）
RATE_WAIT_MAX = 90.0          # 429 の Retry-After がこれ以下なら、待ってやり直す（1 回の取りまとめで最大 RATE_RETRIES 回。超える・続くなら止める）
RATE_RETRIES = 2
PACE = 2.0              # 秒。直列に、1 回ごとに最低これだけあける
MAXLAG = 5
SEARCH_LIMIT = 5
GET_BATCH = 50          # wbgetentities の ids の上限（API の上限）
CACHE_DAYS = 90
CACHE_NONE_DAYS = 30    # 見つからなかった結果を覚える日数
_QID = re.compile(r"^Q[1-9]\d{0,12}$")
_UTC = timezone.utc


class WikidataStop(RuntimeError):
    """この回はもう問い合わせない（429・maxlag・通信の失敗が続いた）。理由は利用者に見せてよい文。"""


class WikidataRateLimited(WikidataStop):
    """429（Retry-After つき）。待ち秒数が短ければ待ってやり直す。2026-11-26 の実測で、1.1 秒おきの検索でも十数回で 429（23 秒待て）が出た。"""

    def __init__(self, message: str, wait: float | None):
        super().__init__(message)
        self.wait = wait


class WikidataBusy(WikidataStop):
    """サーバーが一時的に忙しい（maxlag・検索の混雑 cirrussearch-too-busy-error。2026-11-26 の実測で、間隔を守っていても出る）。少し待ってやり直す（BUSY_WAITS の回数だけ。それでも続けば止める）。"""


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _now() -> float:
    return time.monotonic()


async def _fetch(params: dict) -> dict:
    """Wikidata へ 1 回。429 は WikidataStop（Retry-After を文に）・それ以外の 4xx/5xx と通信の失敗も WikidataStop。maxlag の応答（HTTP 200・error.code=maxlag）も止める。"""
    q = {**params, "format": "json", "formatversion": "2", "maxlag": str(MAXLAG)}
    try:
        status, head, body = await net.get(API, params=q, ua=UA, timeout=20, follow=False, max_bytes=2 * 1024 * 1024)
    except net.NetError as exc:
        raise WikidataStop(f"Wikidata に接続できません: {exc}") from exc
    if status == 429:
        wait = net.parse_retry_after(head.get("retry-after"))
        raise WikidataRateLimited(f"Wikidata が混み合っています（429{f'・{int(wait)} 秒待つよう指示' if wait else ''}）", wait)
    if status >= 400 or status in (301, 302, 303, 307, 308):
        raise WikidataStop(f"Wikidata が応答しません（HTTP {status}）")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WikidataStop("Wikidata の応答を読めません") from exc
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = str(data["error"].get("code", ""))
        if code in ("maxlag", "cirrussearch-too-busy-error"):
            raise WikidataBusy(f"Wikidata のサーバーが忙しい（{code}）")
        raise WikidataStop(f"Wikidata がエラーを返しました（{code[:40]}）")
    return data if isinstance(data, dict) else {}


class Client:
    """直列・間隔つきの問い合わせ。`budget` 回まで（超えたら WikidataStop）。`cache` は {key: (value, at)} の読み取り専用の覚え、`fresh` は今回増えた覚え（呼び出し側が保存する）。"""

    def __init__(self, budget: int, cache: dict[str, tuple[object, datetime]] | None = None):
        self.budget = budget
        self.used = 0
        self.cache = cache or {}
        self.fresh: dict[str, object] = {}
        self._last = 0.0
        self.rate_waits = 0

    def cached(self, key: str) -> object | None:
        if key in self.fresh:
            return self.fresh[key]
        hit = self.cache.get(key)
        if hit is None:
            return None
        value, at = hit
        age = datetime.now(_UTC) - (at if at.tzinfo else at.replace(tzinfo=_UTC))
        limit = CACHE_NONE_DAYS if not value else CACHE_DAYS
        return value if age < timedelta(days=limit) else None

    async def _call(self, params: dict) -> dict:
        if self.used >= self.budget:
            raise WikidataStop("この晩の問い合わせの上限に達しました")
        wait = PACE - (_now() - self._last)
        if wait > 0 and self._last:
            await _sleep(wait)
        for wait_busy in (*BUSY_WAITS, None):
            self._last = _now()
            self.used += 1
            try:
                return await _fetch(params)
            except WikidataRateLimited as exc:
                if exc.wait is None or exc.wait > RATE_WAIT_MAX or self.rate_waits >= RATE_RETRIES or self.used >= self.budget:
                    raise
                self.rate_waits += 1
                await _sleep(exc.wait + 1.0)
            except WikidataBusy:
                if wait_busy is None or self.used >= self.budget:
                    raise
                await _sleep(wait_busy)
        raise WikidataStop("Wikidata が忙しい状態が続きました")      # ここには来ない

    async def search(self, name: str, language: str) -> list[dict]:
        """名前で候補を引く（label・alias の一致のみ。language は ja か en）。覚えがあれば通信しない。"""
        key = f"s:{language}:{rule_key(name) or name.casefold()}"
        got = self.cached(key)
        if got is not None:
            return got if isinstance(got, list) else []
        data = await self._call({"action": "wbsearchentities", "search": name[:100], "language": language, "uselang": language, "type": "item", "limit": str(SEARCH_LIMIT)})
        hits = [flatten(h) for h in data.get("search", []) or [] if isinstance(h, dict) and _QID.match(str(h.get("id", "")))]
        self.fresh[key] = hits
        return hits

    async def entities(self, qids: list[str]) -> dict[str, dict]:
        """QID → {label_ja, label_en, description, aliases}。覚えの無いものだけ、50 件ずつまとめて引く。"""
        out: dict[str, dict] = {}
        todo: list[str] = []
        for q in dict.fromkeys(qids):
            if not _QID.match(q):
                continue
            got = self.cached(f"e:{q}")
            if isinstance(got, dict) and got:
                out[q] = got
            else:
                todo.append(q)
        for i in range(0, len(todo), GET_BATCH):
            part = todo[i:i + GET_BATCH]
            data = await self._call({"action": "wbgetentities", "ids": "|".join(part), "props": "labels|descriptions|aliases", "languages": "ja|en"})
            ents = data.get("entities") if isinstance(data.get("entities"), dict) else {}
            for q in part:
                e = ents.get(q)
                info = parse_entity(e) if isinstance(e, dict) else {}
                self.fresh[f"e:{q}"] = info
                if info:
                    out[q] = info
        return out


def flatten(h: dict) -> dict:
    """検索の返りの 1 件 → 覚えと照合で使う形（id・label・description・match_type・match_text）。"""
    m = h.get("match") if isinstance(h.get("match"), dict) else {}
    return {"id": h["id"], "label": str(h.get("label", ""))[:200], "description": str(h.get("description", ""))[:300],
            "match_type": str(m.get("type", "")), "match_text": str(m.get("text", ""))[:200]}


def _text(block: object, lang: str) -> str:
    v = block.get(lang) if isinstance(block, dict) else None
    return str(v.get("value", "")) if isinstance(v, dict) else ""


def parse_entity(e: dict) -> dict:
    """wbgetentities の 1 件 → {label_ja, label_en, description, aliases}。無い・欠けているものは {}。"""
    if e.get("missing") is not None or not e.get("labels"):
        return {}
    labels, descs, aliases = e.get("labels"), e.get("descriptions"), e.get("aliases")
    al: list[str] = []
    for lang in ("ja", "en"):
        rows = aliases.get(lang) if isinstance(aliases, dict) else None
        for a in rows or []:
            v = str(a.get("value", "")).strip() if isinstance(a, dict) else ""
            if v and v not in al:
                al.append(v[:100])
    return {"label_ja": _text(labels, "ja")[:200], "label_en": _text(labels, "en")[:200],
            "description": (_text(descs, "ja") or _text(descs, "en"))[:300], "aliases": al[:20]}


# ---- 種類の合う候補だけ ----

_BAD = re.compile(r"disambiguation|曖昧さ回避|wikimedia (category|list|template|project|disambig)|ウィキメディア|given name|family name|surname|male given|female given|人名|名字|苗字|"
                  r"wikipedia (list|article)|scholarly article|学術論文|species of|生物の種|album|song|single by|film|映画|television series|テレビ番組|テレビドラマ|漫画|novel|小説|"
                  r"musical group|バンド|singer|歌手|actor|俳優|footballer|サッカー選手|painter|画家|politician and|district|行政区|village|村$|asteroid|小惑星|crater|コミューン|commune", re.I)
_TECH = re.compile(r"software|program|application|\bapp\b|library|framework|operating system|programming language|browser|game engine|platform|open[- ]source|web ?service|website|\bservice\b|"
                   r"\btool|database|compiler|editor|language model|\bmodel\b|neural network|machine learning|artificial intelligence|\bai\b|chatbot|\bllm\b|deep learning|generative|"
                   r"product|device|smartphone|processor|\bchip|\bgpu\b|\bcpu\b|console|computer|laptop|\bphone|tablet|brand|series|hardware|semiconductor|technology|protocol|standard|algorithm|"
                   r"architecture|method|technique|file format|interface|network|concept|specification|"
                   r"ソフト|アプリ|ライブラリ|フレームワーク|オペレーティングシステム|プログラミング言語|ブラウザ|サービス|ツール|データベース|ウェブサイト|モデル|言語モデル|人工知能|チャットボット|ニューラル|機械学習|"
                   r"製品|端末|スマートフォン|プロセッサ|半導体|ゲーム機|ブランド|シリーズ|ハードウェア|技術|規格|プロトコル|アルゴリズム|方式|手法|概念|フォーマット|プラットフォーム|エンジン|コンピュータ|サーバ", re.I)
_ORG = re.compile(r"company|corporation|organi[sz]ation|research (lab|institute|organi)|institute|startup|foundation|agency|university|manufacturer|conglomerate|business|firm|enterprise|"
                  r"publisher|studio|consortium|association|laboratory|会社|企業|団体|機関|法人|研究所|組織|大学|メーカー|財団|ベンダー|スタートアップ|協会|コンソーシアム|研究室|開発元", re.I)
_PERSON = re.compile(r"\bborn\b|entrepreneur|engineer|researcher|scientist|businessperson|businessman|businesswoman|executive|programmer|\bceo\b|founder|professor|investor|developer|inventor|"
                     r"人物|起業家|実業家|研究者|科学者|技術者|プログラマ|経営者|教授|創業者|生まれ|投資家|開発者|エンジニア", re.I)
_GAME = re.compile(r"video game|game|ゲーム", re.I)
_POLICY = re.compile(r"\blaw\b|\bact\b|regulation|policy|treaty|legislation|directive|法|規制|条約|政策|制度|指令|条例", re.I)
_PLACE = re.compile(r"city|country|region|prefecture|state|town|municipality|capital|island|province|市|国|県|州|地域|都市|町|島|首都", re.I)
_CLUSTER = {"model": "tech", "software": "tech", "tech": "tech", "product": "tech", "game": "game", "org": "org", "person": "person", "policy": "policy", "place": "place", "other": "other"}


def cluster(entity_type: str) -> str:
    """種類の大きなまとまり。model・software・tech・product は、LLM の取り出しでよく揺れるので同じまとまり（tech）とみなす。"""
    return _CLUSTER.get(entity_type, "other")


def clusters_compatible(a: str, b: str) -> bool:
    ca, cb = cluster(a), cluster(b)
    return ca == cb or "other" in (ca, cb)


def type_ok(entity_type: str, description: str) -> bool:
    """説明が物の種類に合うか。曖昧さ回避・カテゴリ・人名などは常に不合格。種類 other は、技術・製品・組織らしい説明のときだけ通す。説明が空なら不合格（根拠が無い）。"""
    d = str(description or "").strip()
    if not d or _BAD.search(d):
        return False
    c = cluster(entity_type)
    if c == "other":
        return bool(_TECH.search(d) or _ORG.search(d))      # 種類が分からない物は、ニュースの物らしい説明（技術・製品・組織）のときだけ（Alpamayo が同名の山に付いた実測の対策）
    if c == "tech":
        return bool(_TECH.search(d)) and not (_GAME.search(d) and not re.search(r"software|ソフト|engine|エンジン", d, re.I)) and not _PERSON.search(d.split(",")[0])
    pat = {"org": _ORG, "person": _PERSON, "game": _GAME, "policy": _POLICY, "place": _PLACE}[c]
    return bool(pat.search(d))


def pick(name: str, entity_type: str, hits: list[dict]) -> tuple[str, str, str]:
    """検索の返りから 1 つ選ぶ。(status, qid, 理由)。status: linked・none・ambiguous。
    名前が label／alias と正規化して一致し、説明が種類に合うものがちょうど 1 つのときだけ linked。"""
    want = rule_key(name)
    if not want:
        return "none", "", "名前が短すぎる"
    ok = []
    for h in hits:
        names = {rule_key(h.get("label", "")), rule_key(h.get("match_text", ""))} if h.get("match_type") in ("label", "alias") else {rule_key(h.get("label", ""))}
        if want in names and type_ok(entity_type, h.get("description", "")):
            ok.append(h)
    if not ok:
        return "none", "", "名前と種類が合う候補なし"
    if len(ok) == 1:
        return "linked", ok[0]["id"], "一意"
    exact = [h for h in ok if rule_key(h.get("label", "")) == want]
    if len(exact) == 1:
        return "linked", exact[0]["id"], "正式名が一致"
    return "ambiguous", "", f"候補が {len(ok)} 件"


def query_language(name: str) -> str:
    """名前の言語（検索の language）。ASCII だけなら en、そうでなければ ja。"""
    return "en" if str(name).isascii() else "ja"
