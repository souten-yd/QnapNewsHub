"""独立した原文注釈と抽出結果・検索結果をオフラインで比較する。
実データはリポジトリに置かない。python -m tools.eval_measure GOLD PRED [--ranking RANKING]
GOLD/PRED: {"documents":[{"id":"d","text":"..."}],"measurements":[
{"id":"m","doc_id":"d","subject":"Foo","metric_id":"llm.decode_tps","value_span":[0,2],
"unit":"tok/s","conditions":{"hw":{"value":"RTX 4090","span":[10,18]}}}]}
GOLD のみ questions: [{"id":"q","relevant":["m"]}]。RANKING: {"q":["m",...]}。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _span(text: str, span: list[int]) -> str:
    if (not isinstance(span, list) or len(span) != 2
            or any(type(x) is not int for x in span)
            or not 0 <= span[0] < span[1] <= len(text)):
        raise ValueError("invalid source span")
    return text[span[0]:span[1]]


def annotations(data: dict, documents: dict[str, str]) -> tuple[Counter, Counter, set[str]]:
    values, conditions, ids = Counter(), Counter(), set()
    for row in data["measurements"]:
        rid = row["id"]
        if rid in ids:
            raise ValueError("duplicate measurement id")
        ids.add(rid)
        doc = row["doc_id"]
        text = documents[doc]
        _span(text, row["value_span"])
        key = (doc, row["subject"], row["metric_id"], tuple(row["value_span"]), row["unit"])
        values[key] += 1
        for name, cond in row.get("conditions", {}).items():
            if cond is None:
                continue
            if _span(text, cond["span"]) != cond["value"]:
                raise ValueError("condition evidence does not match source")
            conditions[(key, name, cond["value"], tuple(cond["span"]))] += 1
    return values, conditions, ids


def scores(gold: Counter, pred: Counter) -> dict:
    tp = sum((gold & pred).values())
    ng, np = sum(gold.values()), sum(pred.values())
    precision = tp / np if np else (1.0 if not ng else 0.0)
    recall = tp / ng if ng else (1.0 if not np else 0.0)
    return {"tp": tp, "fp": np - tp, "fn": ng - tp, "precision": precision,
            "recall": recall, "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def evaluate(gold: dict, pred: dict, ranking: dict | None = None) -> dict:
    docs = {d["id"]: d["text"] for d in gold["documents"]}
    if len(docs) != len(gold["documents"]):
        raise ValueError("duplicate document id")
    gv, gc, ids = annotations(gold, docs)
    pv, pc, _ = annotations(pred, docs)
    out = {"measurement": scores(gv, pv), "condition_link": scores(gc, pc)}
    if ranking is not None:
        recalls, reciprocals, seen = [], [], set()
        for q in gold.get("questions", []):
            qid, relevant = q["id"], set(q["relevant"])
            if qid in seen or not relevant or not relevant <= ids:
                raise ValueError("invalid gold question")
            seen.add(qid)
            hits = list(dict.fromkeys(ranking.get(qid, [])))[:10]
            recalls.append(len(set(hits) & relevant) / len(relevant))
            reciprocals.append(next((1 / i for i, v in enumerate(hits, 1) if v in relevant), 0.0))
        out["retrieval"] = {"questions": len(recalls), "r@10": sum(recalls)/len(recalls) if recalls else None,
                            "mrr@10": sum(reciprocals)/len(reciprocals) if recalls else None}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", type=Path)
    parser.add_argument("pred", type=Path)
    parser.add_argument("--ranking", type=Path)
    args = parser.parse_args()
    load = lambda p: json.loads(p.read_text(encoding="utf-8"))
    print(json.dumps(evaluate(load(args.gold), load(args.pred), load(args.ranking) if args.ranking else None),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
