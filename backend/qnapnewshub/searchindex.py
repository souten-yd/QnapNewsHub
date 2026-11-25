"""検索の索引（SQLite FTS5）。日本語の部分一致（trigram）と英語（porter）の 2 つを持ち、検索はその BM25 を合わせて順位づける。

- `news_fts_tri`（trigram）: 日本語・記号まじりの語の部分一致。3 文字以上の語だけ（短い語は LIKE）。
- `news_fts_word`（porter unicode61）: 英語の語（語幹で一致。`run` → `running`）。ASCII の語はこちら。ASCII の語がここで 1 件も無ければ trigram の部分一致に落とす。
- どちらも **中身を持たない表**（content=''・contentless_delete=1）。列は 題・概要（日本語）・物の名前・抜粋・出どころ。本文（Trafilatura）は索引に入れない。
  **機械翻訳（translate.py）の題・概要・本文は「概要（日本語）」の列に足して入れる**（日本語の問いで英語の記事を引くため。news_translation のトリガーで追う）。
  1 年より古い記事は抜粋を圧縮して索引からも外す（題・概要・物・出どころだけ）。
- 索引は news_items のトリガーで追う（書くのはまとめ書き＝Write Coordinator の 1 トランザクションの中だけ。ここで別に書かない）。物の名前は entities.apply_extractions が reindex_ids を呼ぶ。
- 古い DB（3 列の外部コンテンツの索引）は、開くときに新しい表・トリガーだけ先に作り（以後の書き込みは索引に載る）、既存の行は「作り直し」の仕事（backfill_batch）が
  まとめ書きの単位（既定 2 万件ごとの 1 トランザクション）で入れる。入れ終わるまで、検索は未索引の範囲を LIKE で補う。お休み中は進めない（呼ぶ側 maintenance.py）。
- 主張（claims）の索引は別の 2 表（`claim_fts_tri`・`claim_fts_word`。列は 主張の本文＋値・主語の物の名前と別名）。主張は短い文なので、記事のような作り直しの仕事は持たず、
  開くときに 1 回だけ既存の行を入れる（docstore の索引と同じ作り。`claim_fts_v1` の印）。以後はトリガーが追う。MPKR の種（POST /retrieve/seeds）が主張を直接引くための索引。
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


def _tr(alias: str, col: str) -> str:
    """機械翻訳（news_translation。translate.py）の題・概要・本文。日本語の問いで英語の記事を引くため、題の列・概要（日本語）の列に足して入れる。"""
    return f"COALESCE((SELECT t.{col} FROM news_translation t WHERE t.item_id = {alias}.id), '')"


def _row(alias: str) -> str:
    title = f"{alias}.title"
    summary_ja = f"COALESCE({alias}.summary_ja, '') || ' ' || {_tr(alias, 'title_ja')} || ' ' || {_tr(alias, 'summary_ja')} || ' ' || {_tr(alias, 'body_ja')}"
    return f"{title}, {summary_ja}, {_ents(alias)}, {alias}.summary, {alias}.source"


def has_index(conn) -> bool:
    if conn.dialect.name != "sqlite":
        return False
    return conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": TRI}).first() is not None


def _meta(conn, key: str, default: str = "") -> str:
    row = conn.execute(text("SELECT value FROM news_meta WHERE key = :k"), {"k": key}).first()
    return row[0] if row else default


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute(text("INSERT INTO news_meta(key, value) VALUES (:k, :v) ON CONFLICT(key) DO UPDATE SET value = :v"), {"k": key, "v": value})


CLAIM_TRI, CLAIM_WORD = "claim_fts_tri", "claim_fts_word"
CLAIM_COLUMNS = "body, ents"
CLAIM_WEIGHTS = "4.0, 3.0"      # 主張の本文 > 主語の物の名前（問いが物の名前で引くとき、その物の主張が出るように）
_CLAIM_ENTS = ("(SELECT e.name || ' ' || COALESCE((SELECT group_concat(a.alias, ' ') FROM entity_aliases a WHERE a.entity_id = e.id), '') FROM entities e WHERE e.id = {a}.entity_id)")


def _claim_row(a: str) -> str:
    return f"{a}.text || ' ' || {a}.value, COALESCE({_CLAIM_ENTS.format(a=a)}, '')"


def ensure_claims(conn) -> bool:
    """主張の索引の表・トリガーを用意し、既にある主張を 1 回だけ入れる（冪等）。作れなければ False（種は主張を返さない）。"""
    try:
        for name, tok in ((CLAIM_TRI, "trigram"), (CLAIM_WORD, "porter unicode61 remove_diacritics 2")):
            conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5({CLAIM_COLUMNS}, content='', contentless_delete=1, tokenize='{tok}')"))
        ins = " ".join(f"INSERT INTO {t}(rowid, {CLAIM_COLUMNS}) VALUES (new.id, {_claim_row('new')});" for t in (CLAIM_TRI, CLAIM_WORD))
        dele = " ".join(f"DELETE FROM {t} WHERE rowid = old.id;" for t in (CLAIM_TRI, CLAIM_WORD))
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS claims_fts_ai AFTER INSERT ON claims BEGIN {ins} END"))
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS claims_fts_ad AFTER DELETE ON claims BEGIN {dele} END"))
        conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS claims_fts_au AFTER UPDATE OF text, value, entity_id ON claims BEGIN {dele} {ins} END"))
        for trig, event, ent in (("claim_fts_alias_ai", "AFTER INSERT ON entity_aliases", "new.entity_id"), ("claim_fts_alias_ad", "AFTER DELETE ON entity_aliases", "old.entity_id"),
                                 ("claim_fts_name_au", "AFTER UPDATE OF name ON entities", "new.id")):      # 物の名前・別名が変わったら、その物の主張を入れ直す
            body = " ".join(f"DELETE FROM {t} WHERE rowid IN (SELECT id FROM claims WHERE entity_id = {ent}); "
                            f"INSERT INTO {t}(rowid, {CLAIM_COLUMNS}) SELECT c.id, {_claim_row('c')} FROM claims c WHERE c.entity_id = {ent};" for t in (CLAIM_TRI, CLAIM_WORD))
            conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS {trig} {event} BEGIN {body} END"))
        if conn.execute(text("SELECT 1 FROM news_meta WHERE key = 'claim_fts_v1'")).first() is None:
            reindex_claims(conn)
            _set_meta(conn, "claim_fts_v1", "1")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("主張の検索の索引を用意できません: %s", exc)
        return False


def reindex_claims(conn, entity_ids: list[int] | None = None) -> None:
    """主張の索引を入れ直す（全部、または物の名前・別名が変わった物の主張だけ）。"""
    where, args = "", {}
    if entity_ids:
        marks = ",".join(f":e{n}" for n in range(len(entity_ids)))
        where, args = f"WHERE c.entity_id IN ({marks})", {f"e{n}": v for n, v in enumerate(entity_ids)}
    for t in (CLAIM_TRI, CLAIM_WORD):
        conn.execute(text(f"DELETE FROM {t} WHERE rowid IN (SELECT c.id FROM claims c {where})" if where else f"DELETE FROM {t}"), args)
        conn.execute(text(f"INSERT INTO {t}(rowid, {CLAIM_COLUMNS}) SELECT c.id, {_claim_row('c')} FROM claims c {where}"), args)


def has_claim_index(conn) -> bool:
    return conn.dialect.name == "sqlite" and conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": CLAIM_TRI}).first() is not None


def _create_tables(conn) -> None:
    for name, tok in ((TRI, "trigram"), (WORD, "porter unicode61 remove_diacritics 2")):
        conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5({COLUMNS}, content='', contentless_delete=1, tokenize='{tok}')"))


TRIGGER_V = "4"      # 3: 機械翻訳（news_translation）の題・概要・本文を索引に入れる（トリガーの中身が変わったので、古いトリガーは作り直す）


def _create_triggers(conn) -> None:
    if _meta(conn, "search_trg_v") != TRIGGER_V:      # 古い中身のトリガーを捨てて作り直す（索引の行は変えない。新しい書き込みから翻訳が載る）
        for name in ("news_items_ai", "news_items_ad", "news_items_au", "news_tr_ai", "news_tr_au", "news_tr_ad"):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        _set_meta(conn, "search_trg_v", TRIGGER_V)
    both_ins = " ".join(f"INSERT INTO {t}(rowid, {COLUMNS}) VALUES (new.id, {_row('new')});" for t in (TRI, WORD))
    both_del = " ".join(f"DELETE FROM {t} WHERE rowid = old.id;" for t in (TRI, WORD))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_items_ai AFTER INSERT ON news_items BEGIN {both_ins} END"))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_items_ad AFTER DELETE ON news_items BEGIN {both_del} END"))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_items_au AFTER UPDATE OF title, summary, summary_ja, source ON news_items BEGIN {both_del} {both_ins} END"))

    def redo(ref: str) -> str:
        return " ".join(f"DELETE FROM {t} WHERE rowid = {ref}.item_id; INSERT INTO {t}(rowid, {COLUMNS}) SELECT n.id, {_row('n')} FROM news_items n WHERE n.id = {ref}.item_id;"
                        for t in (TRI, WORD))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_tr_ai AFTER INSERT ON news_translation BEGIN {redo('new')} END"))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_tr_au AFTER UPDATE ON news_translation BEGIN {redo('new')} END"))
    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS news_tr_ad AFTER DELETE ON news_translation BEGIN {redo('old')} END"))


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
    ensure_claims(conn)
    ensure_vocab(conn)
    if fresh or legacy:
        top = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM news_items")).scalar() or 0
        if top:      # 既存の行は、まとめ書きの単位の仕事で入れる（開くとき重い処理をしない）
            _set_meta(conn, "search_cursor", "0")
            _set_meta(conn, "search_hi", str(top))
        else:
            _set_meta(conn, "search_cursor", "-1")
        _set_meta(conn, "search_v", VERSION)
    return True


TRI_V, CLAIM_TRI_V = "news_fts_tri_v", "claim_fts_tri_v"
SHORT_PREFIX_MAX = 120      # 3 文字未満の語 1 つあたり、広げる trigram の語（その語で始まる 3 文字）の数の上限（文書数の多い順）
SHORT_DF_RATIO = 0.04       # 広げた語の文書数の合計が全記事のこの割合を超える語（「発表」のようにどこにでも出る語）は使わない
SHORT_MAX = 3               # 1 つの問いで広げる短い語の数
SHORT_BOOST = 0.5           # 広げた短い語の当たりの加点の重み（ほかの語に一致した文書への加点）
SHORT_TITLE = 1.0           # 題・物の名前の列で当たった短い語への、追加の重み
SHORT_EXPAND = True         # 試験・評価で切り替えるため（False なら短い語を広げない＝今までの動き）


def ensure_vocab(conn) -> None:
    """trigram の索引の語の表（fts5vocab。記憶域を持たない。冪等）。3 文字未満の語（「認証」）を、その語で始まる trigram の語（「認証を」「認証基」…）へ広げるために引く。
    既存の DB でも、開くときに表を 1 つ作るだけで済む（索引の作り直しは要らない）。作れなければ何もしない（短い語は今までどおり LIKE）。"""
    try:
        for v, t in ((TRI_V, TRI), (CLAIM_TRI_V, CLAIM_TRI)):
            if conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": t}).first() is not None:
                conn.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {v} USING fts5vocab({t}, 'row')"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("索引の語の表（fts5vocab）を作れません。3 文字未満の語は LIKE で探します: %s", exc)


def expand_short(conn, short: list[str], vocab: str = TRI_V, total: int | None = None) -> list[str]:
    """3 文字未満の語（日本語の 2 文字）を、索引にある「その語で始まる trigram の語」へ広げる（trigram の索引は 3 文字未満の語に一致しないため）。
    語が文の途中・先頭に出る場合は全部拾える。**文字列の最後が語で終わる場合（列の末尾）は拾えない**（後ろに文字が無く、3 文字の語にならないため）。
    どこにでも出る語（文書数の合計が全体の SHORT_DF_RATIO 超）は広げない（雑音になり、OR の数も増える）。表が無ければ空。"""
    out: list[str] = []
    try:
        for w in short[:SHORT_MAX]:
            if not w or w.isascii():
                continue
            rows = conn.execute(text(f"SELECT term, doc FROM {vocab} WHERE term >= :a AND term < :b ORDER BY doc DESC LIMIT :k"),
                                {"a": w, "b": w + "\U0010ffff", "k": SHORT_PREFIX_MAX}).all()
            if not rows:
                continue
            if total and sum(int(d) for _, d in rows) > SHORT_DF_RATIO * total:
                continue
            out += [str(t) for t, _ in rows if len(str(t)) == 3]
    except Exception:  # noqa: BLE001      # fts5vocab の無い DB
        return []
    return out


def request_rebuild(conn) -> int:
    """索引を空にして、全件の作り直しの仕事を予約する（入れるのは backfill_batch）。予約した件数（最大の id）を返す。"""
    conn.execute(text(f"DELETE FROM {TRI}"))
    conn.execute(text(f"DELETE FROM {WORD}"))
    if has_claim_index(conn):
        reindex_claims(conn)
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


def search_any(conn, tri: list[str], word: list[str], limit: int = 200, short: list[str] | None = None) -> list[tuple[int, float]]:
    """問いの文から取った語の、どれかを含む（OR）記事の候補 [(記事の id, 点数)]（点数の大きい順）。点数は BM25 の符号を反転したもの（大きいほど合う）。
    search（全部を含む＝AND）と同じ 2 つの索引・同じ重みを使い、語を OR でつなぐだけ（ControlDeck の MPKR が種を探すための道具。v0.6）。
    trigram の側（3 文字以上の語）と英語の側（ASCII の語）は別々に引いて、記事の id で点数を足す。索引が無いときは空。
    short（3 文字未満の日本語の語）は expand_short で trigram の語へ広げ、trigram の側へ足す（SHORT_EXPAND が False なら使わない）。"""
    if not has_index(conn):
        return []
    extra: list[str] = []
    if short and SHORT_EXPAND:
        total = conn.execute(text("SELECT count(*) FROM news_items")).scalar() or 0
        extra = expand_short(conn, short, TRI_V, total)
    return _any(conn, TRI, WORD, tri, word, extra, limit)


def _any(conn, tri_t: str, word_t: str, tri: list[str], word: list[str], extra: list[str], limit: int) -> list[tuple[int, float]]:
    """search_any・search_claims の中身。extra（広げた短い語の trigram の語）は、ほかの語があるとき**順位づけだけ**に使う（それで当たった文書でも、ほかの語に一致した文書にだけ加点。
    問いの言い回しの語〔「関係」「報道」〕で無関係な文書が候補に入るのを防ぐ）。ほかの語が無いとき（短い語だけ）は、その語で探す。"""
    w = CLAIM_WEIGHTS if tri_t == CLAIM_TRI else WEIGHTS
    if not (tri or word or extra):
        return []

    def run(name: str, terms: list[str], cols: str = "") -> dict[int, float]:
        if not terms:
            return {}
        sql = f"SELECT rowid, bm25({name}, {w}) AS r FROM {name} WHERE {name} MATCH :m ORDER BY r LIMIT :k"
        m = " OR ".join(_phrase(t) for t in terms)
        return {int(rid): -float(r) for rid, r in conn.execute(text(sql), {"m": f"{cols} : ({m})" if cols else m, "k": limit})}

    scores: dict[int, float] = {}
    for name, terms in ((tri_t, tri), (word_t, word)):
        for rid, v in run(name, terms).items():
            scores[rid] = scores.get(rid, 0.0) + v
    if extra:
        boost = run(tri_t, extra)
        if tri_t == TRI and SHORT_TITLE and not (tri or word):      # 短い語だけの問い: 題・物の名前の列に出る語は、本文の言及より強く（題が主題を表す）。ほかの語があるときは足さない（雛形の語の雑音。a5d の測定）
            for rid, v in run(tri_t, extra, "{title ents}").items():
                boost[rid] = boost.get(rid, 0.0) + SHORT_TITLE * v
        if tri or word:
            for rid in scores:
                scores[rid] += SHORT_BOOST * boost.get(rid, 0.0)
        else:
            scores = boost
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def search_claims(conn, tri: list[str], word: list[str], limit: int = 100, short: list[str] | None = None) -> list[tuple[int, float]]:
    """問いの文から取った語のどれかを含む（OR）主張の候補 [(主張の id, 点数)]（点数の大きい順。点数は BM25 の符号を反転したもの）。search_any の主張版（short の扱いも同じ）。索引が無いときは空。"""
    if not has_claim_index(conn):
        return []
    extra: list[str] = []
    if short and SHORT_EXPAND:
        total = conn.execute(text("SELECT count(*) FROM claims")).scalar() or 0
        extra = expand_short(conn, short, CLAIM_TRI_V, total)
    return _any(conn, CLAIM_TRI, CLAIM_WORD, tri, word, extra, limit)
