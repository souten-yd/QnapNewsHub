"""検索の道具（seeds・neighbors・ppr）の性能の実測（模擬の記事）。`python tools/bench_retrieve.py --n 100000 --dir <一時の置き場>`（v0.6）。

- 模擬: 記事 n 件（英語・日本語を半々。語は Zipf 分布）に、物（語彙 n/20 個・Zipf）を 1 件 2〜9 個ずつ付ける（実データではない）。索引はトリガーで作る（本番と同じ道）。
  kg.sync_documents・co_occurs の集計・型つきの関係（記事 20 件に 1 本）まで作ってから測る。
- 測るのは API の中身（retrieval.seeds・kg_view.neighbors・retrieval.ppr。HTTP・キャッシュは含めない）。温まった: 同じ種類の問いを続けて。冷えた: 接続を開き直し posix_fadvise で捨てる。
- 一時の置き場は呼ぶ側が消す。実データ・本番の置き場には触らない。
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KANJI = "".join(chr(c) for c in range(0x4E00, 0x4E00 + 1500))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100000)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--reuse", action="store_true", help="--dir に作ってある DB をそのまま測る（作り直さない）")
    a = ap.parse_args()
    os.environ["NEWSHUB_DATA"] = a.dir
    os.environ.setdefault("NEWSHUB_AUTO", "0")
    from sqlalchemy import func, select, text

    from qnapnewshub import db as news_db
    from qnapnewshub import kg, kg_view, retrieval
    from qnapnewshub.db import Relationship
    from qnapnewshub.rcache import Budget

    rng = random.Random(a.seed)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S.%f")  # noqa: E731
    n_ent = max(200, a.n // 20)
    ew = [1.0 / (i + 1) ** 0.9 for i in range(n_ent)]
    en = [f"w{rng.randrange(10**6):x}{i}" for i in range(30000)]
    ja = ["".join(rng.choices(KANJI, k=rng.randint(2, 4))) for _ in range(30000)]
    zw = [1.0 / (i + 1) for i in range(30000)]
    if not a.reuse:
        eng = news_db.engine()
        t0 = time.perf_counter()
        with eng.begin() as c:
            c.exec_driver_sql("PRAGMA synchronous=OFF")
            c.exec_driver_sql("INSERT INTO entities (id, key, name, type, first_seen, last_seen, article_count, sensitivity, ai_use) VALUES (?,?,?,?,?,?,0,'public','allowed')",
                              [(i + 1, f"ent{i}", f"Entity{i}", "model" if i % 3 else "org", fmt(now), fmt(now)) for i in range(n_ent)])
            for lo in range(1, a.n + 1, 20000):
                rows, links = [], []
                for i in range(lo, min(a.n, lo + 19999) + 1):
                    t = " ".join(rng.choices(en, weights=zw, k=4)) if i % 2 else "の".join(rng.choices(ja, weights=zw, k=3))
                    rows.append((i, "news", "src", f"s{i % 50}", t, f"https://x.test/{i}", " ".join(rng.choices(en, weights=zw, k=12)), fmt(now - timedelta(minutes=i * 2)), fmt(now), "[]",
                                 f"https://x.test/{i}"))
                    for e in set(rng.choices(range(1, n_ent + 1), weights=ew, k=rng.randint(2, 9))):
                        links.append((i, e))
                c.exec_driver_sql("INSERT INTO news_items (id, kind, source, source_key, title, url, summary, published_at, fetched_at, topics, canon_url, gist, summary_ja, img_prompt) "
                                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,'','','')", rows)
                c.exec_driver_sql("INSERT INTO article_entities (article_id, entity_id) VALUES (?,?)", links)
            c.execute(text("UPDATE entities SET article_count = (SELECT count(*) FROM article_entities ae WHERE ae.entity_id = entities.id)"))
        print(f"記事 {a.n}・物 {n_ent} を入れた（索引つき）: {time.perf_counter() - t0:.1f}s")
        with news_db.session() as db:
            t = time.perf_counter()
            kg.sync_documents(db, full=True)
            db.commit()
            out = kg.rebuild_cooccur(db, now)
            db.commit()
            types = ["uses", "replaces", "part_of", "supports", "competes", "implements"]
            seen = set()
            for _ in range(a.n // 20):
                s, o = rng.choices(range(1, n_ent + 1), weights=ew, k=2)
                if s != o and (s, o) not in seen:
                    seen.add((s, o))
                    db.add(Relationship(subject_id=s, object_id=o, type=rng.choice(types), directed=True, status="extracted", confidence=round(rng.uniform(0.3, 0.9), 2),
                                        evidence_count=rng.randint(1, 4), first_at=now - timedelta(days=30), last_at=now - timedelta(days=rng.randint(0, 30)), sensitivity="public", ai_use="allowed"))
            db.commit()
            total = db.scalar(select(func.count(Relationship.id)))
            print(f"文書の対応・co_occurs {out}・関係の総数 {total}: {time.perf_counter() - t:.1f}s")
    hubs = list(range(1, 6))
    rand = [rng.choices(range(1, n_ent + 1), weights=ew, k=1)[0] for _ in range(60)]
    qs = [f"{rng.choice(en[:300])} {rng.choice(en[:3000])} について" for _ in range(40)] + [f"{rng.choice(ja[:300])}の{rng.choice(ja[:3000])}はどうなっているか" for _ in range(40)] + \
         [f"Entity{rng.choice(rand)} と {rng.choice(en[:100])} の違い" for _ in range(20)]

    def measure(label, calls, fresh=False):
        ms = []
        for fn in calls:
            if fresh:
                news_db.engine().dispose()
                news_db._engines.clear()  # noqa: SLF001
                for f in Path(a.dir).rglob("*.sqlite3*"):
                    fd = os.open(f, os.O_RDONLY)
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    os.close(fd)
            with news_db.session() as db:
                t = time.perf_counter()
                fn(db)
                ms.append((time.perf_counter() - t) * 1000)
        print(f"{label}: n={len(ms)} P50 {statistics.median(ms):.1f}ms P95 {pct(ms, 0.95):.1f}ms 最大 {max(ms):.1f}ms")

    S = lambda db, q: retrieval.seeds(db, q, scopes=["public"], limit=20, budget=Budget(400))  # noqa: E731
    N, P = kg_view.neighbors, retrieval.ppr
    measure("seeds（温まった）", [lambda db, q=q: S(db, q) for q in qs * 2])
    measure("neighbors 深さ 1・1 物（温まった）", [lambda db, i=i: N(db, [i], limit=200) for i in rand * 2])
    measure("neighbors 深さ 2・hub_k=50・recent（温まった）", [lambda db, i=i: N(db, [i], depth=2, limit=500, hub_k=50, order="recent") for i in rand])
    measure("neighbors 深さ 3・ハブ・hub_k=50", [lambda db, i=i: N(db, [i], depth=3, limit=1000, hub_k=50) for i in hubs * 4])
    measure("neighbors 深さ 2・ハブ・hub_k なし", [lambda db, i=i: N(db, [i], depth=2, limit=500) for i in hubs * 4])
    measure("ppr（種 3・ε 1e-4・温まった）", [lambda db, i=i: P(db, {i: 1.0, i % n_ent + 1: 1.0, (i * 7) % n_ent + 1: 1.0}, scopes=["public"], budget=Budget(400)) for i in rand])
    measure("ppr（種 1 のハブ・ε 1e-5）", [lambda db, i=i: P(db, {i: 1.0}, scopes=["public"], eps=1e-5, max_nodes=5000, budget=Budget(400)) for i in hubs * 4])
    measure("seeds（冷えた）", [lambda db, q=q: S(db, q) for q in qs[:20]], fresh=True)
    measure("neighbors（冷えた）", [lambda db, i=i: N(db, [i], depth=2, limit=500, hub_k=50) for i in rand[:20]], fresh=True)
    measure("ppr（冷えた）", [lambda db, i=i: P(db, {i: 1.0}, scopes=["public"], budget=Budget(400)) for i in rand[:20]], fresh=True)


if __name__ == "__main__":
    main()
