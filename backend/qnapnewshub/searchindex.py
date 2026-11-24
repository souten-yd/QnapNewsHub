"""検索の索引（SQLite FTS5）。日本語の部分一致（trigram）と英語（porter）の 2 つを持ち、検索はその BM25 を合わせて順位づける。

- `news_fts_tri`（trigram）: 日本語・記号まじりの語の部分一致。3 文字以上の語だけ（短い語は LIKE）。
- `news_fts_word`（porter unicode61）: 英語の語（語幹で一致。`run` → `running`）。ASCII の語はこちら。ASCII の語がここで 1 件も無ければ trigram の部分一致に落とす。
- どちらも **中身を持たない表**（content=''・contentless_delete=1）。列は 題・概要（日本語）・物の名前・抜粋・出どころ。本文（Trafilatura）は索引に入れない。
  1 年より古い記事は抜粋を圧縮して索引からも外す（題・概要・物・出どころだけ）。
- 索引は news_items のトリガーで追う（書くのはまとめ書き＝Write Coordinator の 1 トランザクションの中だけ。ここで別に書かない）。物の名前は entities.apply_extractions が reindex_ids を呼ぶ。
- 古い DB（3 列の外部コンテンツの索引）は、開くときに新しい表・トリガーだけ先に作り（以後の書き込みは索引に載る）、既存の行は「作り直し」の仕事（backfill_batch）が
  まとめ書きの単位（既定 2 万件ごとの 1 トランザクション）で入れる。入れ終わるまで、検索は未索引の範囲を LIKE で補う。お休み中は進めない（呼ぶ側 maintenance.py）。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text

logger = logging.getLogger("qnapnewshub.searchindex")

TRI, WORD = "news_fts_tri", "news_fts_word"
VERSION = "2"
BATCH_ROWS = 20_000
COLUMNS = "title, summary_ja, ents, excerpt, src"
WEIGHTS = "6.0, 2.0, 3.0, 1.0, 1.0"      # bm25 の列の重み（題 > 物 > 概要 > 抜粋・出どころ）
CANDIDATES = 600                         # 順位づけの候補の上限（ここから表示の条件で絞る）
_ENTS = ("(SELECT group_concat(e.name, ' ') FROM article_entities ae JOIN entities e ON e.id = ae.entity_id WHERE ae.article_id = {a}.id)")
_ALNUM = re.compile(r"[A-Za-z0-9]")


def _ents(alias: str) -> str:
    return _ENTS.format(a=alias)


def _row(alias: str) -> str:
    return f"{alias}.title, {alias}.summary_ja, {_ents(alias)}, {alias}.summary, {alias}.source"


def has_index(conn) -> bool:
    if conn.dialect.name != "sqlite":
        return False
    return conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": TRI}).first() is not None


def _meta(conn, key: str, default: str = "") -> str:
    row = conn.execute(text("SELECT value FROM news_meta WHERE key = :k"), {"k": key}).first()
    return row[0] if row else default


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute(text("INSERT INTO news_meta(key, value) VALUES (:k, :v) ON CONFLICT(key) DO UPDATE SET value = :v"), {"k": key, "v": value})


def _create_tables(conn) -> None:
    for name, tok in ((TRI, "trigram"), (WORD, "porter unicode61 remove_diacritics 2")):
        conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5({COLUMNS}, content='', contentless_delete=1, tokenize='{tok}')"))


def _create_triggers(conn) -> None:
    both_ins = " ".join(f"INSERT INTO {t}(rowid, {COLUMNS}) VALUES (new.id, new.title, new.summary_ja, {_ents('new')}, new.summary, new.source);" for t in (TRI, WORD))
    both_del = " ".join(f"DELETE FROM {t} WHERE rowid = old.id;" for t in (TRI, WORD))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_items_ai AFTER INSERT ON news_items BEGIN {both_ins} END"))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_items_ad AFTER DELETE ON news_items BEGIN {both_del} END"))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_items_au AFTER UPDATE OF title, summary, summary_ja, source ON news_items BEGIN {both_del} {both_ins} END"))


def _drop_legacy(conn) -> bool:
    """3 列の外部コンテンツの古い索引（news_items_fts）と、古いトリガー。あれば消して True。"""
    old = conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'news_items_fts'")).first() is not None
    if old:
        for name in ("news_items_ai", "news_items_ad", "news_items_au"):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        conn.execute(text("DROP TABLE IF EXISTS news_items_fts"))
    return old


def ensure(conn) -> bool:
    """開くときに 1 回（冪等）。索引の表・トリガーを用意する。作れない SQLite（FTS5・trigram 無し）では何もせず、検索は LIKE になる。"""
    if conn.dialect.name != "sqlite":
        return False
    fresh = not has_index(conn)
    legacy = _drop_legacy(conn)
    try:
        _create_tables(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("全文検索（FTS5）を作れません。LIKE で探します: %s", exc)
        return False
    _create_triggers(conn)
    if fresh or legacy:
        top = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM news_items")).scalar() or 0
        if top:      # 既存の行は、まとめ書きの単位の仕事で入れる（開くとき重い処理をしない）
            _set_meta(conn, "search_cursor", "0")
            _set_meta(conn, "search_hi", str(top))
        else:
            _set_meta(conn, "search_cursor", "-1")
        _set_meta(conn, "search_v", VERSION)
    return True


def request_rebuild(conn) -> int:
    """索引を空にして、全件の作り直しの仕事を予約する（入れるのは backfill_batch）。予約した件数（最大の id）を返す。"""
    conn.execute(text(f"DELETE FROM {TRI}"))
    conn.execute(text(f"DELETE FROM {WORD}"))
    top = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM news_items")).scalar() or 0
    _set_meta(conn, "search_cursor", "0" if top else "-1")
    _set_meta(conn, "search_hi", str(top))
    return int(top)


def progress(conn) -> dict:
    """{"building": 作り直し中か, "cursor": ここまで入れた id, "hi": 入れる範囲の終わりの id}。"""
    cursor = int(_meta(conn, "search_cursor", "-1") or -1)
    hi = int(_meta(conn, "search_hi", "0") or 0)
    return {"building": cursor >= 0 and cursor < hi, "cursor": max(cursor, 0), "hi": hi}


def backfill_batch(conn, rows: int = BATCH_ROWS) -> dict:
    """未索引の範囲（cursor 〜 hi）から rows 件を索引へ入れる（呼ぶ側が 1 トランザクションにする）。何度呼んでも同じ結果（先に消してから入れる）。
    {"done": 入れた件数, "remaining": 残りの目安（id の幅）, "building": まだ続くか}。"""
    p = progress(conn)
    if not p["building"]:
        return {"done": 0, "remaining": 0, "building": False}
    cursor, hi = p["cursor"], p["hi"]
    last = conn.execute(text("SELECT MAX(id) FROM (SELECT id FROM news_items WHERE id > :c AND id <= :h ORDER BY id LIMIT :n)"),
                        {"c": cursor, "h": hi, "n": rows}).scalar()
    if last is None:
        _set_meta(conn, "search_cursor", str(hi))
        return {"done": 0, "remaining": 0, "building": False}
    args = {"c": cursor, "l": last}
    done = 0
    for t in (TRI, WORD):
        conn.execute(text(f"DELETE FROM {t} WHERE rowid > :c AND rowid <= :l"), args)
        cur = conn.execute(text(f"INSERT INTO {t}(rowid, {COLUMNS}) SELECT n.id, {_row('n')} FROM news_items n WHERE n.id > :c AND n.id <= :l ORDER BY n.id"), args)
        done = max(done, cur.rowcount or 0)
    _set_meta(conn, "search_cursor", str(last if last < hi else hi))
    return {"done": done, "remaining": max(0, hi - last), "building": last < hi}


def reindex_ids(conn, ids: list[int]) -> None:
    """記事（id）の索引を、いまの列と物の名前で入れ直す（物の取り出しのあと）。存在しない id は飛ばす。"""
    if not ids or not has_index(conn):
        return
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join(f":i{n}" for n in range(len(chunk)))
        args = {f"i{n}": v for n, v in enumerate(chunk)}
        for t in (TRI, WORD):
            conn.execute(text(f"DELETE FROM {t} WHERE rowid IN ({marks})"), args)
            conn.execute(text(f"INSERT INTO {t}(rowid, {COLUMNS}) SELECT n.id, {_row('n')} FROM news_items n WHERE n.id IN ({marks})"), args)


def counts(conn) -> dict:
    """索引の中の件数（docsize の行数）。"""
    if not has_index(conn):
        return {"tri": 0, "word": 0}
    return {"tri": conn.execute(text(f"SELECT count(*) FROM {TRI}_docsize")).scalar() or 0,
            "word": conn.execute(text(f"SELECT count(*) FROM {WORD}_docsize")).scalar() or 0}


# ---- 検索 ----

def _phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def classify(q: str) -> dict:
    """検索語を分ける。{"word": ASCII の語, "tri": 3 文字以上の語（ASCII 以外）, "short": LIKE で探す語}。語は空白で区切り、最大 8。"""
    out: dict[str, list[str]] = {"word": [], "tri": [], "short": []}
    for term in [t for t in q.split() if t][:8]:
        if term.isascii() and _ALNUM.search(term):
            out["word"].append(term)
        elif not term.isascii() and len(term) >= 3:
            out["tri"].append(term)
        else:
            out["short"].append(term)
    return out


def _rank(conn, tri: list[str], word: list[str], limit: int) -> list[tuple[int, float]]:
    args: dict = {"k": limit}
    if tri and word:
        args.update(m1=" ".join(_phrase(t) for t in tri), m2=" ".join(_phrase(t) for t in word))
        sql = (f"SELECT {TRI}.rowid, bm25({TRI}, {WEIGHTS}) + bm25({WORD}, {WEIGHTS}) AS r FROM {TRI} JOIN {WORD} ON {WORD}.rowid = {TRI}.rowid "
               f"WHERE {TRI} MATCH :m1 AND {WORD} MATCH :m2 ORDER BY r LIMIT :k")
    else:
        name = TRI if tri else WORD
        args["m"] = " ".join(_phrase(t) for t in (tri or word))
        sql = f"SELECT rowid, bm25({name}, {WEIGHTS}) AS r FROM {name} WHERE {name} MATCH :m ORDER BY r LIMIT :k"
    return [(int(i), float(r)) for i, r in conn.execute(text(sql), args)]


def search(conn, q: str, limit: int = CANDIDATES) -> dict | None:
    """語ごとの索引を選び、BM25 で並べた候補を返す。{"hits": [(id, score)（良い順）], "short": LIKE で絞る短い語, "indexed": 索引を使ったか}。
    索引で探せる語が 1 つも無い（短い語だけ）・索引が無いなら hits は None（呼ぶ側が LIKE で探す）。"""
    if not has_index(conn):
        return None
    groups = classify(q)
    if not groups["tri"] and not groups["word"]:
        return None
    hits = _rank(conn, groups["tri"], groups["word"], limit)
    if not hits and groups["word"] and not groups["tri"] and all(len(t) >= 3 for t in groups["word"]):
        hits = _rank(conn, groups["word"], [], limit)      # 英語の語で見つからなければ、部分一致（trigram）で
    return {"hits": hits, "short": groups["short"]}


def search_any(conn, tri: list[str], word: list[str], limit: int = 200) -> list[tuple[int, float]]:
    """問いの文から取った語の、どれかを含む（OR）記事の候補 [(記事の id, 点数)]（点数の大きい順）。点数は BM25 の符号を反転したもの（大きいほど合う）。
    search（全部を含む＝AND）と同じ 2 つの索引・同じ重みを使い、語を OR でつなぐだけ（ControlDeck の MPKR が種を探すための道具。v0.6）。
    trigram の側（3 文字以上の語）と英語の側（ASCII の語）は別々に引いて、記事の id で点数を足す。索引が無いときは空。"""
    if not has_index(conn) or not (tri or word):
        return []
    scores: dict[int, float] = {}
    for name, terms in ((TRI, tri), (WORD, word)):
        if not terms:
            continue
        match = " OR ".join(_phrase(t) for t in terms)
        sql = f"SELECT rowid, bm25({name}, {WEIGHTS}) AS r FROM {name} WHERE {name} MATCH :m ORDER BY r LIMIT :k"
        for rid, r in conn.execute(text(sql), {"m": match, "k": limit}):
            scores[int(rid)] = scores.get(int(rid), 0.0) - float(r)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
