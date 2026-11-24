"""取得先の CSV・OPML の取り込みと書き出し（取得先の管理の画面）。

- 取り込み: 読んで「確認の表」にするところまでは何も保存しない（preview）。表の各行は new（足せる）・duplicate（同じ URL がすでにある・同じファイルの中で 2 回目）・invalid（URL が使えない）。
- CSV は利用者の一覧の形に合わせ、列の名前の揺れ（大文字小文字・空白・全角・日本語の名前）を許す。区切りはカンマ・タブ・セミコロンを見分け、
  文字コードは UTF-8（BOM つきも）で読めなければ Shift_JIS（Excel の日本語の CSV）。OPML は他の RSS リーダーの書き出しと互換（xmlUrl・text/title・入れ子のフォルダ＝分類）。
- OPML は外部の実体・DTD を持つものを断る（XML の守り。ARCHITECTURE 13）。
- 書き出し: OPML（RSS・一覧の巡回・GitHub/Codeberg の Release のフィード・フォーラムの最新・Reddit）と CSV（同じ内容＋分類・出どころ・間隔・有効）。
  書き出した CSV・OPML をそのまま取り込めば同じ一覧に戻る（往復）。
"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

from qnapnewshub import sourcemgr, sources
from qnapnewshub.dedup import normalize_url

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000

_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "title", "text", "sitename", "feedname", "label", "site", "名前", "名称", "サイト名", "サイト", "タイトル", "取得先", "取得先名", "媒体", "媒体名"),
    "url": ("url", "feed", "feedurl", "xmlurl", "rss", "rssurl", "atom", "link", "address", "uri", "アドレス", "フィード", "フィードurl", "フィードのurl", "rssのurl", "リンク"),
    "html_url": ("htmlurl", "siteurl", "website", "homepage", "web", "サイトurl", "サイトのurl", "ホームページ"),
    "category": ("category", "categories", "group", "groups", "folder", "tag", "tags", "genre", "カテゴリ", "カテゴリー", "分類", "分野", "グループ", "フォルダ", "ジャンル"),
    "origin": ("kind", "origin", "source", "sourcetype", "種類", "出どころ", "出どころの種類", "種別", "区分"),
    "type": ("type", "format", "形式", "タイプ"),
    "interval": ("interval", "intervalminutes", "minutes", "間隔", "取得間隔", "取得間隔分", "間隔分"),
    "enabled": ("enabled", "active", "enable", "on", "有効", "使用", "取得する"),
}
_ORIGIN_WORDS = {
    "official": ("official", "公式", "一次", "メーカー", "企業", "vendor"), "press": ("press", "news", "media", "報道", "ニュース", "メディア", "新聞"),
    "blog": ("blog", "ブログ"), "community": ("community", "forum", "social", "コミュニティ", "フォーラム", "sns", "掲示板"),
    "paper": ("paper", "papers", "research", "論文", "研究", "preprint"), "other": ("other", "その他", "他"),
}
_TRUE = {"1", "true", "yes", "y", "on", "有効", "はい", "○", "〇", "✓", "有", "enabled", "t"}
_FALSE = {"0", "false", "no", "n", "off", "無効", "いいえ", "×", "✕", "無", "disabled", "f", "停止"}


class ImportError_(ValueError):
    """取り込めないファイル（利用者に見せてよい文）。"""


def _key(text: str) -> str:
    return re.sub(r"[\s_\-・（）()　]+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


def _column_map(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    keys = [_key(h) for h in header]
    for field, names in _ALIASES.items():
        wanted = {_key(n) for n in names}
        for i, k in enumerate(keys):
            if k in wanted and field not in found and i not in found.values():
                found[field] = i
    return found


def decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ImportError_("ファイルが大きすぎます（2MB まで）")
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_("文字コードを読めません（UTF-8 か Shift_JIS にしてください）")


def decode_b64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ImportError_("ファイルを読めません") from exc


def detect(text: str) -> str:
    head = text.lstrip()[:400].lower()
    if head.startswith("<?xml") or head.startswith("<opml") or "<opml" in head:
        return "opml"
    return "csv"


def origin_of(value: str) -> str:
    low = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not low:
        return ""
    for origin, words in _ORIGIN_WORDS.items():
        if low in {w.lower() for w in words}:
            return origin
    return ""


def _flag(value: str) -> bool | None:
    low = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return None


def _minutes(value: str) -> int:
    try:
        n = int(float(unicodedata.normalize("NFKC", str(value or "")).strip()))
    except ValueError:
        return 0
    return n if sources.INTERVAL_MIN <= n <= sources.INTERVAL_MAX else 0


def parse_csv(text: str) -> list[dict]:
    """CSV を行の一覧にする（正規化の前）。1 行目が見出し。url の列が無ければ ImportError_。"""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise ImportError_("空のファイルです")
    cols = _column_map(rows[0])
    if "url" not in cols and "html_url" not in cols:
        # 見出しが無く、1 列目が URL に見える一覧も許す
        if rows[0] and re.match(r"https?://", rows[0][0].strip()):
            cols, rows = {"url": 0, **({"name": 1} if len(rows[0]) > 1 else {})}, [["url"], *rows]
        else:
            raise ImportError_("URL の列が見つかりません（列の名前は url・feed・xmlUrl・フィード・アドレス などに対応しています）")
    out = []
    for n, row in enumerate(rows[1:MAX_ROWS + 1], start=2):
        def cell(field: str) -> str:
            i = cols.get(field)
            return row[i].strip() if i is not None and i < len(row) else ""
        out.append({"line": n, "name": cell("name"), "url": cell("url") or cell("html_url"), "category": cell("category"), "origin": origin_of(cell("origin")),
                    "type": cell("type").lower(), "interval": _minutes(cell("interval")), "enabled": _flag(cell("enabled"))})
    return out


def parse_opml(text: str) -> list[dict]:
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        raise ImportError_("DTD・外部の実体を含む OPML は読みません")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ImportError_(f"OPML として読めません: {exc}") from exc
    body = next((e for e in root.iter() if e.tag.rsplit("}", 1)[-1].lower() == "body"), None)
    if body is None:
        raise ImportError_("OPML の body がありません")
    out: list[dict] = []

    def walk(node, folder: str) -> None:
        for child in node:
            if child.tag.rsplit("}", 1)[-1].lower() != "outline" or len(out) >= MAX_ROWS:
                continue
            attrs = {k.rsplit("}", 1)[-1].lower(): v for k, v in child.attrib.items()}
            label = attrs.get("text") or attrs.get("title") or ""
            url = attrs.get("xmlurl") or ""
            if url or attrs.get("type", "").lower() in ("rss", "atom", "page"):
                out.append({"line": len(out) + 1, "name": label, "url": url or attrs.get("htmlurl", ""), "category": attrs.get("category") or folder,
                            "origin": origin_of(attrs.get("origin", "")), "type": attrs.get("type", "").lower(), "interval": _minutes(attrs.get("intervalminutes", "")),
                            "enabled": _flag(attrs.get("enabled", ""))})
            elif len(child):
                walk(child, label or folder)

    walk(body, "")
    if not out:
        raise ImportError_("取得先（xmlUrl を持つ項目）が見つかりません")
    return out


def feed_key(url: str) -> str:
    """同じ URL かの判定の鍵（http/https・www・末尾の /・追跡の引数の違いを同じとみなす）。"""
    return normalize_url(url).lower()


def build_preview(cfg: dict, raw_rows: list[dict]) -> dict:
    """行ごとに {line, name, url, category, origin, origin_guess, interval, enabled, kind, status, reason}。status は new・duplicate・invalid。
    すでにある取得先（RSS・一覧の巡回）と、同じファイルの中で先に出たものは duplicate。"""
    have = {feed_key(f["url"]) for f in cfg["rss"]} | {feed_key(f["url"]) for f in cfg.get("pages", [])}
    seen: set[str] = set()
    rows = []
    for raw in raw_rows:
        url, note = str(raw.get("url") or "").strip(), ""
        if url.startswith("http://"):
            url, note = "https://" + url[len("http://"):], "http を https に直しました"
        kind = "page" if raw.get("type") == "page" else "rss"
        row = {"line": raw.get("line", 0), "name": (raw.get("name") or "").strip()[:60], "url": url[:500], "category": (raw.get("category") or "").strip()[:40],
               "origin": raw.get("origin") or "", "interval": raw.get("interval") or 0, "enabled": raw.get("enabled") is not False, "kind": kind,
               "status": "new", "reason": note}
        try:
            sources.https_url(url)
        except sources.ConfigError:
            row.update(status="invalid", reason="https の URL ではありません" if url else "URL がありません")
            rows.append(row)
            continue
        key = feed_key(url)
        if key in have:
            row.update(status="duplicate", reason="すでに登録されています")
        elif key in seen:
            row.update(status="duplicate", reason="ファイルの中で重複しています")
        else:
            seen.add(key)
        row["name"] = row["name"] or urlsplit(url).hostname or "RSS"
        row["origin_guess"] = sourcemgr.infer_origin(kind, url, row["name"])
        rows.append(row)
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("new", "duplicate", "invalid")}
    room = max(0, sources.MAX_PER_LIST - len(cfg["rss"]))
    return {"rows": rows, "counts": counts, "room": room, "total": len(rows)}


def preview(cfg: dict, data: bytes, fmt: str = "auto") -> dict:
    text = decode(data)
    kind = detect(text) if fmt in ("", "auto") else fmt
    if kind not in ("csv", "opml"):
        raise ImportError_("形式は csv か opml です")
    rows = parse_opml(text) if kind == "opml" else parse_csv(text)
    return {"format": kind, **build_preview(cfg, rows)}


# ---- 書き出し ----

def _feed_url(e: dict) -> str:
    kind, value = e["kind"], e["value"]
    return {"rss": value, "page": value, "github": f"https://github.com/{value}/releases.atom", "codeberg": f"https://codeberg.org/{value}/releases.rss",
            "discourse": f"https://{value}/latest.rss", "reddit": f"https://www.reddit.com/r/{value}/.rss"}.get(kind, "")


def export_rows(cfg: dict) -> list[dict]:
    """書き出す行。URL を持たない取得先（Hacker News・Hugging Face）は含めない。"""
    out = []
    for e in sourcemgr.entries(cfg):
        url = _feed_url(e)
        if url:
            out.append({"name": e["name"], "url": url, "category": e["category"], "origin": e["origin"], "type": "page" if e["kind"] == "page" else "rss",
                        "interval": e["interval"], "enabled": e["enabled"]})
    return out


def export_csv(cfg: dict) -> str:
    buf = io.StringIO()
    writer_ = csv.writer(buf, lineterminator="\n")
    writer_.writerow(["name", "url", "category", "origin", "type", "interval", "enabled"])
    for r in export_rows(cfg):
        writer_.writerow([r["name"], r["url"], r["category"], r["origin"], r["type"], r["interval"] or "", "true" if r["enabled"] else "false"])
    return "﻿" + buf.getvalue()      # Excel が UTF-8 と分かるよう BOM を付ける


def export_opml(cfg: dict, title: str = "QNAP News Hub") -> str:
    folders: dict[str, list[dict]] = {}
    for r in export_rows(cfg):
        folders.setdefault(r["category"], []).append(r)

    def outline(r: dict, pad: str) -> str:
        attrs = [("type", "rss" if r["type"] == "rss" else "page"), ("text", r["name"]), ("title", r["name"]), ("xmlUrl", r["url"]), ("origin", r["origin"])]
        if r["interval"]:
            attrs.append(("intervalMinutes", str(r["interval"])))
        if not r["enabled"]:
            attrs.append(("enabled", "false"))
        return pad + "<outline " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs) + "/>"

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">', f"  <head><title>{escape(title)}</title></head>", "  <body>"]
    for folder, rows in sorted(folders.items(), key=lambda kv: kv[0]):
        if folder:
            lines.append(f"    <outline text={quoteattr(folder)} title={quoteattr(folder)}>")
            lines += [outline(r, "      ") for r in rows]
            lines.append("    </outline>")
        else:
            lines += [outline(r, "    ") for r in rows]
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)
