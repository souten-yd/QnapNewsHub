"""点数の計算（外に何も呼ばない純粋な関数）。

参照（ref）は文字列 `doc:<文書の id>`・`ent:<物の id>`・`clm:<主張の id>`・`rel:<関係の id>`・`evt:<イベントの id>`。
ランキングは参照の並び（先頭が最上位。重複は最初の 1 回だけ数える）、正解は参照の集合。二値の関連（正解か否か）で数える。
"""
from __future__ import annotations

import math
import re


def dedupe(ranked: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for r in ranked:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def recall_at(ranked: list[str], gold: set[str], k: int) -> float:
    """上位 k 件に入った正解の割合（正解が 0 件の問いは 0）。"""
    if not gold:
        return 0.0
    return len(set(dedupe(ranked)[:k]) & gold) / len(gold)


def mrr(ranked: list[str], gold: set[str]) -> float:
    """最初の正解の順位の逆数（正解が出なければ 0）。"""
    for i, r in enumerate(dedupe(ranked), start=1):
        if r in gold:
            return 1.0 / i
    return 0.0


def ndcg_at(ranked: list[str], gold: set[str], k: int = 10) -> float:
    """二値の nDCG@k。理想は min(正解の数, k) 件が先頭に並ぶこと。"""
    if not gold:
        return 0.0
    dcg = sum(1.0 / math.log2(i + 1) for i, r in enumerate(dedupe(ranked)[:k], start=1) if r in gold)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal else 0.0


def only_kind(refs, kind: str) -> list[str]:
    return [r for r in refs if r.startswith(kind + ":")]


_ASCII = re.compile(r"[\x00-\x7f]")


def est_tokens(text: str) -> int:
    """トークン数の見積り（外のトークナイザを使わない）。ASCII は 3.5 文字で 1、それ以外（日本語など）は 1.3 文字で 1。
    実際の値は LLM ごとに違う。道ごとの比較には同じ式を使う（絶対値ではなく相対の比較のための物差し）。"""
    ascii_n = len(_ASCII.findall(text))
    return max(1, round(ascii_n / 3.5 + (len(text) - ascii_n) / 1.3)) if text else 0


def density(hits: int, tokens: int) -> float:
    """文脈の密度＝渡した文脈 1,000 トークンあたりに入っていた正解の根拠の数。"""
    return hits / tokens * 1000.0 if tokens else 0.0


def percentile(xs: list[float], p: float) -> float:
    """最近傍順位法の百分位（p は 0〜1）。空は 0。"""
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p * len(s)) - 1))]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def score_one(ranked: list[str], gold: set[str], context_refs: set[str] | None, context_tokens: int) -> dict:
    """1 つの問い・1 つの道の点数。context_refs は LLM に渡した文脈に入っていた参照（無ければ上位 10 件）。"""
    r = dedupe(ranked)
    ctx = set(r[:10]) if context_refs is None else context_refs
    hits = len(ctx & gold)
    docs_gold = {g for g in gold if g.startswith("doc:")}
    kind_r20 = {}
    for kind, name in (("ent", "ent_r20"), ("clm", "clm_r20"), ("rel", "rel_r20")):
        g = {x for x in gold if x.startswith(kind + ":")}
        kind_r20[name] = recall_at(r, g, 20) if g else None      # 正解にその種類が無い問いは数えない（None）
    return {
        **kind_r20,
        "r5": recall_at(r, gold, 5), "r10": recall_at(r, gold, 10), "r20": recall_at(r, gold, 20),
        "mrr": mrr(r, gold), "ndcg10": ndcg_at(r, gold, 10),
        "doc_r10": recall_at(only_kind(r, "doc"), docs_gold, 10) if docs_gold else None,
        "ctx_tokens": context_tokens, "ctx_hits": hits, "ctx_density": density(hits, context_tokens),
        "ctx_cover": hits / len(gold) if gold else 0.0,
    }
