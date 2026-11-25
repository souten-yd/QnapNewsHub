"""軽い翻訳（英→日）。エンジンの枠・用語の保護・訳の検査・文ごとのキャッシュ・保存と読み。ARCHITECTURE 8.5。

目的: **LLM が引く検索の精度と密度を上げる**（とくに日本語の問いで英語の記事を引く）。訳した題・概要・本文は全文検索の索引に入る（searchindex）。
決定の根拠は 2026-11-25 の実測（QNAP の標準は QuickMT int8。Bergamot は固有名詞を崩すので採らない。PLaMo は QNAP では遅すぎるので、重要な記事の高品質の訳は
ControlDeck が借りる仕事 translate_hq で）。

- **エンジンの枠**: `Engine`（translate(文の一覧) → 訳の一覧）。既定は QuickMT（CTranslate2・int8・beam 2・スレッド 2・バッチ 32）。設定の engine=off で切れる。
  モデルは利用者の操作で取得する（Hugging Face の quickmt/quickmt-en-ja の版を固定・ファイルごとの sha256 を確かめる・置き場は data_dir/models/）。
  メモリに 1 つだけ保持する。CPU の命令（AVX など）が足りない機械でも動く（CTranslate2 が実行時に選ぶ。設定の cpu_isa で CT2_FORCE_CPU_ISA を指定できる）。
- **用語の保護**（必須）: 訳す前に URL・コード（`…`・パス・コマンド・フラグ）・Things の名前と別名・指定の語・版・単位つきの数値を「ZQ1Z」の印に置き換え、訳した後に戻す
  （実測で QuickMT が URL の /en/ を /ja/ に書き換え・quantization を「定量化」と誤訳したため）。対訳表の語は日本語の訳へ置き換える（設定で足せる）。
  印の形は ZQ{n}Z（12 文の実測で全部残った。[[n]]・# n #・⟦n⟧ は崩れる・落ちる）。
- **訳の検査**: 印がすべて 1 回ずつ戻ったか・数値の一致・英語のまま残った割合・同じ語の繰り返し・空。落ちた文は **原文のまま残し「〔未訳〕」の印**を付ける。
- **文ごとのキャッシュ**: 文（印に置き換えたあと）の hash → 訳。配信記事・同じ出来事の重複で同じ文を訳さない。エンジンの版が変われば別の鍵（訳し直せる）。
- 保存は原文と別の表（news_translation。エンジン・版・時刻つき）。書き込みは Write Coordinator のまとめ書き（stage_translation）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from qnapnewshub import db as news_db
from qnapnewshub import egress, settings, writer
from qnapnewshub.config import data_dir
from qnapnewshub.db import NewsItem, NewsTr, NewsTrCache

logger = logging.getLogger("qnapnewshub.translate")

_UTC = timezone.utc
UNTRANSLATED = "〔未訳〕"
CACHE_KEEP_DAYS = 45
CACHE_MAX_ROWS = 300_000
MAX_SENTENCE = 400          # これより長い文は、読点・セミコロンの所で分けて訳す（QuickMT の 1 文の上限に近づけない）

# ---- モデル（取得・場所） ----

MODEL = {
    "id": "quickmt-en-ja", "title": "QuickMT 英→日", "repo": "quickmt/quickmt-en-ja", "rev": "c09e98b8438a239a8210060114cea19c426c0559",
    "license": "CC-BY-4.0", "license_note": "モデルは CC-BY-4.0（商用・再配布可・帰属表示）。ライブラリは Apache-2.0（quickmt）・MIT（CTranslate2）",
    "files": {
        "model.bin": ("a2653ed557273846433473e87bc35e482d956dd529be02faafc0ab9597816585", 401699775),
        "src.spm.model": ("01dc857df9df5987bec61812d1fbc2e78ad530346c09fe3ffaf27184358ab8fd", 583983),
        "tgt.spm.model": ("cbd449d7e92d850f6d36efeafb40cea3a8468f55db0ca751ee4ece0ceb70d19b", 583133),
        "source_vocabulary.json": ("b896ae8632a0ebb369c5982658c3bd94eefa01e4574ccd659d87272498bbe743", 332239),
        "target_vocabulary.json": ("b130ef6213111d052be24288d6a0ac1eecf2b0bbe54cc4f7d63b56d2bd57c334", 435375),
        "config.json": ("66edc5d58ceb812a41b4b784497001f815c35400884f40984b19de29e1bc3d69", 223),
    },
}
MODEL_HOSTS = ("huggingface.co",)         # 取得してよいホスト（と、その下 *.huggingface.co・*.hf.co）。https の 443 だけ
MAX_REDIRECTS = 4
UA = "QnapNewsHub-model-fetch"


def model_dir() -> Path:
    return data_dir() / "models" / f"{MODEL['id']}-{MODEL['rev'][:8]}"


def installed() -> bool:
    """モデルのファイルがそろい、取得時に sha256 を確かめた印（.verified）がある。"""
    d = model_dir()
    try:
        if not (d / ".verified").is_file():
            return False
        return all((d / name).is_file() and (d / name).stat().st_size == size for name, (_, size) in MODEL["files"].items())
    except OSError:
        return False


def _host_ok(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in MODEL_HOSTS) or host.endswith(".hf.co") or host == "hf.co"


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or not parts.hostname or not _host_ok(parts.hostname):
        raise ModelError("許可していない取得先です（https の huggingface.co だけ）")
    try:
        port = parts.port
    except ValueError:
        raise ModelError("取得先の URL が正しくありません") from None
    if port not in (None, 443):
        raise ModelError("許可していないポートへは接続しません")


class ModelError(RuntimeError):
    """モデルの取得・読み込みの失敗（利用者に見せてよい文）。"""


_DL: dict = {"phase": "idle", "message": "", "done": 0, "total": 0, "file": "", "started": 0.0, "error": ""}
_dl_task: asyncio.Task | None = None


def download_state() -> dict:
    return dict(_DL)


def _new_client():
    """外への接続は egress（検査した IP へ固定・私的な IP へは行かない）。試験はここを差し替える。"""
    return egress.guarded_client(timeout=60, headers={"User-Agent": UA})


async def _fetch_file(name: str, sha: str, size: int, dest: Path) -> None:
    url = f"https://huggingface.co/{MODEL['repo']}/resolve/{MODEL['rev']}/{name}"
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    total = 0
    current = url
    fh = None
    try:
        async with _new_client() as client:
            for _ in range(MAX_REDIRECTS + 1):
                _check_url(current)
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            raise ModelError("リダイレクト先がありません")
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code != 200:
                        raise ModelError(f"取得できませんでした（HTTP {resp.status_code}）")
                    fh = await asyncio.to_thread(open, part, "wb")
                    async for chunk in resp.aiter_bytes(1 << 20):
                        total += len(chunk)
                        if total > size:
                            raise ModelError("ファイルが想定より大きいので止めました")
                        h.update(chunk)
                        await asyncio.to_thread(fh.write, chunk)
                        _DL["done"] += len(chunk)
                    break
            else:
                raise ModelError("リダイレクトが多すぎます")
    except egress.EgressRejected as exc:
        raise ModelError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ModelError):
            raise
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            raise ModelError("時間内に応答がありません") from exc
        if isinstance(exc, httpx.HTTPError):
            raise ModelError(f"接続できません（{type(exc).__name__}）") from exc
        raise
    finally:
        if fh is not None:
            await asyncio.to_thread(fh.close)
    if total != size or h.hexdigest() != sha:
        part.unlink(missing_ok=True)
        raise ModelError(f"{name} の大きさか sha256 が固定の値と違います。入れません")
    await asyncio.to_thread(os.replace, part, dest)


async def download_model() -> None:
    """モデルを取得して、ファイルごとに sha256 を確かめて置く（利用者の操作で）。進み具合は download_state()。"""
    global _dl_task
    if _DL["phase"] == "downloading":
        raise ModelError("取得の途中です")
    if installed():
        raise ModelError("すでに入っています")
    d = model_dir()
    _DL.update(phase="downloading", message="取得しています", done=0, total=sum(s for _, s in MODEL["files"].values()), file="", started=time.time(), error="")
    try:
        await asyncio.to_thread(d.mkdir, 0o700, True, True)
        for name, (sha, size) in MODEL["files"].items():
            _DL["file"] = name
            dest = d / name
            if dest.is_file() and dest.stat().st_size == size and _sha_file(dest) == sha:
                _DL["done"] += size
                continue
            await _fetch_file(name, sha, size, dest)
        (d / ".verified").write_text(MODEL["rev"], encoding="utf-8")
        _DL.update(phase="done", message="取得して sha256 を確かめました", file="")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) if isinstance(exc, ModelError) else f"取得に失敗しました（{type(exc).__name__}）"
        logger.warning("翻訳モデルの取得に失敗しました: %s", type(exc).__name__)
        _DL.update(phase="error", message=msg, error=msg, file="")
        raise ModelError(msg) from exc


def start_download() -> bool:
    """バックグラウンドで取得を始める。すでに取得中・入っているなら False。"""
    global _dl_task
    if _DL["phase"] == "downloading" or installed():
        return False

    async def run() -> None:
        try:
            await download_model()
        except ModelError:
            pass

    _dl_task = asyncio.get_running_loop().create_task(run())
    return True


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def remove_model() -> bool:
    """モデルを消す（メモリからも外す）。"""
    import shutil

    unload()
    d = model_dir()
    if d.is_dir():
        shutil.rmtree(d)
        _DL.update(phase="idle", message="", done=0, total=0, error="")
        return True
    return False


# ---- エンジン ----

class Engine:
    """翻訳のエンジンの枠。translate は文（印に置き換えたあと）の一覧を訳して、同じ長さの一覧を返す。version は訳の覚えの鍵に入る（変われば訳し直す）。"""

    name = "off"
    version = "off"

    def translate(self, sentences: list[str]) -> list[str]:
        raise NotImplementedError


class QuickMT(Engine):
    name = "quickmt"

    def __init__(self, path: Path, *, threads: int, batch: int, beam: int, isa: str = "") -> None:
        if isa:
            os.environ["CT2_FORCE_CPU_ISA"] = isa      # CTranslate2 の CPU 判定を指定する（空なら CTranslate2 が実行時に選ぶ）
        try:
            import ctranslate2
            import sentencepiece as spm
        except ImportError as exc:
            raise ModelError("翻訳の部品（ctranslate2・sentencepiece）が入っていません") from exc
        try:
            self._tr = ctranslate2.Translator(str(path), device="cpu", compute_type="int8", intra_threads=threads, inter_threads=1)
            self._src = spm.SentencePieceProcessor(model_file=str(path / "src.spm.model"))
            self._tgt = spm.SentencePieceProcessor(model_file=str(path / "tgt.spm.model"))
        except Exception as exc:  # noqa: BLE001
            raise ModelError(f"翻訳モデルを読み込めません（{type(exc).__name__}）") from exc
        self.batch, self.beam = batch, beam
        self.version = f"quickmt-en-ja@{MODEL['rev'][:8]}:int8:beam{beam}"

    def translate(self, sentences: list[str]) -> list[str]:
        if not sentences:
            return []
        toks = [self._src.encode(s, out_type=str) for s in sentences]
        res = self._tr.translate_batch(toks, beam_size=self.beam, max_batch_size=self.batch, max_decoding_length=256)
        return [self._tgt.decode(r.hypotheses[0]) for r in res]


_eng_lock = threading.Lock()
_eng: dict = {"key": None, "engine": None}
_override: Engine | None = None
run_lock = threading.Lock()      # CT2 の呼び出しは 1 つずつ（背景の仕事と、記事を開いたときの要求がぶつからない）


def use_engine(engine: Engine | None) -> None:
    """試験用: 本物のモデルの代わりに模擬のエンジンを使う（None で戻す）。"""
    global _override
    _override = engine


def unload() -> None:
    with _eng_lock:
        _eng.update(key=None, engine=None)


def get_engine(cfg: dict | None = None) -> Engine | None:
    """設定のエンジン。切・モデルが無い・読めないときは None。読み込んだものはメモリに保持する（設定が変われば読み直す）。"""
    if _override is not None:
        return _override
    cfg = cfg or settings.load()["translate"]
    if cfg["engine"] != "quickmt" or not installed():
        return None
    key = (cfg["engine"], cfg["threads"], cfg["batch"], cfg["beam"], cfg["cpu_isa"], str(model_dir()))
    with _eng_lock:
        if _eng["key"] != key or _eng["engine"] is None:
            t0 = time.monotonic()
            _eng.update(key=None, engine=None)
            try:
                _eng["engine"] = QuickMT(model_dir(), threads=cfg["threads"], batch=cfg["batch"], beam=cfg["beam"], isa=cfg["cpu_isa"])
            except ModelError as exc:
                logger.warning("%s", exc)
                _STATS["load_error"] = str(exc)
                return None
            _eng["key"] = key
            _STATS["load_ms"] = int((time.monotonic() - t0) * 1000)
            _STATS["load_error"] = ""
        return _eng["engine"]


def engine_loaded() -> bool:
    return _override is not None or _eng["engine"] is not None


# ---- 文字・言語・文の分割 ----

_JA = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")
_ASCII_LETTER = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def is_english(text: str) -> bool:
    """英語の文章か（日本語の記事は訳さない）。仮名・漢字が 1 割以上なら日本語（題に固有名詞だけ英語の日本語の記事を訳さない）。"""
    text = text or ""
    ja = len(_JA.findall(text))
    asc = len(_ASCII_LETTER.findall(text))
    if asc < 4:
        return False
    return ja / (ja + asc) < 0.1


ABBR = ("Mr", "Mrs", "Ms", "Dr", "Prof", "Inc", "Ltd", "Co", "vs", "etc", "e.g", "i.e", "No", "St", "Jr", "Sr", "U.S", "Fig")
_ABBR_END = re.compile(r"(?:%s)\.$" % "|".join(re.escape(a) for a in ABBR))
_SENT_SPLIT = re.compile(r"(?<=[.!?])[\"”’)]?\s+(?=[A-Z“\"(\[0-9])")


def split_sentences(text: str) -> list[str]:
    """段落（改行）ごとに文へ分ける。略語（Dr.・U.S. など）の後ろでは分けない。MAX_SENTENCE を超える文は読点・セミコロンの所で分ける。"""
    out: list[str] = []
    for para in [p.strip() for p in (text or "").split("\n") if p.strip()]:
        cur = ""
        for part in _SENT_SPLIT.split(para):
            if cur and _ABBR_END.search(cur):
                cur += " " + part
            else:
                if cur:
                    out.append(cur)
                cur = part
        if cur:
            out.append(cur)
    final: list[str] = []
    for s in out:
        final.extend(_split_long(s))
    return final


def _split_long(s: str) -> list[str]:
    if len(s) <= MAX_SENTENCE:
        return [s]
    parts = re.split(r"(?<=[;:,])\s+", s)
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > MAX_SENTENCE:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    # 区切りの無い長い文は、空白で強制的に切る
    res: list[str] = []
    for p in out:
        while len(p) > MAX_SENTENCE * 2:
            cut = p.rfind(" ", 0, MAX_SENTENCE)
            cut = cut if cut > 40 else MAX_SENTENCE
            res.append(p[:cut])
            p = p[cut:].strip()
        res.append(p)
    return res


def lead_sentences(body: str, n: int = 3, min_chars: int = 40) -> str:
    """本文の先頭 n 文（40 文字未満の行・見出しは飛ばす。同じ文は 1 回）。英文の概要に使う。"""
    out: list[str] = []
    for s in split_sentences(body):
        s = s.strip()
        if len(s) < min_chars or not is_english(s) or s in out:
            continue
        out.append(s)
        if len(out) >= n:
            break
    return " ".join(out)


# ---- 用語の保護 ----

PLACEHOLDER = "ZQ{n}Z"
_PH = re.compile(r"Z\s?Q\s?(\d{1,3})\s?Z")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BACKTICK = re.compile(r"`[^`\n]{1,200}`")
_PATH = re.compile(r"(?<![\w/.])(?:~|\.{1,2})?(?:/[\w.@+\-]+){2,}/?|(?<![\w/.])~/[\w.@+\-/]+|(?<![\w/.])\./[\w.@+\-/]+")
_FILE = re.compile(r"(?<![\w/.])[\w\-]+(?:\.[\w\-]+)*\.(?:py|js|ts|tsx|jsx|json|ya?ml|md|txt|toml|sh|cpp|cc|hpp|rs|go|gguf|bin|safetensors|onnx|pt|ckpt|zip|tar\.gz|exe|dll|so|log|csv|html|css|ini|cfg|conf)\b")
_FLAG = re.compile(r"(?<![\w-])--[a-zA-Z][\w-]*(?:=[^\s,;]+)?")
_ENVVAR = re.compile(r"\b[A-Z][A-Z0-9_]{2,}=[^\s,;]+")
_CMD = re.compile(
    r"\b(?:pip3?|npm|npx|pnpm|yarn|apt(?:-get)?|brew|git|docker|podman|curl|wget|sudo|make|cargo|python3?|uv|conda|ollama|systemctl|cmake|go)\s+"
    r"(?:install|run|add|build|pull|clone|exec|start|stop|restart|enable|checkout|commit|push|serve|init|update|upgrade|compose|-[\w-]+)\b"
    r"(?:\s+(?:--?[\w-]+(?:=\S+)?|[\w./@:=~+-]*[0-9=/@.:~_-][\w./@:=~+-]*))*(?:\s+[\w./@-]+(?![\w ]*\b(?:and|or|to|for|on|in|with|the)\b))?")
_VERSION = re.compile(r"(?<![\w.])(?:v\d+(?:\.\d+)*(?:-[A-Za-z0-9.]+)?|\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?|[bB]\d{4,})(?![\w.]*\w)|(?<![\w.])\d+\.\d+\.\d+\.\d+")
_MODELNAME = re.compile(r"(?<![\w.-])[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*\d[A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*(?![\w-])")
_UNITNUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?(?:GB|MB|KB|TB|GiB|MiB|KiB|TiB|tok/s|tokens/s|tokens/sec|t/s|TFLOPS|GFLOPS|GHz|MHz|kHz|Gbps|Mbps|ms|fps|W|mAh|nm|TOPS)\b")
_ORDINAL = re.compile(r"^\d+(?:st|nd|rd|th|s|x|k|m|b|d|h|am|pm)$", re.I)

# 既定の対訳表（英語 → 日本語。訳す前に印へ置き換え、訳した後に日本語の語へ戻す）。設定の対訳表で足す・上書きする。
DEFAULT_GLOSSARY = {
    "quantization": "量子化", "quantisation": "量子化", "quantized": "量子化された", "quantised": "量子化された", "prefill": "プリフィル", "inference": "推論",
    "fine-tuning": "ファインチューニング", "fine-tune": "ファインチューニング", "fine-tuned": "ファインチューニングされた", "context window": "コンテキストウィンドウ",
    "throughput": "スループット", "latency": "レイテンシ", "embedding": "埋め込み", "embeddings": "埋め込み", "open-source": "オープンソース", "open source": "オープンソース",
    "benchmark": "ベンチマーク", "benchmarks": "ベンチマーク", "checkpoint": "チェックポイント", "tokenizer": "トークナイザー", "hallucination": "ハルシネーション",
    "security token": "セキュリティトークン", "tokenized": "トークン化された",
}
GLOSSARY_V = "g1"      # 既定の対訳表を変えたら上げる（保護の結果が変わるので、訳の覚えの鍵に入れる）


class Ctx:
    """1 つの記事（題・概要・本文）を訳すときの、保護する語の集まり。"""

    def __init__(self, entities: list[str] | None = None, cfg: dict | None = None) -> None:
        cfg = cfg or settings.load()["translate"]
        self.glossary = {**DEFAULT_GLOSSARY, **{k.lower(): v for k, v in cfg["glossary"].items()}}
        self.max_en_ratio = float(cfg["max_en_ratio"])
        names = [n for n in dict.fromkeys([*(entities or []), *cfg["keep"]]) if len(n) >= 3 and n.isascii()]
        names.sort(key=len, reverse=True)
        self._ents = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % "|".join(re.escape(n) for n in names[:400])) if names else None
        keys = sorted(self.glossary, key=len, reverse=True)
        self._gl = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in keys), re.I) if keys else None


def _trim_url(s: str) -> str:
    while s and s[-1] in ".,;:!?)]}'\"”’":
        if s[-1] == ")" and s.count("(") >= s.count(")"):
            break
        s = s[:-1]
    return s


def protect(text: str, ctx: Ctx) -> tuple[str, list[tuple[int, str, str]]]:
    """text の保護する部分を印（ZQ1Z…）に置き換える。(印に置き換えた文, [(番号, 元の文字列, 戻すときの文字列)])。優先順位: URL・コード > 対訳表・Things・指定の語 > 版・型番・単位つきの数値。"""
    cands: list[tuple[int, int, int, str, str]] = []      # (優先, 始まり, 終わり, 元, 戻す)

    def add(prio: int, rx: re.Pattern | None, trim=None, glossary: bool = False) -> None:
        if rx is None:
            return
        for m in rx.finditer(text):
            s, e = m.start(), m.end()
            if trim:
                got = trim(m.group(0))
                e = s + len(got)
            if e <= s:
                continue
            raw = text[s:e]
            back = raw
            if glossary:
                back = ctx.glossary.get(re.sub(r"\s+", " ", raw.lower()), raw)
            cands.append((prio, s, e, raw, back))

    add(0, _BACKTICK)
    add(0, _URL, _trim_url)
    add(0, _EMAIL)
    add(1, _CMD)
    add(1, _ENVVAR)
    add(1, _PATH)
    add(1, _FILE)
    add(1, _FLAG)
    add(2, ctx._gl, glossary=True)
    add(2, ctx._ents)
    add(3, _VERSION)
    add(3, _UNITNUM)
    # 型番（Qwen3-32B・GPT-5.1・R9700 など）。序数・単位だけの語（1st・10x）は除く
    for m in _MODELNAME.finditer(text):
        raw = m.group(0)
        if len(raw) >= 3 and not _ORDINAL.match(raw) and not raw.isdigit():
            cands.append((4, m.start(), m.end(), raw, raw))
    cands.sort(key=lambda c: (c[0], -(c[2] - c[1]), c[1]))
    taken: list[tuple[int, int, int, str, str]] = []
    for c in cands:
        if all(c[2] <= t[1] or c[1] >= t[2] for t in taken):
            taken.append(c)
    taken.sort(key=lambda c: c[1])
    out, slots, pos = [], [], 0
    for n, (_, s, e, raw, back) in enumerate(taken, 1):
        if n > 99:
            break
        out.append(text[pos:s])
        out.append(PLACEHOLDER.format(n=n))
        slots.append((n, raw, back))
        pos = e
    out.append(text[pos:])
    return "".join(out), slots


def restore(masked: str, slots: list[tuple[int, str, str]]) -> str | None:
    """訳した文の印を元（または対訳表の日本語）へ戻す。印が欠けた・重複した・知らない番号が出たら None。"""
    want = {n: back for n, _, back in slots}
    got = [int(x) for x in _PH.findall(masked)]
    if sorted(got) != sorted(want):
        return None
    return _PH.sub(lambda m: want[int(m.group(1))], masked)


# ---- 訳の検査 ----

_NUM = re.compile(r"\d+(?:\.\d+)?")
_SCALE_AFTER = re.compile(r"\s?(?:thousand|million|billion|trillion|percent|per cent|%|k\b|bn\b|m\b|cents?)", re.I)
_REPEAT = re.compile(r"(\S.{1,19}?)\1{3,}")


def _numbers(text: str, *, source: bool) -> set[str]:
    text = unicodedata.normalize("NFKC", text).replace(",", "")
    out: set[str] = set()
    for m in _NUM.finditer(text):
        if source and _SCALE_AFTER.match(text, m.end()):
            continue      # 「1.5 billion」→「15 億」のように数の見かけが変わる
        out.add(m.group(0))
    return out


def check(src: str, masked_src: str, masked_out: str, restored: str | None, ctx: Ctx) -> str:
    """訳の検査。合格なら空、落ちたら理由（marks・numbers・english・repeat・empty）。src は原文、masked_src は印に置き換えた原文。"""
    if not masked_out.strip():
        return "empty"
    if restored is None or "ZQ" in _PH.sub("", masked_out) and _PH.search(restored):
        return "marks"
    if _REPEAT.search(masked_out) or len(masked_out) > 8 * len(masked_src) + 60:
        return "repeat"
    if not _numbers(src, source=True) <= _numbers(restored, source=False):
        return "numbers"
    bare = _PH.sub("", masked_out)
    # 英語のまま残った割合: 固有名詞（大文字で始まる語・大文字を含む語）は訳しても英語で残るので数えない。小文字だけの英単語が残ったら訳せていない
    asc = sum(len(w) for w in _WORD.findall(bare) if w.islower())
    ja = len(_JA.findall(bare))
    src_asc = sum(len(w) for w in _WORD.findall(_PH.sub("", masked_src)) if w.islower())
    if src_asc >= 12 and asc / (asc + ja + 1) > ctx.max_en_ratio:
        return "english"
    if src_asc >= 12 and ja < 2:
        return "english"
    return ""


# ---- 統計（診断。プロセスの間だけ。保存した行の数は DB から数える） ----

_STATS: dict = {"units": 0, "cache_hits": 0, "checked": 0, "failed": 0, "reasons": Counter(), "chars": 0, "seconds": 0.0, "items": 0, "bodies": 0, "ondemand": 0,
                "load_ms": 0, "load_error": "", "last_at": "", "last_error": "", "recent": deque(maxlen=50)}
_stats_lock = threading.Lock()


def stats() -> dict:
    with _stats_lock:
        s = dict(_STATS)
        s["reasons"] = dict(s["reasons"])
        s["recent"] = list(s["recent"])
    s["chars_per_sec"] = round(s["chars"] / s["seconds"]) if s["seconds"] > 0 else 0
    s["cache_hit_pct"] = round(s["cache_hits"] * 100 / s["units"]) if s["units"] else None
    s["fail_pct"] = round(s["failed"] * 100 / s["checked"], 1) if s["checked"] else None
    return s


def _note(**kw) -> None:
    with _stats_lock:
        for k, v in kw.items():
            if k == "reasons":
                _STATS["reasons"].update(v)
            elif k in ("last_at", "last_error", "load_error"):
                _STATS[k] = v
            else:
                _STATS[k] += v


# ---- 訳す（キャッシュつき） ----

def _key(engine: Engine, masked: str) -> str:
    return hashlib.sha1(f"{engine.version}|{GLOSSARY_V}\0{masked}".encode("utf-8")).hexdigest()[:20]


def _cache_get(keys: list[str]) -> dict[str, str]:
    out = writer.get().pending_trcache(keys)
    rest = [k for k in dict.fromkeys(keys) if k not in out]
    if rest:
        with news_db.session() as db:
            for i in range(0, len(rest), 500):
                out.update(dict(db.execute(select(NewsTrCache.h, NewsTrCache.ja).where(NewsTrCache.h.in_(rest[i:i + 500]))).all()))
    return out


def _translatable(masked: str) -> bool:
    """印を除いて、訳す英語（ASCII の文字 3 つ以上の語）が残っているか。URL・コードだけの文は訳さない。"""
    return bool(re.search(r"[A-Za-z]{3,}", _PH.sub(" ", masked)))


class Unit:
    __slots__ = ("src", "masked", "slots", "key", "out", "status")

    def __init__(self, src: str) -> None:
        self.src, self.masked, self.slots, self.key, self.out, self.status = src, src, [], "", src, "keep"


def translate_units(texts: list[str], ctx: Ctx, engine: Engine, cache_out: dict[str, str]) -> list[Unit]:
    """文（または題）の一覧を訳す。各 Unit の status: keep（訳す必要が無い）・ok・cached（覚えから）・fail:<理由>（検査に落ちた。out は原文）。
    cache_out に、新しく訳した文の覚え（鍵 → 印のままの訳）を足す。"""
    units = [Unit(t) for t in texts]
    for u in units:
        if not is_english(u.src):
            continue
        u.masked, u.slots = protect(u.src, ctx)
        if not _translatable(u.masked):
            continue
        u.key = _key(engine, u.masked)
        u.status = "todo"
    todo = [u for u in units if u.status == "todo"]
    hits = _cache_get([u.key for u in todo]) if todo else {}
    fresh: dict[str, str] = {}
    need = list(dict.fromkeys(u.masked for u in todo if u.key not in hits))
    t0 = time.monotonic()
    chars = sum(len(m) for m in need)
    if need:
        outs = engine.translate(need)
        if len(outs) != len(need):
            raise ModelError("翻訳の結果の数が合いません")
        for m, o in zip(need, outs):
            fresh[_key(engine, m)] = o
        cache_out.update(fresh)
    dt = time.monotonic() - t0
    fails: Counter = Counter()
    for u in todo:
        raw = hits.get(u.key)
        cached = raw is not None
        raw = raw if cached else fresh[u.key]
        restored = restore(raw, u.slots)
        reason = check(u.src, u.masked, raw, restored, ctx)
        if reason:
            u.status, u.out = f"fail:{reason}", u.src
            fails[reason] += 1
        else:
            u.status, u.out = ("cached" if cached else "ok"), restored or raw
    n_hit = sum(1 for u in todo if u.status == "cached" or (u.status.startswith("fail") and u.key in hits))
    _note(units=len(todo), cache_hits=n_hit, checked=len(todo), failed=sum(fails.values()), reasons=fails, chars=chars, seconds=dt, last_at=_now_iso())
    if need:
        with _stats_lock:
            _STATS["recent"].append({"at": _now_iso(), "units": len(need), "chars": chars, "ms": int(dt * 1000)})
    return units


def _now_iso() -> str:
    return datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def translate_text(text: str, ctx: Ctx, engine: Engine, cache_out: dict[str, str]) -> tuple[str, int, int]:
    """段落つきの文章（本文）を訳す。(訳した文章, 訳した文の数, 落ちた文の数)。落ちた文は原文のまま「〔未訳〕」を付ける。段落は改行で残す。"""
    paras = [[s for s in split_sentences(p)] for p in (text or "").split("\n") if p.strip()]
    flat = [s for p in paras for s in p]
    got = translate_units(flat, ctx, engine, cache_out) if flat else []
    out_paras, k, n_units, n_fail = [], 0, 0, 0
    for p in paras:
        pieces: list[str] = []
        for _ in p:
            u = got[k]
            k += 1
            if u.status == "keep":
                pieces.append(u.out)
            elif u.status.startswith("fail"):
                pieces.append(f"{UNTRANSLATED}{u.out}")
                n_units += 1
                n_fail += 1
            else:
                pieces.append(u.out)
                n_units += 1
        out_paras.append("".join(p_ if p_.startswith(UNTRANSLATED) or not re.match(r"[A-Za-z0-9]", p_[:1]) else " " + p_ for p_ in pieces).strip())
    return "\n".join(out_paras), n_units, n_fail


def translate_short(text: str, ctx: Ctx, engine: Engine, cache_out: dict[str, str]) -> tuple[str, bool]:
    """題など 1 つの文（分けない）。(訳, 訳せたか)。訳せなかった・訳す必要が無いときは ("", False)。"""
    if not is_english(text):
        return "", False
    u = translate_units([re.sub(r"\s+", " ", text).strip()], ctx, engine, cache_out)[0]
    return (u.out, True) if u.status in ("ok", "cached") else ("", False)


def entity_names(db, item_id: int) -> list[str]:
    """記事に出る Things の名前と別名（保護する語）。取り出しが済んでいない記事は空。"""
    from sqlalchemy import text

    rows = db.execute(text("SELECT e.name FROM article_entities ae JOIN entities e ON e.id = ae.entity_id WHERE ae.article_id = :a "
                           "UNION SELECT al.alias FROM article_entities ae JOIN entity_aliases al ON al.entity_id = ae.entity_id WHERE ae.article_id = :a"), {"a": item_id}).all()
    return [r[0] for r in rows if r[0]]


# ---- 保存（まとめ書きの中で） ----

def apply(db, translations: dict[int, dict], trcache: dict[str, str]) -> int:
    """積んだ機械翻訳と文の覚えを書く（コミットは呼ぶ側。1 トランザクション。全文検索の索引は news_translation のトリガーが追う）。"""
    ids = list(translations)
    exist: set[int] = set()
    for i in range(0, len(ids), 500):
        exist |= set(db.scalars(select(NewsItem.id).where(NewsItem.id.in_(ids[i:i + 500]))))
    done = 0
    for item_id, rec in translations.items():
        if item_id not in exist:
            continue
        row = db.get(NewsTr, item_id)
        if row is None:
            row = NewsTr(item_id=item_id, kind=rec.get("kind", "mt"), engine=rec.get("engine", ""), at=rec.get("at") or datetime.now(_UTC), n_units=0, n_fail=0)
            db.add(row)
        elif row.kind == "hq" and rec.get("kind") != "hq":
            continue      # 高品質の訳は軽いエンジンで上書きしない
        if rec.get("reset"):
            row.n_units = row.n_fail = 0
        for f in ("kind", "engine", "title_ja", "summary_ja", "summary_src", "summary_state", "body_ja"):
            if f in rec and rec[f] is not None:
                setattr(row, f, rec[f])
        if rec.get("at"):
            row.at = rec["at"]
        row.n_units = int(row.n_units or 0) + int(rec.get("add_units", 0))
        row.n_fail = int(row.n_fail or 0) + int(rec.get("add_fail", 0))
        done += 1
    if trcache:
        now = datetime.now(_UTC)
        items = list(trcache.items())
        for i in range(0, len(items), 400):
            db.execute(sqlite_insert(NewsTrCache).values([{"h": h, "ja": ja, "at": now} for h, ja in items[i:i + 400]]).on_conflict_do_nothing(index_elements=["h"]))
        total = db.scalar(select(func.count()).select_from(NewsTrCache)) or 0
        db.execute(delete(NewsTrCache).where(NewsTrCache.at < now - timedelta(days=CACHE_KEEP_DAYS)))
        if total > CACHE_MAX_ROWS:
            old = db.scalars(select(NewsTrCache.h).order_by(NewsTrCache.at.asc()).limit(total - CACHE_MAX_ROWS)).all()
            for i in range(0, len(old), 500):
                db.execute(delete(NewsTrCache).where(NewsTrCache.h.in_(old[i:i + 500])))
    db.flush()
    return done


def _iso(value: datetime | None) -> str:
    return value.replace(tzinfo=_UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def get_row(db, item_id: int) -> dict | None:
    """記事 1 件の機械翻訳（保存済みと、まだ書いていない分を重ねる）。無ければ None。"""
    row = db.get(NewsTr, item_id)
    base = {c.key: getattr(row, c.key) for c in NewsTr.__table__.columns} if row is not None else {}
    pend = writer.get().pending_translation(item_id)
    if pend and not (base.get("kind") == "hq" and pend.get("kind") != "hq"):
        for k, v in pend.items():
            if k in ("add_units", "add_fail", "reset", "body_at"):
                continue
            if v is not None:
                base[k] = v
    return base or None


def attach(db, rows: list[dict]) -> list[dict]:
    """一覧の各記事に mt（題・概要の機械翻訳）を付ける。LLM の日本語の概要が無い記事は、機械翻訳の概要を summary に出す（元は summary_original）。"""
    ids = [r["id"] for r in rows if r.get("kind") != "event"]
    if not ids:
        return rows
    found: dict[int, dict] = {}
    for i in range(0, len(ids), 500):
        for row in db.scalars(select(NewsTr).where(NewsTr.item_id.in_(ids[i:i + 500]))):
            found[row.item_id] = {c.key: getattr(row, c.key) for c in NewsTr.__table__.columns}
    coord = writer.get()
    for r in rows:
        if r.get("kind") == "event":
            continue
        rec = found.get(r["id"])
        pend = coord.pending_translation(r["id"])
        if pend and not (rec and rec.get("kind") == "hq" and pend.get("kind") != "hq"):
            rec = {**(rec or {}), **{k: v for k, v in pend.items() if v is not None and k not in ("add_units", "add_fail", "reset", "body_at")}}
        if not rec or rec.get("kind") == "ja" or not (rec.get("title_ja") or rec.get("summary_ja") or rec.get("body_ja")):
            continue
        mt = {"title": rec.get("title_ja") or "", "summary": rec.get("summary_ja") or "", "engine": rec.get("engine", ""), "hq": rec.get("kind") == "hq",
              "at": _iso(rec["at"]) if isinstance(rec.get("at"), datetime) else str(rec.get("at") or ""), "body": bool(rec.get("body_ja")),
              "partial": int(rec.get("n_fail") or 0) > 0}
        r["mt"] = mt
        if mt["summary"] and not r.get("translated"):      # LLM の日本語の概要が無いときだけ、機械翻訳の概要を出す
            r["summary_original"] = r.get("summary", "")
            r["summary"], r["translated"], r["mt_summary"] = mt["summary"], True, True
    return rows


def counts(db) -> dict:
    """診断: 保存した翻訳の数・今日の数・検査で落ちた率・文の覚えの数。"""
    since = datetime.now(_UTC).astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(_UTC).replace(tzinfo=None)
    by_kind = {k: int(n) for k, n in db.execute(select(NewsTr.kind, func.count()).group_by(NewsTr.kind))}
    n_title = db.scalar(select(func.count()).where(NewsTr.title_ja != "")) or 0
    n_body = db.scalar(select(func.count()).where(NewsTr.body_ja != "")) or 0
    today = db.scalar(select(func.count()).where(NewsTr.at >= since, NewsTr.kind != "ja")) or 0
    units, fails = db.execute(select(func.coalesce(func.sum(NewsTr.n_units), 0), func.coalesce(func.sum(NewsTr.n_fail), 0))).one()
    engines = {e or "?": int(n) for e, n in db.execute(select(NewsTr.engine, func.count()).where(NewsTr.kind != "ja").group_by(NewsTr.engine))}
    return {"rows": by_kind, "titles": int(n_title), "bodies": int(n_body), "today": int(today), "units": int(units), "fails": int(fails),
            "fail_pct": round(int(fails) * 100 / int(units), 1) if units else None, "engines": engines,
            "cache": int(db.scalar(select(func.count()).select_from(NewsTrCache)) or 0)}
