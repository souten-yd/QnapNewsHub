"""条件つきの数値の比較（ベンチマークの DB。ARCHITECTURE 12 章・v0.7 の続き）。

- **条件**: 数値が測られた条件を 6 つの欄で持つ（`COND_KEYS`）: model（測ったモデル）・quant（量子化）・hw（GPU・端末）・engine（エンジン・バックエンド）・ctx（文脈の長さ）・version（版）。
  取り出しは**既存の下ごしらえの facts に cond の欄を足した**もの（呼び出しは増えない。プロンプト ex5）。LLM が書いた条件は、原文（題・抜粋）にそのまま出る値だけ残す（`clean_cond`）。
  古い主張（cond の欄が無い）・LLM が書かなかった欄は、主張の文から規則（`parse_cond`）で補う。補えない欄は「不明」のまま（推測しない）。
- **同じ指標でも条件の違うものを平均しない**: `compare` は（物・単位・条件）ごとの組に分けて、組の中の新しい順の点・最小・最大・最新だけを返す。平均は出さない。
  `cond` の指定（例 `hw:R9700,quant:Q4_K_M`）があれば、全部の欄が合う組（matching）・合わない組（other）・指定の欄が不明の組（unspecified）に分ける。
- 数値の出どころは claims（値のある主張。条件は claims.cond）と metric_points（connector の時系列。条件は metric_points.cond の文字列）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

COND_KEYS = ("model", "quant", "hw", "engine", "ctx", "version")
COND_LABEL = {"model": "モデル", "quant": "量子化", "hw": "GPU・端末", "engine": "エンジン", "ctx": "文脈の長さ", "version": "版"}
COND_MAX_CHARS = 60
COMPARE_THINGS_MAX = 5
GROUP_POINTS_MAX = 6

_QUANT = re.compile(r"\b(?:i?q[2-8]_(?:k_)?(?:xxs|xs|s|m|l|0|1)|iq[1-4]_(?:xxs|xs|s|m|nl)|[fb]f?16|fp(?:4|8|16|32)|nvfp4|mxfp4|int[48]|[2-8][- ]?bit)\b|[2-8] ?ビット", re.I)
_HW = re.compile(r"\b(?:rtx ?(?:pro )?\d{4}(?: ?ti)?(?: ?super)?|gtx ?\d{3,4}|rx ?\d{4}(?: ?xt)?|r9700|mi\d{2,3}x?|[hab]\d{2,3}0?|gb\d{2,3}|m[1-9] ?(?:pro|max|ultra)?|a1[0-9] ?pro|snapdragon ?\w*(?: ?elite)?|"
                 r"arc ?[ab]\d{3}|radeon ?(?:pro )?\w+ ?\d*|strix ?halo|dgx ?spark|jetson ?\w+|ryzen ?ai ?\w*)\b|骁龙 ?\w+(?: ?elite)?|スナドラ ?\w*", re.I)
_ENGINE = re.compile(r"\b(?:llama\.cpp|vllm|sglang|ollama|mlx|tensorrt-?llm|onnx ?runtime|ctranslate2|lm studio|exllama\w*|transformers|rocm|cuda|vulkan|metal|migraphx|openvino|directml|"
                     r"hexagon|coreml|webgpu|candle|burn|mistral\.rs|strata|radiance)\b", re.I)
_CTX = re.compile(r"(?:\b(\d{1,4}(?:\.\d)?) ?([km]) ?(?:tokens? )?(?:ctx|context|コンテキスト)|(?:ctx|context(?: length| window)?|コンテキスト(?:長|窓|ウィンドウ)?|文脈(?:の)?長さ?)[ :=は]*(\d{1,4}(?:\.\d)?) ?([km])\b)", re.I)
_VERSION = re.compile(r"\bv?(\d+\.\d+(?:\.\d+){0,2})\b(?= ?(?:版|release|で|にて|時点|以降|以上))|\bb(\d{4,5})\b")
_UNIT = re.compile(r"(tok(?:en)?s?/s(?:ec)?|t/s|tps|ms|秒|倍|％|%|gb|mb|gib|w|usd|ドル|円|fps|x\b)", re.I)
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def fold(text: object) -> str:
    return re.sub(r"[\s_\-]+", "", unicodedata.normalize("NFKC", str(text or "")).casefold())


def _clip(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:COND_MAX_CHARS]


def parse_cond(text: str) -> dict[str, str]:
    """文から条件の欄を規則で取る（見つかった欄だけ。最初の 1 つ）。model は規則では取らない（物の名前と区別できない）。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    out: dict[str, str] = {}
    for key, pat in (("quant", _QUANT), ("hw", _HW), ("engine", _ENGINE)):
        m = pat.search(s)
        if m:
            out[key] = _clip(m.group(0))
    m = _CTX.search(s)
    if m:
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        out["ctx"] = f"{num}{unit.upper()}"
    m = _VERSION.search(s)
    if m:
        out["version"] = _clip(m.group(1) or f"b{m.group(2)}")
    return out


def clean_cond(raw: object, source_text: str = "") -> dict[str, str]:
    """LLM が書いた条件を整える。知らない欄・空・長すぎる値は捨てる。source_text（原文の題と抜粋）が渡されたら、原文に出ない値も捨てる（書かれていない条件を作らせない）。"""
    if not isinstance(raw, dict):
        return {}
    src = fold(source_text)
    out: dict[str, str] = {}
    for key in COND_KEYS:
        v = raw.get(key)
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            continue
        v = _clip(v)
        if len(fold(v)) < 2 or fold(v) in {"unknown", "不明", "none", "null", "なし", "na", "n/a"}:
            continue
        if source_text and fold(v) not in src:
            continue
        out[key] = v
    return out


def cond_of(stored: str | dict | None, text: str = "") -> dict[str, str]:
    """主張 1 件の条件: 保存された cond（JSON）＋ 文からの規則（保存された欄が先）。"""
    d: dict = {}
    if isinstance(stored, dict):
        d = stored
    elif stored:
        try:
            raw = json.loads(stored)
            d = raw if isinstance(raw, dict) else {}
        except ValueError:
            d = {}
    base = {k: _clip(v) for k, v in d.items() if k in COND_KEYS and _clip(v)}
    for k, v in parse_cond(text).items():
        base.setdefault(k, v)
    return base


def cond_json(cond: dict[str, str]) -> str:
    return json.dumps({k: cond[k] for k in COND_KEYS if cond.get(k)}, ensure_ascii=False) if cond else ""


def parse_filter(raw: str) -> dict[str, str]:
    """`hw:R9700,quant:Q4_K_M`（`=` でもよい）→ {hw: R9700, quant: Q4_K_M}。知らない欄は無視。区切りの無い語は、どの欄でもよい語として `*` に入れる。"""
    out: dict[str, str] = {}
    for part in re.split(r"[,、;]", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        key, sep, val = re.split(r"([:=])", part, maxsplit=1) if re.search(r"[:=]", part) else (part, "", "")
        key = key.strip().lower()
        if sep and key in COND_KEYS and val.strip():
            out[key] = val.strip()
        elif not sep:
            out["*"] = part
    return out


def _field_match(want: str, have: str) -> bool:
    w, h = fold(want), fold(have)
    return bool(w) and bool(h) and (w == h or w in h or h in w)


def cond_status(filter_: dict[str, str], cond: dict[str, str]) -> str:
    """matching（指定の欄が全部合う）・other（どれかが違う）・unspecified（どれかが不明で、違う欄は無い）。指定が空なら matching。"""
    if not filter_:
        return "matching"
    unknown = False
    for key, want in filter_.items():
        if key == "*":
            if not any(_field_match(want, v) for v in cond.values()):
                unknown = True if not cond else unknown
                if cond:
                    return "other"
            continue
        have = cond.get(key, "")
        if not have:
            unknown = True
        elif not _field_match(want, have):
            return "other"
    return "unspecified" if unknown else "matching"


def parse_value(value: str, text: str = "") -> tuple[float | None, str]:
    """値の文字列（"383.7"・"91%"・"5x"）と文から (数, 単位)。数が読めなければ (None, "")。"""
    s = unicodedata.normalize("NFKC", str(value or ""))
    m = _NUM.search(s)
    if not m:
        return None, ""
    try:
        num = float(m.group(0).replace(",", ""))
    except ValueError:
        return None, ""
    tail = s[m.end():].strip()
    um = _UNIT.search(tail) if tail else None
    if um is None:
        um = _UNIT.search(unicodedata.normalize("NFKC", str(text or "")))
    unit = um.group(1).lower() if um else ""
    unit = {"tokens/s": "tok/s", "token/s": "tok/s", "tokens/sec": "tok/s", "tok/sec": "tok/s", "t/s": "tok/s", "tps": "tok/s", "％": "%", "x": "倍"}.get(unit, unit)
    return num, unit


def _label(cond: dict[str, str]) -> str:
    return " / ".join(f"{COND_LABEL[k]} {cond[k]}" for k in COND_KEYS if cond.get(k)) or "条件の記載なし"


def _group_key(cond: dict[str, str]) -> tuple:
    return tuple((k, fold(cond[k])) for k in COND_KEYS if cond.get(k))


def _metric_match(metric: str, kind: str, text: str, unit: str) -> bool:
    if not metric:
        return True
    m = fold(metric)
    return m in {fold(kind), fold(unit)} or m in fold(text) or fold(metric) in fold(unit)


def resolve_things(db: Session, spec: str, scopes: list[str]) -> list:
    """`thing=` の指定（id か名前。カンマで最大 5）→ Entity。読めない・無いものは返さない。"""
    from qnapnewshub.db import Entity
    from qnapnewshub.extract import normalize_key

    out, seen = [], set()
    for part in [p.strip() for p in str(spec or "").split(",") if p.strip()][:COMPARE_THINGS_MAX]:
        ent = db.get(Entity, int(part)) if part.isdigit() else db.scalar(select(Entity).where(Entity.key == normalize_key(part)))
        if ent is not None and ent.sensitivity in scopes and ent.id not in seen:
            seen.add(ent.id)
            out.append(ent)
    return out


def _claim_rows(db: Session, ent, scopes: list[str], metric: str) -> list[dict]:
    from qnapnewshub import kg_view, things
    from qnapnewshub.db import Claim

    rows = []
    for c in db.scalars(select(Claim).where(Claim.entity_id == ent.id, Claim.value != "", Claim.sensitivity.in_(scopes)).order_by(Claim.id.desc()).limit(300)):
        num, unit = parse_value(c.value, c.text)
        if num is None or not _metric_match(metric, c.kind, c.text, unit):
            continue
        rows.append({"source": "claim", "claim_id": c.id, "kind": c.kind, "text": c.text[:160], "value": num, "raw": c.value, "unit": unit, "cond": cond_of(c.cond, c.text),
                     "at": c.stated_at or c.created_at})
    if not rows:
        return rows
    ev = kg_view._claims_with_evidence(db, ent.id, scopes, limit=300)      # 根拠の文書（出どころの印・原文に合うか）
    by_id = {e["id"]: e for e in ev}
    for r in rows:
        e = by_id.get(r["claim_id"])
        if not e or not e["evidences"]:
            continue
        d = e["evidences"][0]["document"]
        r["doc"] = d["id"]
        r["src_label"] = things.doc_label(d.get("source", ""), d.get("published_at", ""), d["id"])
        r["url"] = d.get("url", "")
        r["grounded"] = any(x.get("grounded") for x in e["evidences"])
    return rows


def _metric_rows(db: Session, ent, metric: str) -> list[dict]:
    """connector の時系列。subject の最後の / の後ろ（owner/name の name・名前空間 `repo:` の後ろ）が物の名前と同じもの。"""
    import re as _re

    from qnapnewshub.db import MetricPoint
    from qnapnewshub.extract import normalize_key

    if not ent.name:
        return []
    pat = "%" + _re.sub(r"([%_\\])", r"\\\1", ent.name)
    q = select(MetricPoint).where(MetricPoint.subject.like(pat, escape="\\"))
    if metric:
        q = q.where(MetricPoint.metric == metric)
    rows = []
    for p in db.scalars(q.order_by(MetricPoint.at.desc()).limit(600)):
        if normalize_key(re.split(r"[/:]", p.subject)[-1]) != ent.key:
            continue
        cond = {k: v for k, v in parse_cond(p.cond).items()}
        rows.append({"source": "metric", "claim_id": None, "kind": p.metric, "text": f"{p.metric}（{p.cond}）" if p.cond else p.metric, "value": float(p.value), "raw": f"{p.value:g}",
                     "unit": p.unit, "cond": cond, "cond_note": p.cond, "at": p.at, "src_label": p.source, "url": "", "grounded": True, "subject": p.subject, "metric": p.metric})
    return rows


def _iso(at) -> str:
    if not isinstance(at, datetime):
        return ""
    return at.strftime("%Y-%m-%d")


def compare(db: Session, ents: list, scopes: list[str], *, metric: str = "", cond: str = "") -> dict:
    """物ごと・（単位, 条件）の組ごとに数値を並べる。**平均は出さない**（条件の違う数値を混ぜない）。"""
    flt = parse_filter(cond)
    out_things = []
    for ent in ents:
        rows = _claim_rows(db, ent, scopes, metric) + _metric_rows(db, ent, metric)
        groups: dict[tuple, dict] = {}
        for r in rows:
            key = (r["unit"], r["kind"] if r["source"] == "metric" else "", r.get("metric", ""), _group_key(r["cond"]))
            g = groups.setdefault(key, {"unit": r["unit"], "cond": r["cond"], "cond_label": _label(r["cond"]), "status": cond_status(flt, r["cond"]), "points": []})
            g["points"].append(r)
        out_groups = []
        for g in groups.values():
            pts = sorted(g["points"], key=lambda r: (r["at"] is not None, r["at"] or datetime.min), reverse=True)
            vals = [p["value"] for p in pts]
            out_groups.append({"unit": g["unit"], "cond": g["cond"], "cond_label": g["cond_label"], "status": g["status"], "n": len(pts), "latest": {"value": pts[0]["value"], "at": _iso(pts[0]["at"])},
                               "min": min(vals), "max": max(vals),
                               "points": [{"value": p["value"], "at": _iso(p["at"]), "text": p["text"], "source": p["source"], **({"src": p["src_label"]} if p.get("src_label") else {}),
                                           **({"url": p["url"]} if p.get("url") else {}), **({"grounded": True} if p.get("grounded") else {}),
                                           **({"claim_id": p["claim_id"]} if p.get("claim_id") else {}), **({"doc": p["doc"]} if p.get("doc") else {})} for p in pts[:GROUP_POINTS_MAX]]})
        order = {"matching": 0, "unspecified": 1, "other": 2}
        out_groups.sort(key=lambda g: (order[g["status"]], -g["n"], g["cond_label"]))
        counts = {s: sum(1 for g in out_groups if g["status"] == s) for s in order}
        out_things.append({"id": ent.id, "name": ent.name, "type": ent.type, "groups": out_groups, "counts": counts})
    return {"metric": metric, "cond_filter": flt, "things": out_things,
            "note": "条件の違う数値は平均・合算しない。組（単位と条件が同じもの）の中だけで並べる。条件が不明の組は、条件が同じとは限らない。"}


def compare_text(c: dict, max_groups: int = 6) -> str:
    """LLM に渡す短い Markdown。組ごとに「条件: 最新の値（日付・出どころの印）」。他の条件の組・不明の組は別の節。"""
    lines: list[str] = []
    for t in c["things"]:
        lines.append(f"# {t['name']} (#{t['id']})" + (f" の {c['metric']}" if c["metric"] else ""))
        if not t["groups"]:
            lines.append("- 数値の記録なし")
            continue
        titles = {"matching": "## 指定の条件に合う", "unspecified": "## 条件が不明（指定の条件と同じとは限らない）", "other": "## 別の条件（比べられない）"}
        for status in ("matching", "unspecified", "other"):
            gs = [g for g in t["groups"] if g["status"] == status][:max_groups]
            if not gs:
                continue
            if c["cond_filter"] or status != "matching":
                lines.append(titles[status])
            for g in gs:
                p = g["points"][0]
                span = f"（{g['n']} 件・{g['min']:g}〜{g['max']:g}）" if g["n"] > 1 else ""
                lines.append(f"- {g['cond_label']}: {g['latest']['value']:g}{g['unit']}{span} [{p.get('src', p['source'])}]")
    lines.append(f"注: {c['note']}")
    return "\n".join(lines)
