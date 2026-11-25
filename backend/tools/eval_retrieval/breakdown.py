"""検索の評価の内訳（前後の比較）。`python -m tools.eval_retrieval.breakdown --questions <json> --raw <ラベル>=<raw json>...`

目的: 検索の変更（日本語の短い語・主張の文を種の検索へ・文脈の前置）が効いたかを、問いの性質ごとに前後で並べる。全体の平均だけだと、
効く問い（3 文字未満の語を含む・事実を問う）が全体に薄まって見えないため。
根拠: 問いの集めの 60 問・85 問は規則の雛形で作ったので、3 文字未満の語の多くは雛形の語（「関係」「報道」…）で、内容の語ではない。
そこで「3 文字未満の語が内容（keywords）にある」「雛形の語だけ」「無い」を分けて数える（雛形の語は検索の語にもなるが、探したい物の名前ではない）。
問いの種類 fact（事実を問う）とそれ以外も分ける。--raw は eval-retrieval の --raw の出力（問いごとの生の点数）。評価の置き場へ出す（リポジトリに入れない）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import metrics
from .runner import PATH_LABEL

SHOW = ("seeds", "neighbors", "meet", "context")


def short_terms(q: str) -> list[str]:
    from qnapnewshub import retrieval

    return retrieval._split(retrieval.query_terms(q))[2]


def classify(q: dict) -> dict[str, bool]:
    sh = short_terms(q["q"])
    content = [s for s in sh if any(s in k for k in q.get("keywords", []))]
    return {"短い語（内容）": bool(content), "短い語（雛形の語だけ）": bool(sh) and not content, "短い語なし": not sh,
            "事実を問う": q["type"] == "fact", "事実以外": q["type"] != "fact"}


def load_questions(paths: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in paths:
        for q in json.loads(Path(p).expanduser().read_text(encoding="utf-8"))["questions"]:
            out[q["id"]] = q
    return out


def render(runs: dict[str, list[dict]], qs: dict[str, dict]) -> str:
    L: list[str] = []
    groups = ["全体", "短い語（内容）", "短い語（雛形の語だけ）", "短い語なし", "事実を問う", "事実以外"]
    for g in groups:
        L += [f"### {g}", "", "| 道 | ラベル | 問い | R@10 | R@20 | MRR | nDCG@10 | 密度 | P50 ms | P95 ms |", "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for p in SHOW:
            for label, rows in runs.items():
                sel = [r for r in rows if r["id"] in qs and (g == "全体" or classify(qs[r["id"]])[g])]
                if not sel:
                    continue
                v = lambda k: metrics.mean([r["paths"][p][k] for r in sel])  # noqa: E731
                ms = [r["paths"][p]["ms"] for r in sel]
                L.append(f"| {PATH_LABEL[p]} | {label} | {len(sel)} | {v('r10'):.3f} | {v('r20'):.3f} | {v('mrr'):.3f} | {v('ndcg10'):.3f} | {v('ctx_density'):.2f} | "
                         f"{metrics.percentile(ms, 0.5):.0f} | {metrics.percentile(ms, 0.95):.0f} |")
        L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval-breakdown")
    ap.add_argument("--questions", required=True, help="問いの集めの JSON 1 つ（id が集めごとに重なるので、集めごとに呼ぶ）")
    ap.add_argument("--raw", nargs="+", required=True, help="ラベル=raw の JSON（前後を並べる）")
    a = ap.parse_args(argv)
    runs = {}
    for spec in a.raw:
        label, _, path = spec.partition("=")
        runs[label] = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    print(render(runs, load_questions([a.questions])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
