"""検索・保存の性能の実測（模擬の記事）。`python tools/bench_search.py --n 100000 --dir <一時の置き場>`。

- 模擬の記事: 英語・日本語を半々。語は Zipf 分布（実際のニュースの語の偏りに近づけた。実データではない）。題・概要（日本語）・抜粋・物の名前（一部）。
- 手順: 記事を入れる（索引のトリガーは外す）→ 索引を作り直す（まとめ書きの単位 2 万件。時間を測る）→ 検索（日本語・英語・2 語）を 冷えた・温まった で測る
  → 古い記事（半分）の抜粋を圧縮して VACUUM INTO で大きさの前後を測る。
- 冷えた: 検索ごとに DB を開き直し、ファイルのページキャッシュを posix_fadvise(DONTNEED) で捨てる（root 不要）。温まった: 同じ検索を 2 周して 2 周目。
- 測るのは store.list_items（API の中身。順位づけ・表示の条件・物の札・1 ページ 24 件の組み立てまで。HTTP は含めない）。
- 一時の置き場は呼ぶ側が消す。実データ・本番の置き場には触らない。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KANJI = "".join(chr(c) for c in range(0x4E00, 0x4E00 + 1800))
HIRA = "".join(chr(c) for c in range(0x3041, 0x3094))
SYL = ["ka", "to", "ri", "mo", "de", "ne", "su", "la", "vi", "ex", "or", "al", "in", "pro", "ter", "con", "gen", "tion", "ing", "ment", "net", "data", "mod", "ai"]


def zipf_weights(n: int) -> list[float]:
    return [1.0 / (i + 1) for i in range(n)]


def make_vocab(rng: random.Random):
    en = sorted({"".join(rng.choice(SYL) for _ in range(rng.randint(1, 3))) for _ in range(60000)}, key=lambda _: rng.random())[:30000]
    ja = []
    seen = set()
    while len(ja) < 30000:
        w = "".join(rng.choices(KANJI, weights=zipf_weights(len(KANJI)), k=rng.randint(2, 4)))
        if rng.random() < 0.3:
            w += "".join(rng.choices(HIRA, k=rng.randint(1, 3)))
        if w not in seen:
            seen.add(w)
            ja.append(w)
    return en, ja


def make_phrases(rng, words, weights, n, lo, hi, sep):
    return [sep.join(rng.choices(words, weights=weights, k=rng.randint(lo, hi))) for _ in range(n)]


def build(db_dir: Path, n: int, seed: int) -> dict:
    os.environ["NEWSHUB_DATA"] = str(db_dir)
    from qnapnewshub import db as news_db

    rng = random.Random(seed)
    en, ja = make_vocab(rng)
    we, wj = zipf_weights(len(en)), zipf_weights(len(ja))
    P = 40000
    en_t, ja_t = make_phrases(rng, en, we, P, 3, 6, " "), make_phrases(rng, ja, wj, P, 2, 4, "の")
    en_x, ja_x = make_phrases(rng, en, we, P, 12, 20, " "), make_phrases(rng, ja, wj, P, 8, 14, "、")
    news_db._engines.clear()
    eng = news_db.engine()
    with eng.begin() as c:
        for t in ("news_items_ai", "news_items_ad", "news_items_au"):
            c.exec_driver_sql(f"DROP TRIGGER IF EXISTS {t}")
        c.exec_driver_sql("DELETE FROM news_meta")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    t0 = time.time()
    raw = eng.raw_connection()
    cur = raw.cursor()
    batch = []
    ents = []
    for i in range(1, n + 1):
        is_en = rng.random() < 0.5
        title = (rng.choice(en_t) + " " + rng.choice(en_t)) if is_en else (rng.choice(ja_t) + "、" + rng.choice(ja_t))
        summary = " ".join(rng.choice(en_x) for _ in range(3)) if is_en else "".join(rng.choice(ja_x) + "。" for _ in range(3))
        sja = "".join(rng.choice(ja_x) + "。" for _ in range(2))
        published = now - timedelta(minutes=int(rng.random() * 60 * 24 * 365 * 2))      # 2 年ぶんに散らす（半分が 1 年より古い）
        batch.append((i, "news", f"Src{i % 300}", f"rss:{i % 300}", title[:400], f"https://example.test/{i}", summary, None, published, now, "[]", None, "", 0, 0, sja, "", 0, "", "", None, None, 0, 0, 0))
        if i % 80 == 0:
            ents.append((i, rng.choice(en)))
        if len(batch) >= 50000:
            _insert(cur, batch)
            batch = []
    if batch:
        _insert(cur, batch)
    raw.commit()
    raw.close()
    return {"insert_s": round(time.time() - t0, 1), "en": en, "ja": ja, "we": we, "wj": wj}


def _insert(cur, rows):
    cur.executemany("INSERT INTO news_items(id, kind, source, source_key, title, url, summary, image_url, published_at, fetched_at, topics, user_id, gist, image_tried, prep_tries, summary_ja, "
                    "img_prompt, extract_v, canon_url, guid, simhash, dup_of, body_state, cold) VALUES (" + ",".join("?" * 24) + ")", [r[:24] for r in rows])


def evict(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(path) + suffix)
        if f.exists():
            fd = os.open(f, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def queries(rng, vocab, n_each=60):
    en, ja, we, wj = vocab["en"], vocab["ja"], vocab["we"], vocab["wj"]
    out = {"日本語": [], "英語": [], "2語": [], "共通の語(最悪)": []}
    for _ in range(n_each):
        w = rng.choices(ja, weights=wj)[0]
        out["日本語"].append(w[:4] if len(w) >= 3 else w + "の")
        out["英語"].append(rng.choices(en, weights=we)[0])
        if rng.random() < 0.5:
            out["2語"].append(" ".join(rng.choices(en, weights=we, k=2)))
        else:
            a, b = rng.choices(ja, weights=wj, k=2)
            out["2語"].append(f"{a[:3]} {b[:3]}")
    out["共通の語(最悪)"] = [ja[0][:3], ja[1][:3], en[0], en[1], f"{en[0]} {en[1]}", f"{ja[0][:3]} {ja[1][:3]}"]
    return out


def run_queries(qs: dict, cold: bool, path: Path) -> dict:
    from qnapnewshub import db as news_db, store

    res = {}
    if not cold:      # 温める（1 周）
        for items in qs.values():
            for q in items:
                _one(store, news_db, q)
    for name, items in qs.items():
        times = []
        for q in items:
            if cold:
                news_db.engine().dispose()      # 接続を閉じて SQLite のページキャッシュを空にする（スキーマの確認は入れない）
                evict(path)
            times.append(_one(store, news_db, q))
        res[name] = {"n": len(times), "p50_ms": round(statistics.median(times) * 1000, 1), "p95_ms": round(pct(times, 0.95) * 1000, 1), "max_ms": round(max(times) * 1000, 1)}
    return res


def _one(store, news_db, q):
    t = time.perf_counter()
    with news_db.session() as db:
        store.list_items(db, 1, view="latest", q=q)
    return time.perf_counter() - t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100000)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    root = Path(a.dir)
    root.mkdir(parents=True, exist_ok=True)
    out: dict = {"n": a.n}
    vocab = build(root, a.n, a.seed)
    out["insert_s"] = vocab["insert_s"]
    from qnapnewshub import db as news_db, maintenance, searchindex, writer

    path = news_db.path()
    with news_db.engine().begin() as c:
        c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    out["db_mb_no_index"] = round(path.stat().st_size / 1e6)
    t = time.time()
    with news_db.engine().begin() as c:
        searchindex.request_rebuild(c)
    steps = 0
    while maintenance.index_step()["building"]:
        steps += 1
    out["rebuild_s"] = round(time.time() - t, 1)
    out["rebuild_batches"] = steps + 1
    with news_db.engine().begin() as c:
        c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    out["db_mb_indexed"] = round(path.stat().st_size / 1e6)
    qs = queries(random.Random(a.seed + 1), vocab)
    out["cold"] = run_queries(qs, True, path)
    out["warm"] = run_queries(qs, False, path)
    # 圧縮の前後
    copy = root / "before.sqlite3"
    with news_db.engine().connect() as c:
        c.exec_driver_sql(f"VACUUM INTO '{copy}'")
    out["vacuum_before_mb"] = round(copy.stat().st_size / 1e6)
    copy.unlink()
    t = time.time()
    done = 0
    while True:
        got = maintenance.compact_step(years=1)
        done += got["done"]
        if not got["done"]:
            break
    out["compact_s"], out["compacted"] = round(time.time() - t, 1), done
    with news_db.engine().connect() as c:
        c.exec_driver_sql(f"VACUUM INTO '{copy}'")
    out["vacuum_after_mb"] = round(copy.stat().st_size / 1e6)
    copy.unlink()
    out["warm_after_compact"] = run_queries(qs, False, path)
    writer.forget_all()
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
