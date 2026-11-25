"""問いの正解が、いまの DB に無い物を指していないかの検査（作るときと測るときの両方で警告する）。

背景（2026-11-26）: 問いを作った後に、一般語の除外（retire）・名寄せ（merge）・型 other の関係の削除で、正解の物・関係・主張が DB から消えた。
消えた参照は絶対に取れないので、点数が実力より低く出て、改善の前後の比較を狂わせる。DB は読むだけ（mode=ro）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_TABLES = (("docs", "doc", "documents"), ("entities", "ent", "entities"), ("claims", "clm", "claims"), ("relationships", "rel", "relationships"), ("events", "evt", "events"))


def dangling_refs(db_path: Path, questions: list[dict]) -> dict[str, list[str]]:
    """{問いの id: 存在しない参照（"ent:12" など）}。区分が public でない物・文書も「無い」とみなす（問いは public だけで作るため）。"""
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        alive: dict[str, set[int]] = {}
        for key, _prefix, table in _TABLES:
            where = " WHERE sensitivity='public'" if table in {"documents", "entities", "claims", "relationships"} else ""
            alive[key] = {r[0] for r in c.execute(f"SELECT id FROM {table}{where}")}
    finally:
        c.close()
    out: dict[str, list[str]] = {}
    for q in questions:
        gone = [f"{prefix}:{i}" for key, prefix, _t in _TABLES for i in q.get("gold", {}).get(key, []) if i not in alive[key]]
        if gone:
            out[q["id"]] = gone
    return out


def warn_dangling(db_path: Path, questions: list[dict], label: str, *, file=None) -> int:
    """警告を stderr に出し、指している問いの数を返す。0 なら何も出さない。"""
    import sys

    bad = dangling_refs(db_path, questions)
    if bad:
        n = sum(len(v) for v in bad.values())
        print(f"警告（{label}）: 正解が DB に無い物（外された・寄せられた・非公開）を指す問いが {len(bad)}/{len(questions)} 問・参照 {n} 件。"
              f"問いを作り直してください: " + ", ".join(f"{k}→{'/'.join(v[:3])}" for k, v in list(bad.items())[:10]), file=file or sys.stderr)
    return len(bad)
