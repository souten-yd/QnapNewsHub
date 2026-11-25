"""点数の集計と Markdown の表。"""
from __future__ import annotations

from . import metrics
from .build_questions import TYPE_LABEL, TYPES
from .runner import PATH_LABEL, PATHS

KIND_COLS = [("doc_r10", "文書 R@10（文書だけ並べ直し）"), ("ent_r20", "物 R@20"), ("clm_r20", "主張 R@20"), ("rel_r20", "関係 R@20")]
COLS = [("r5", "R@5"), ("r10", "R@10"), ("r20", "R@20"), ("mrr", "MRR"), ("ndcg10", "nDCG@10"), ("doc_r10", "文書だけ R@10")]


def aggregate(rows: list[dict], qtype: str | None = None) -> dict[str, dict]:
    sel = [r for r in rows if qtype in (None, r["type"])]
    out: dict[str, dict] = {}
    for p in PATHS:
        ps = [r["paths"][p] for r in sel]
        a = {"n": len(ps)}
        for key, _ in COLS + KIND_COLS:
            vals = [x[key] for x in ps if x[key] is not None]
            a[key] = metrics.mean(vals) if vals else None
        a["ctx_tokens"] = metrics.mean([x["ctx_tokens"] for x in ps])
        a["ctx_density"] = metrics.mean([x["ctx_density"] for x in ps])
        a["ctx_cover"] = metrics.mean([x["ctx_cover"] for x in ps])
        ms = [x["ms"] for x in ps]
        a["p50"], a["p95"] = metrics.percentile(ms, 0.5), metrics.percentile(ms, 0.95)
        out[p] = a
    return out


def _f(v, d: int = 3) -> str:
    return "-" if v is None else f"{v:.{d}f}"


def render(rows: list[dict], info: dict) -> str:
    L: list[str] = []
    types = [t for t in TYPES if any(r["type"] == t for r in rows)]
    L += ["# 検索の評価の物差し（結果）", "", f"- 問い: {len(rows)} 問（" + "・".join(f"{TYPE_LABEL[t]} {sum(1 for r in rows if r['type'] == t)}" for t in types) + "）",
          f"- 評価のハブの規模: {info.get('db', '')}", "- 点数は問いごとに出して平均（マクロ平均）。正解が 1 件も取れない問いは 0 点。時間は 1 問ぶんの合計（呼び出しごとにキャッシュを捨てる）。", ""]
    allagg = aggregate(rows)
    L += ["## 1. 全体", "", "| 道 | " + " | ".join(n for _, n in COLS) + " | 文脈のトークン | 文脈の密度（正解/1k トークン） | 文脈に入った正解の割合 | P50 ms | P95 ms |", "|---|" + "---:|" * (len(COLS) + 5)]
    for p in PATHS:
        a = allagg[p]
        L.append(f"| {PATH_LABEL[p]} | " + " | ".join(_f(a[k]) for k, _ in COLS) + f" | {a['ctx_tokens']:.0f} | {_f(a['ctx_density'], 2)} | {_f(a['ctx_cover'])} | {a['p50']:.0f} | {a['p95']:.0f} |")
    L.append("")
    L += ["### 参照の種類ごとの再現率（正解にその種類を持つ問いだけの平均）", "", "| 道 | " + " | ".join(n for _, n in KIND_COLS) + " |", "|---|" + "---:|" * len(KIND_COLS)]
    for p in PATHS:
        L.append(f"| {PATH_LABEL[p]} | " + " | ".join(_f(allagg[p][k]) for k, _ in KIND_COLS) + " |")
    L.append("")
    by = {t: aggregate(rows, t) for t in types}
    n = 2
    for key, title, d in [("r10", "Recall@10", 3), ("r20", "Recall@20", 3), ("mrr", "MRR", 3), ("ndcg10", "nDCG@10", 3), ("ctx_density", "文脈の密度（正解の根拠の数 / 1,000 トークン）", 2),
                          ("ctx_cover", "文脈に入った正解の割合", 3), ("p50", "時間 P50（ms）", 0), ("p95", "時間 P95（ms）", 0)]:
        L += [f"## {n}. 問いの種類ごと: {title}", "", "| 道 | " + " | ".join(TYPE_LABEL[t] for t in types) + " |", "|---|" + "---:|" * len(types)]
        for p in PATHS:
            L.append(f"| {PATH_LABEL[p]} | " + " | ".join(_f(by[t][p][key], d) for t in types) + " |")
        L.append("")
        n += 1
    L += [f"## {n}. 弱い所（自動の抜き出し）", ""]
    for t in types:
        sel = [r for r in rows if r["type"] == t]
        best = max(PATHS, key=lambda p: by[t][p]["r20"] or 0)
        zero = [r for r in sel if all(r["paths"][p]["r20"] == 0 for p in PATHS)]
        L.append(f"- **{TYPE_LABEL[t]}**（{len(sel)} 問）: 最良の道は {PATH_LABEL[best]}（R@20 {_f(by[t][best]['r20'])}）。どの道でも R@20 が 0 の問い {len(zero)} 問" + (": " + "・".join(r["id"] for r in zero) if zero else ""))
    L += ["", f"## {n + 1}. 問いごとの R@10（①〜⑤）", "", "| id | 種類 | 問い | 正解数 | ① | ② | ③ | ④ | ⑤ |", "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        L.append(f"| {r['id']} | {TYPE_LABEL[r['type']]} | {r['q']} | {r['gold_n']} | " + " | ".join(f"{r['paths'][p]['r10']:.2f}" for p in PATHS) + " |")
    L.append("")
    return "\n".join(L)
