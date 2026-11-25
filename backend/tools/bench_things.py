"""Things の全体の地図（overview）・context・夜の計算（まとまり）の性能の実測（模擬の記事）。`python tools/bench_things.py --n 100000 --entities 5000 --dir <一時の置き場>`。

- 模擬: 記事 n 件に、物（語彙 n/20 個・Zipf 分布）を 1 件 2〜9 個ずつ付ける（実データではない。物の偏りは実際のニュースに近づけた）。
- 物の数は --entities（既定 5,000）。手順: 記事・物・結びつきを SQL で入れる → kg.sync_documents → kg.rebuild_cooccur（時間を測る）→ 型つきの関係（記事 20 件に 1 本）を足す
  → neighbors（深さ 1 の 1 物・深さ 2・10 物まとめ・ハブ）を温まった状態で測り、P50・P95 を出す。冷えた状態は DB を開き直して posix_fadvise で捨てる。
- 測るのは kg_view.neighbors（API の中身。HTTP は含めない）。一時の置き場は呼ぶ側が消す。実データ・本番の置き場には触らない。
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100000)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--entities", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    os.environ["NEWSHUB_DATA"] = a.dir
    os.environ.setdefault("NEWSHUB_AUTO", "0")
    from sqlalchemy import func, select, text

    from qnapnewshub import db as news_db
    from qnapnewshub import kg, kg_view, things
    from qnapnewshub.db import Entity, Relationship

    rng = random.Random(a.seed)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n_ent = a.entities
    w = [1.0 / (i + 1) ** 0.9 for i in range(n_ent)]
    eng = news_db.engine()
    t0 = time.perf_counter()
    with eng.begin() as c:
        c.execute(text("PRAGMA synchronous=OFF"))
        c.exec_driver_sql("INSERT INTO entities (id, key, name, type, first_seen, last_seen, article_count, sensitivity, ai_use) VALUES (?,?,?,?,?,?,0,'public','allowed')",
                          [(i + 1, f"ent{i}", f"Entity{i}", "model" if i % 3 else "org", now, now) for i in range(n_ent)])
        rows, links = [], []
        for i in range(1, a.n + 1):
            rows.append((i, "news", "src", f"s{i % 50}", f"title {i}", f"https://x.test/{i}", "", now - timedelta(minutes=i * 2), now, "[]", f"https://x.test/{i}"))
            for e in set(rng.choices(range(1, n_ent + 1), weights=w, k=rng.randint(2, 9))):
                links.append((i, e))
        c.exec_driver_sql("INSERT INTO news_items (id, kind, source, source_key, title, url, summary, published_at, fetched_at, topics, canon_url, gist, summary_ja, img_prompt) VALUES (?,?,?,?,?,?,?,?,?,?,?,'','','')", rows)
        c.exec_driver_sql("INSERT INTO article_entities (article_id, entity_id) VALUES (?,?)", links)
        c.execute(text("UPDATE entities SET article_count = (SELECT count(*) FROM article_entities ae WHERE ae.entity_id = entities.id)"))
    print(f"記事 {a.n}・物 {n_ent}・結びつき {len(links)} を入れた: {time.perf_counter() - t0:.1f}s")
    with news_db.session() as db:
        t = time.perf_counter()
        kg.sync_documents(db, full=True)
        db.commit()
        print(f"文書の対応: {time.perf_counter() - t:.1f}s（{db.scalar(select(func.count()).select_from(__import__('qnapnewshub.db', fromlist=['Document']).Document))} 件）")
        t = time.perf_counter()
        out = kg.rebuild_cooccur(db, now)
        db.commit()
        print(f"co_occurs の集計: {time.perf_counter() - t:.1f}s {out}")
        types = ["uses", "replaces", "part_of", "supports", "competes", "implements"]
        extra = []
        for _ in range(a.n // 20):
            s, o = rng.choices(range(1, n_ent + 1), weights=w, k=2)
            if s != o:
                extra.append(Relationship(subject_id=min(s, o) if True else s, object_id=max(s, o), type=rng.choice(types), directed=True, status="extracted", confidence=round(rng.uniform(0.3, 0.9), 2),
                                          evidence_count=rng.randint(1, 4), first_at=now - timedelta(days=30), last_at=now, sensitivity="public", ai_use="allowed"))
        seen = set()
        for r in extra:
            k = (r.subject_id, r.object_id, r.type)
            if k not in seen:
                seen.add(k)
                db.add(r)
        db.commit()
        total = db.scalar(select(func.count(Relationship.id)))
        print(f"関係の総数: {total}（辺の多い物の上位 {sorted((db.execute(text('SELECT count(*) c FROM (SELECT subject_id e FROM relationships UNION ALL SELECT object_id FROM relationships) GROUP BY e ORDER BY c DESC LIMIT 3')).scalars().all()), reverse=True)}）")
    hubs = list(range(1, 6))
    rand = [rng.choices(range(1, n_ent + 1), weights=w, k=1)[0] for _ in range(60)]
    with news_db.session() as db:
        t = time.perf_counter()
        out = things.compute_communities(db)
        db.commit()
        print(f"まとまりの計算（夜）: {time.perf_counter() - t:.1f}s {out}")
        for i in range(1, 400):      # 定義つきのカード（相手の 1 行の定義を引く手間を含めるため）
            db.connection().exec_driver_sql("INSERT OR IGNORE INTO entity_cards (entity_id, summary, model, sig, tried_sig, tries, at, definition, state, questions, opposing) VALUES (?,?,?,?,?,0,?,?,?,?,?)",
                                            (i, "", "m", "", "", now, f"Entity{i} の 1 行の定義です。", "official", "[]", "[]"))
        db.commit()

    def measure(label: str, calls) -> None:
        ms = []
        for fn in calls:
            with news_db.session() as db:
                t = time.perf_counter()
                fn(db)
                ms.append((time.perf_counter() - t) * 1000)
        print(f"{label}: n={len(ms)} P50 {statistics.median(ms):.1f}ms P95 {pct(ms, 0.95):.1f}ms 最大 {max(ms):.1f}ms")

    scopes = ["public"]
    measure("全体の地図 overview（1,500 点・キャッシュ前）", [lambda db: things.overview(db, scopes, limit=1500) for _ in range(15)])
    with news_db.session() as db:
        o = things.overview(db, scopes, limit=1500)
        import json as _json
        print(f"  応答 {len(_json.dumps(o, ensure_ascii=False).encode()) / 1000:.0f}KB・点 {len(o['nodes'])}・線 {len(o['edges'])}・まとまり {len(o['communities'])}・全体の関係のある点 {o['totals']['with_relations']}")
    measure("context（ランダム）", [lambda db, i=i: things.context(db, db.get(__import__('qnapnewshub.db', fromlist=['Entity']).Entity, i), scopes) for i in rand * 2])
    measure("context（ハブ上位 5）", [lambda db, i=i: things.context(db, db.get(__import__('qnapnewshub.db', fromlist=['Entity']).Entity, i), scopes) for i in hubs * 10])
    measure("カード（kg_view.card・ハブ）", [lambda db, i=i: kg_view.card(db, i, scopes) for i in hubs * 4])
    measure("まとまりの中（community_members）", [lambda db, i=i: things.community_members(db, i, scopes) for i in range(1, 11)])


if __name__ == "__main__":
    main()
