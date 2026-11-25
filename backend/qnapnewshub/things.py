"""Things（旧「物」）を LLM の別の知識体（Obsidian のような脳）として使うための読み方と夜の計算（2026-11-25 利用者の指示）。データ名は entity のまま（互換）。

読む（すべて scopes で絞る。既定は public だけ。confidential は管理者・許可つきのトークンだけ＝kg_router.scopes_for）:
- `brief`（カードの先頭の帯）: 1 行の定義・種類・今の状態・確かさ・別名・根拠の数と最新の根拠の日時・最後に確かめた日。
  **確かさ** = 1 − 0.5^min(根拠の文書の数, 5)（1 件 0.5・2 件 0.75・3 件 0.88。根拠が無ければ 0）。LLM の言い分ではなく、根拠の数だけで決める（誇張しない）。
- `relation_groups`（ExcaliBrain の分け方）: 上位（part_of の先）・下位（part_of の元）・仲間（competes・co_occurs）・使う／使われる（uses・implements・supports の向き）・
  置き換える／置き換えられる（replaces の向き）・矛盾・その他。向きは「その物から見て」。同じ相手が複数の型で出れば別の行。
- `metric_rows`（条件つきの数値）: 値を持つ主張（条件は主張の文）と、Metric（metric_points）の最新の値。
- `context`（LLM の脳の API）: 1 行の定義・frontmatter のような項目・型つきの関係（相手の 1 行の定義つき）・主な事実と根拠の URL・未解決の問い。短く（上限つき）。
- `overview`（全体の地図）: 次数と最近の関係で上位 N 点（既定 1,500）・その間の線・まとまりの代表。孤立した点は別の配列。`community_members`（まとまりの中）。
- MOC（主題の入口）: まとまりごと・チャンネルごとの入口（主要な Things・最近の出来事・未解決の問い）。

夜に計算（kg_job.aggregate から。LLM なし・依存なし）:
- `compute_communities`: 関係（public だけ）の重み付きグラフを **Louvain**（局所移動と集約の 2 段。決まった順・同点は小さい番号で乱数なし。label propagation は橋のある塊を 1 つに潰したので採らなかった）で分ける。小さすぎるまとまり（MIN_COMMUNITY 未満）は作らない。
- `compute_mentions`: 別名・名前が記事の題・抜粋に出ているのに article_entities に無い記事を「つなぐ候補」として保存（全文の索引で探し、文字列の一致で確かめる）。利用者の確定・却下は残す。
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, or_, select, text
from sqlalchemy.orm import Session

from qnapnewshub import kg, searchindex
from qnapnewshub.db import (ArticleEntity, Claim, Community, Document, Entity, EntityAlias, EntityCard, EntityCommunity, Event, EventArticle, Evidence, MapLayout, MentionCandidate, MetricPoint,
                            NewsDocument, NewsItem, NewsMeta, Relationship)
from qnapnewshub.extract import normalize_key

_UTC = timezone.utc
STATES = {"official": "正式公開", "experimental": "実験的", "deprecated": "廃止", "unknown": "不明"}
GROUPS: list[tuple[str, str]] = [("upper", "上位"), ("lower", "下位"), ("peers", "仲間"), ("uses", "使う"), ("used_by", "使われる"), ("replaces", "置き換える"),
                                 ("replaced_by", "置き換えられる"), ("contradicts", "矛盾"), ("other", "その他")]
GROUP_LABEL = dict(GROUPS)
GROUP_PER = 30      # 1 つの節に出す関係の上限（残りは more の数だけ）
MIN_COMMUNITY = 3
MAX_COMMUNITIES = 300
OVERVIEW_LIMIT_DEFAULT, OVERVIEW_LIMIT_MAX = 1500, 3000
OVERVIEW_EDGES_MAX = 3500
OVERVIEW_ISOLATED = 200
RECENT_DAYS = 30
MENTION_ENTITIES = 1500      # 夜に探す物の数（記事の多い順）
MENTION_PER_ENTITY = 20
MENTION_MIN_ARTICLES = 3
MENTION_DAYS = 180
DEF_CHARS = 80


def _now() -> datetime:
    return datetime.now(_UTC)


def confidence_of(n_docs: int) -> float:
    return round(1 - 0.5 ** min(max(int(n_docs), 0), 5), 2)


def _loads(raw: str | None) -> list[str]:
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(x) for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def _first_sentence(text_: str, limit: int = DEF_CHARS) -> str:
    t = re.sub(r"\s+", " ", str(text_ or "")).strip()
    m = re.search(r"[。.！!？?]", t)
    if m and m.end() <= limit:
        return t[:m.end()]
    return t[:limit] + ("…" if len(t) > limit else "")


def definitions(db: Session, ids: list[int], limit: int = DEF_CHARS) -> dict[int, str]:
    """{物の id: 1 行の定義}。定義が無ければ概要の最初の文。どちらも無ければ空。"""
    out: dict[int, str] = {}
    ids = list(dict.fromkeys(int(i) for i in ids))
    for i in range(0, len(ids), 400):
        for eid, d, s in db.execute(select(EntityCard.entity_id, EntityCard.definition, EntityCard.summary).where(EntityCard.entity_id.in_(ids[i:i + 400]))):
            out[int(eid)] = _first_sentence(d or s, limit) if (d or s) else ""
    return out


# ---- カードの帯・関係の分け方・数値 ----

def brief(db: Session, ent: Entity) -> dict:
    cd = db.get(EntityCard, ent.id)
    n = int(ent.article_count or 0)
    state = (cd.state if cd and cd.state in STATES else "unknown") if cd else "unknown"
    conf = confidence_of(n)
    return {"definition": (cd.definition if cd and cd.definition else ""), "type": ent.type, "state": {"key": state, "label": STATES[state]},
            "confidence": conf, "confidence_label": "高" if conf >= 0.87 else "中" if conf >= 0.7 else "低",
            "aliases": list(db.scalars(select(EntityAlias.alias).where(EntityAlias.entity_id == ent.id).order_by(EntityAlias.id).limit(12))),
            "evidence_count": n, "latest_evidence_at": kg.iso(ent.last_seen) if n else "", "checked_at": kg.iso(cd.at) if cd and (cd.definition or cd.summary) else ""}


def group_of(rel_type: str, direction: str) -> str:
    """その物から見た関係の節。direction は out（その物が主語）・in（その物が目的語）・both（向きなし）。"""
    if rel_type == "part_of":
        return "upper" if direction == "out" else "lower" if direction == "in" else "peers"
    if rel_type in ("competes", "co_occurs"):
        return "peers"
    if rel_type in ("uses", "implements", "supports"):
        return "uses" if direction != "in" else "used_by"
    if rel_type == "replaces":
        return "replaces" if direction != "in" else "replaced_by"
    if rel_type == "contradicts":
        return "contradicts"
    return "other"


def relation_groups(rels: list[dict], defs: dict[int, str]) -> list[dict]:
    """relations_of の行を節に分ける。空の節は出さない。節の中は確かさ・根拠の順（呼ぶ側の並びのまま）。"""
    by: dict[str, list[dict]] = {k: [] for k, _ in GROUPS}
    for r in rels:
        row = {**r, "other": {**r["other"], "definition": defs.get(r["other"]["id"], "")}}
        by[group_of(r["type"], r.get("direction", "both"))].append(row)
    return [{"key": k, "label": label, "count": len(by[k]), "items": by[k][:GROUP_PER], "more": max(0, len(by[k]) - GROUP_PER)} for k, label in GROUPS if by[k]]


def metric_rows(db: Session, ent: Entity, claims: list[dict], limit: int = 12) -> list[dict]:
    """主な数値。①値を持つ主張（text が条件）②Metric の最新値（subject の最後の / の後ろが物の名前と同じもの）。"""
    rows = [{"source": "claim", "kind": c["kind"], "label": c["text"], "value": c["value"], "unit": "", "at": "", "url": (c["evidences"][0]["document"]["url"] if c["evidences"] else "")}
            for c in claims if c["value"]][:limit]
    pat = "%/" + re.sub(r"([%_\\])", r"\\\1", ent.name) if ent.name else ""
    if pat:
        seen: set[tuple] = set()
        for subject, metric, value, unit, at, cond in db.execute(select(MetricPoint.subject, MetricPoint.metric, MetricPoint.value, MetricPoint.unit, MetricPoint.at, MetricPoint.cond)
                                                                 .where(MetricPoint.subject.like(pat, escape="\\")).order_by(MetricPoint.at.desc()).limit(200)):
            if normalize_key(subject.rsplit("/", 1)[-1]) != ent.key or (subject, metric, cond) in seen:
                continue
            seen.add((subject, metric, cond))
            rows.append({"source": "metric", "kind": metric, "label": f"{metric}" + (f"（{cond}）" if cond else ""), "value": f"{value:g}", "unit": unit, "at": kg.iso(at), "url": ""})
    return rows[:limit]


def mentions_of(db: Session, entity_id: int, limit: int = 8) -> list[dict]:
    rows = db.execute(select(MentionCandidate, NewsItem.title, NewsItem.url, NewsItem.source).join(NewsItem, NewsItem.id == MentionCandidate.article_id)
                      .where(MentionCandidate.entity_id == entity_id, MentionCandidate.status == "candidate").order_by(MentionCandidate.published_at.desc()).limit(limit))
    return [{"article_id": m.article_id, "title": title, "url": url or "", "source": source, "matched": m.matched, "published_at": kg.iso(m.published_at)} for m, title, url, source in rows]


# ---- LLM の脳の API（context） ----

def doc_label(source: str, published_at, doc_id) -> str:
    """LLM に渡す文脈の出どころの印: 「出どころの名前 日付 d<文書の id>」。Google ニュースの転送 URL のような長い URL をトークンに使わず、元の URL は JSON の sources・src に残す。
    d<id> は /retrieve/evidence・/retrieve/seeds の文書の id と同じ（そこから根拠・URL をたどれる）。"""
    day = str(published_at or "")[:10]
    return " ".join(x for x in (str(source or "").strip(), day, f"d{doc_id}" if doc_id else "") if x)


def context(db: Session, ent: Entity, scopes: list[str], *, max_relations: int = 12, max_facts: int = 5, def_chars: int = 60) -> dict:
    """短い文脈。frontmatter のような項目（type・status・aliases・confidence・updated・sources）＋ 1 行の定義 ＋ 型つきの関係（相手の 1 行の定義つき）＋ 主な事実と根拠の URL ＋ 未解決の問い。
    空の項目は出さない（トークンの節約）。関係は節ごとの均等な枠で max_relations まで。"""
    from qnapnewshub import kg_view

    b = brief(db, ent)
    cd = db.get(EntityCard, ent.id)
    rels = kg_view.relations_of(db, ent.id, scopes, limit=max(60, max_relations * 4))
    defs = definitions(db, [r["other"]["id"] for r in rels], def_chars)
    groups = relation_groups(rels, defs)
    rel_out: dict[str, list[dict]] = {}
    left = max_relations
    for rnd in range(max_relations):      # 節ごとに 1 つずつ配る（1 つの節（co_occurs の山）が枠を占めない）
        took = False
        for g in groups:
            if left <= 0:
                break
            if rnd < len(g["items"]):
                r = g["items"][rnd]
                row = {"id": r["other"]["id"], "name": r["other"]["name"], "rel": r["type"], "conf": r["confidence"]}
                if r["other"]["definition"]:
                    row["def"] = r["other"]["definition"]
                rel_out.setdefault(g["key"], []).append(row)
                left -= 1
                took = True
        if not took or left <= 0:
            break
    claims = kg_view._claims_with_evidence(db, ent.id, scopes, limit=max_facts * 3)
    facts = []
    for c in claims:
        docs = [e["document"] for e in c["evidences"] if e["document"]["url"]][:2]
        if c["value"] or docs:
            row = {"text": c["text"][:120]}
            if c["value"]:
                row["value"] = c["value"]
            if docs:
                row["src"] = [d["url"] for d in docs]
                row["src_label"] = [doc_label(d.get("source", ""), d.get("published_at", ""), d["id"]) for d in docs]
            facts.append(row)
        if len(facts) >= max_facts:
            break
    src_rows = db.execute(select(NewsItem.url, NewsItem.source, NewsItem.published_at, NewsDocument.document_id).join(ArticleEntity, ArticleEntity.article_id == NewsItem.id)
                          .outerjoin(NewsDocument, NewsDocument.news_item_id == NewsItem.id).where(ArticleEntity.entity_id == ent.id, NewsItem.url != "")
                          .order_by(NewsItem.published_at.desc()).limit(3)).all()
    sources = [u for u, *_ in src_rows]
    out: dict = {"id": ent.id, "name": ent.name, "type": ent.type, "status": b["state"]["key"], "confidence": b["confidence"], "updated": (b["latest_evidence_at"] or "")[:10]}
    if b["definition"]:
        out["definition"] = b["definition"]
    elif cd and cd.summary:
        out["definition"] = _first_sentence(cd.summary, 120)
    if b["aliases"]:
        out["aliases"] = b["aliases"][:6]
    if sources:
        out["sources"] = sources
        out["source_label"] = [doc_label(src, kg.iso(at), did) for _, src, at, did in src_rows]
    if rel_out:
        out["relations"] = rel_out
    if facts:
        out["facts"] = facts
    qs = _loads(cd.questions) if cd else []
    if qs:
        out["open_questions"] = qs[:4]
    op = _loads(cd.opposing) if cd else []
    if op:
        out["opposing"] = op[:3]
    return out


def context_text(c: dict) -> str:
    """context を、LLM に読ませる短い Markdown（frontmatter ＋ 本文）にする。JSON より短い。"""
    fm = [f"id: {c['id']}", f"name: {c['name']}", f"type: {c['type']}", f"status: {c['status']}", f"confidence: {c['confidence']}"]
    if c.get("aliases"):
        fm.append("aliases: " + ", ".join(c["aliases"]))
    if c.get("updated"):
        fm.append(f"updated: {c['updated']}")
    for s in c.get("source_label", []):
        fm.append(f"source: {s}")
    lines = ["---", *fm, "---"]
    if c.get("definition"):
        lines.append(c["definition"])
    for key, rows in c.get("relations", {}).items():
        lines.append(f"## {GROUP_LABEL.get(key, key)}")
        lines += [f"- [{r['name']}]#{r['id']} ({r['rel']}, {r['conf']})" + (f": {r['def']}" if r.get("def") else "") for r in rows]
    if c.get("facts"):
        lines.append("## Facts")
        lines += [f"- {f['text']}" + (f" = {f['value']}" if f.get("value") else "") + ("".join(f" <{u}>" for u in f.get("src_label", []))) for f in c["facts"]]
    if c.get("open_questions"):
        lines.append("## Open Questions")
        lines += [f"- {q}" for q in c["open_questions"]]
    if c.get("opposing"):
        lines.append("## Opposing Views")
        lines += [f"- {q}" for q in c["opposing"]]
    return "\n".join(lines)


# ---- 夜: まとまり（Louvain の局所移動を 2 段） ----

def _local_move(adj: dict[int, dict[int, float]], passes: int) -> dict[int, int]:
    """Louvain の第 1 段（モジュラリティが上がる移動だけ）。点を番号の順に回し、同点は小さい番号＝乱数なし。{点: まとまりの番号}。"""
    m2 = sum(sum(d.values()) for d in adj.values())
    if m2 <= 0:
        return {n: n for n in adj}
    comm = {n: n for n in adj}
    k = {n: sum(d.values()) for n, d in adj.items()}
    tot = dict(k)
    for _ in range(passes):
        moved = 0
        for n in sorted(adj):
            cur = comm[n]
            links: dict[int, float] = {}
            for nb, w in adj[n].items():
                if nb != n:
                    links[comm[nb]] = links.get(comm[nb], 0.0) + w
            tot[cur] -= k[n]
            best, gain_best = cur, links.get(cur, 0.0) - k[n] * tot[cur] / m2
            for c in sorted(links):
                gain = links[c] - k[n] * tot[c] / m2
                if gain > gain_best + 1e-12:
                    best, gain_best = c, gain
            tot[best] += k[n]
            if best != cur:
                comm[n] = best
                moved += 1
        if not moved:
            break
    return comm


def louvain(edges: list[tuple[int, int, float]], passes: int = 10) -> dict[int, int]:
    """重み付きの無向グラフから、モジュラリティを上げる主題のまとまりを作る（Louvain の移動と集約を 2 回。依存なし・乱数なし）。
    {点: まとまりの代表の点の id（まとまりの中で最小）}。つながりの薄い塊は別のまとまりのまま残る。"""
    adj: dict[int, dict[int, float]] = {}
    for a, b, w in edges:
        if a == b:
            continue
        adj.setdefault(a, {})[b] = adj.setdefault(a, {}).get(b, 0.0) + w
        adj.setdefault(b, {})[a] = adj.setdefault(b, {}).get(a, 0.0) + w
    member = {n: n for n in adj}      # 元の点 → 今の（集約した）点
    graph = adj
    for _ in range(2):
        comm = _local_move(graph, passes)
        if len(set(comm.values())) == len(graph):
            break
        member = {n: comm[c] for n, c in member.items()}
        nxt: dict[int, dict[int, float]] = {}
        for a, d in graph.items():
            for b, w in d.items():
                row = nxt.setdefault(comm[a], {})
                row[comm[b]] = row.get(comm[b], 0.0) + w
        graph = nxt
    lowest: dict[int, int] = {}
    for n, c in member.items():
        lowest[c] = min(lowest.get(c, n), n)
    return {n: lowest[c] for n, c in member.items()}


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9.+]{2,}|[ァ-ヴー]{3,}|[一-龥]{2,}")
_WORD_STOP = frozenset({"the", "and", "for", "with", "new", "inc", "corp", "corporation", "ltd", "llc", "pro", "max", "mini", "plus", "series", "update", "release"})


def community_names(groups: list[list[str]], words: int = 2) -> list[str]:
    """まとまりの名前。groups は各まとまりの Things の名前（関係の多い順。先頭が代表）。名前 =「代表・特徴の語・特徴の語」（番号ではなく中身で付ける）。
    特徴の語は、そのまとまりの Things の名前に出る語のうち、ほかのまとまりに少ない語（tf × idf。代表の名前に入っている語・定型語は除く）。足りなければ次に関係の多い Things の名前で補う。
    同じ名前が重なったら、次の候補の語を足して区別する。"""
    toks: list[dict[str, float]] = []
    df: dict[str, int] = {}
    for names in groups:
        tf: dict[str, float] = {}
        for rank, name in enumerate(names):
            for w in {w.casefold() for w in _WORD.findall(unicodedata.normalize("NFKC", name))}:
                tf[w] = tf.get(w, 0.0) + 1.0 / (1 + 0.1 * rank)
        toks.append(tf)
        for w in tf:
            df[w] = df.get(w, 0) + 1
    n = max(1, len(groups))
    out: list[str] = []
    used: set[str] = set()
    for names, tf in zip(groups, toks):
        rep = names[0] if names else ""
        rep_words = {w.casefold() for w in _WORD.findall(unicodedata.normalize("NFKC", rep))} | {_fold(rep)}
        scored = sorted(((v * math.log(1 + n / df[w]), w) for w, v in tf.items() if w not in rep_words and w not in _WORD_STOP and not any(w in rw or rw in w for rw in rep_words if len(rw) > 2)),
                        key=lambda t: (-t[0], t[1]))
        display = {}
        for name in names:
            for w in _WORD.findall(unicodedata.normalize("NFKC", name)):
                display.setdefault(w.casefold(), w)
        picks = [d for d in (display.get(w, w) for _, w in scored) if not (d.isascii() and d.islower() and d.isalpha())]      # 小文字だけの一般語（text・open・file）は特徴の語にしない
        # 足りなければ、次に関係の多い Things の名前で補う
        picks += [x for x in names[1:] if x and x not in picks]
        label = ""
        for take in range(words, words + 4):
            label = "・".join(dict.fromkeys([rep, *picks[:take]]))
            if label not in used:
                break
        used.add(label)
        out.append(label)
    return out


def compute_communities(db: Session) -> dict:
    """public の関係から主題のまとまりを作り直す（コミットは呼ぶ側）。{"communities", "assigned"}。"""
    rows = db.execute(select(Relationship.subject_id, Relationship.object_id, Relationship.confidence, Relationship.type)
                      .where(Relationship.sensitivity == "public")).all()
    public = {i for (i,) in db.execute(select(Entity.id).where(Entity.sensitivity == "public"))}
    edges = [(a, b, float(c or 0.1) * (0.6 if t == "co_occurs" else 1.0) + 0.05) for a, b, c, t in rows if a in public and b in public]
    label = louvain(edges)
    deg: dict[int, int] = {}
    for a, b, _ in edges:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    groups: dict[int, list[int]] = {}
    for n, lab in label.items():
        groups.setdefault(lab, []).append(n)
    big = sorted((g for g in groups.values() if len(g) >= MIN_COMMUNITY), key=lambda g: (-len(g), min(g)))[:MAX_COMMUNITIES]
    db.execute(delete(EntityCommunity))
    db.execute(delete(Community))
    names = {e.id: e.name for e in db.scalars(select(Entity).where(Entity.id.in_({i for g in big for i in g})))} if big else {}
    for members in big:
        members.sort(key=lambda i: (-deg.get(i, 0), i))
    labels = community_names([[names.get(i, "") for i in members] for members in big])
    now = _now()
    assigned = 0
    for cid, members in enumerate(big, 1):
        db.add(Community(id=cid, name=labels[cid - 1][:200], size=len(members), rep_id=members[0], top_ids=json.dumps(members[:8]), computed_at=now))
        db.execute(insert(EntityCommunity), [{"entity_id": i, "community_id": cid} for i in members])
        assigned += len(members)
    return {"communities": len(big), "assigned": assigned}


# ---- 夜: リンクされていない言及 ----

def _fold(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).casefold()


MENTION_TYPES = frozenset({"model", "software", "org", "product", "game", "tech", "person"})      # 文脈に合う種類（other・place・policy は一般語が多く、除く）
MENTION_SEARCH = 400
MENTION_GENERIC_DF = 20      # 文書頻度がこれ以上で、結びついた記事の MENTION_GENERIC_RATIO 倍を超えて出る名前は一般語（Metal・Windows など）として外す
MENTION_GENERIC_RATIO = 3
_ALNUM = re.compile(r"[0-9a-z]", re.I)
_CAMEL = re.compile(r"\s+[A-Z][a-z0-9]+[A-Z]")      # 直後が EventBridge のような大文字混じりの語（Amazon EventBridge）＝より長い名前の一部
_SPONSOR = re.compile(r"(sponsored|presented|brought to you|powered) by\s*$")
_TITLE_TAIL = re.compile(r"\s+[-|–—:]\s+([^-|–—:]{2,40})$")


_TAIL_WORDS = re.compile(r"(ブログ|blog|ニュース|news|メディア|公式|official|qiita|zenn|note)", re.I)


def _strip_tail(title: str, source: str) -> str:
    """題の末尾の「 | 出どころ」「 - 出どころ」を外す（取得元の名前・ブログ名は、記事の中身の言及ではない）。"""
    m = _TITLE_TAIL.search(title or "")
    if m is None:
        return title or ""
    tail = _fold(m.group(1))
    src = _fold(source)
    if (src and (tail in src or src in tail)) or _TAIL_WORDS.search(tail):
        return title[:m.start()]
    return title


def _ambiguous(name: str) -> bool:
    """一般の語と同じ綴りになりやすい名前（英字だけの 1 語・10 文字以内）。文脈（同じ記事に出る物）が合うときだけ候補にする。"""
    return name.isascii() and name.isalpha() and len(name) <= 10


def _name_ok(name: str, kind: str) -> bool:
    n = name.strip()
    if kind not in MENTION_TYPES:
        return False
    if n.isascii():
        if len(n) < 4:      # 3 文字以下の英字（AI・Mac など）は一般語・略語と区別できない
            return False
        return not (kind == "person" and " " not in n)
    return len(n) >= 3


def _occurrences(hay: str, orig: str, name: str, longer: list[str]) -> int:
    """題＋抜粋 hay（折りたたみ済み）で、name が独立した言及として出る回数。orig は NFKC のままの文（大文字小文字を見る）。
    英数字の途中・ハイフンやドット＋英数字の続き（Qwen3.8-Flash）・より長い既知の名前の一部（Claude Code の Claude）・CamelCase の語が続く場合（Amazon EventBridge）は数えない。"""
    f = _fold(name)
    n = 0
    pos = hay.find(f)
    spans = [(m.start(), m.end()) for ln in longer for m in re.finditer(re.escape(ln), hay)] if longer and pos >= 0 else []
    ascii_name = f.isascii()
    exact = None
    plain = name.replace(" ", "")
    if ascii_name and plain.isalpha():      # 英字だけの名前は大文字小文字を見る（metal は金属、Metal は API。coding agent は一般の語、Coding Agent だけが名前）。1 語は Name・NAME も可
        exact = ({name, name.upper(), name.capitalize()} if " " not in name else {name, name.upper()}) if name[0].isupper() or name.isupper() else {name}
    while pos >= 0:
        end = pos + len(f)
        before = hay[pos - 1] if pos else " "
        after = hay[end] if end < len(hay) else " "
        ok = True
        if ascii_name:
            if _ALNUM.match(before) or _ALNUM.match(after) or before == "_" or after == "_":
                ok = False
            elif after in "-." and end + 1 < len(hay) and _ALNUM.match(hay[end + 1]):
                ok = False
            elif before in "-." and pos >= 2 and _ALNUM.match(hay[pos - 2]):
                ok = False
        if ok and any(a <= pos and end <= b and (a, b) != (pos, end) for a, b in spans):
            ok = False
        if ok and exact is not None and orig[pos:end] not in exact:
            ok = False
        if ok and ascii_name and _CAMEL.match(orig[end:end + 24]):
            ok = False
        if ok and _SPONSOR.search(hay[max(0, pos - 24):pos]):
            ok = False      # 「9to5Mac is sponsored by Sony」のような提供元の定型句
        if ok:
            n += 1
        pos = hay.find(f, pos + 1)
    return n


def compute_mentions(db: Session, now: datetime | None = None) -> dict:
    """記事の多い物（public・allowed）の名前・別名が、記事の題・抜粋に出ているのに article_entities に無い記事を候補にする（雑音を減らした版）。
    全文の索引で候補を引き、`_occurrences` で独立した言及だけを数える。外すもの: ①種類が文脈に合わない物（other・place・policy）・短い語 ②文書頻度が高く、結びついた記事の数に比べて多すぎる名前（一般語）
    ③より長い既知の名前の一部・CamelCase・ハイフン続き ④取得元の名前・題の末尾の出どころ ⑤題に無く、抜粋に 1 回だけ出る言及 ⑥一般の語と同じ綴りの名前（Windows など）で、同じ記事に出る物が合わないもの。
    確定・却下は残し、未確定の候補だけ作り直す。"""
    since = (now or _now()) - timedelta(days=MENTION_DAYS)
    conn = db.connection()
    if not searchindex.has_index(conn):
        return {"entities": 0, "candidates": 0}
    ents = list(db.scalars(select(Entity).where(Entity.sensitivity == "public", Entity.ai_use == "allowed", Entity.article_count >= MENTION_MIN_ARTICLES, Entity.type.in_(MENTION_TYPES))
                           .order_by(Entity.article_count.desc(), Entity.id).limit(MENTION_ENTITIES)))
    aliases: dict[int, list[str]] = {}
    ids = [e.id for e in ents]
    for i in range(0, len(ids), 400):
        for eid, alias in db.execute(select(EntityAlias.entity_id, EntityAlias.alias).where(EntityAlias.entity_id.in_(ids[i:i + 400]))):
            aliases.setdefault(eid, []).append(alias)
    # より長い既知の名前（どの物の名前・別名でも）
    all_names = {_fold(n) for (n,) in db.execute(select(Entity.name).where(Entity.sensitivity == "public"))}
    all_names |= {_fold(a) for (a,) in db.execute(select(EntityAlias.alias))}
    all_names = {n for n in all_names if len(n) >= 4}
    db.execute(delete(MentionCandidate).where(MentionCandidate.status == "candidate"))
    decided = {(e, a) for e, a in db.execute(select(MentionCandidate.entity_id, MentionCandidate.article_id))}
    total = 0
    for ent in ents:
        names = [n for n in dict.fromkeys([ent.name, *aliases.get(ent.id, [])]) if _name_ok(n, ent.type)][:4]
        if not names:
            continue
        linked = {a for (a,) in db.execute(select(ArticleEntity.article_id).where(ArticleEntity.entity_id == ent.id))}
        longers = {n: [x for x in all_names if x != _fold(n) and _fold(n) in x] for n in names}
        cand: dict[int, str] = {}
        verified: set[int] = set()      # 言及として数えられる記事（結びつき済みを含む）＝文書頻度
        texts: dict[int, tuple[str, str, str, str, str]] = {}
        for name in names:
            res = searchindex.search(conn, name, limit=MENTION_SEARCH)
            hits = [aid for aid, _ in (res or {}).get("hits") or []]
            need = [a for a in hits if a not in texts]
            for i in range(0, len(need), 400):
                for aid, title, summary, source, key, dup in db.execute(select(NewsItem.id, NewsItem.title, NewsItem.summary, NewsItem.source, NewsItem.source_key, NewsItem.dup_of)
                                                                         .where(NewsItem.id.in_(need[i:i + 400]))):
                    o = unicodedata.normalize("NFKC", f"{_strip_tail(title, source)}\n{summary or ''}")
                    texts[aid] = (o, _fold(o), _fold(title), _fold(f"{source} {key}"), "dup" if dup is not None else "")
            for aid in hits:
                if aid not in texts:
                    continue
                o, hay, _, _, _ = texts[aid]
                if _occurrences(hay, o, name, longers[name]) > 0:
                    verified.add(aid)
                    if aid not in linked and aid not in cand and (ent.id, aid) not in decided:
                        cand[aid] = name
        if len(verified) >= MENTION_GENERIC_DF and len(verified) > MENTION_GENERIC_RATIO * max(1, ent.article_count):
            continue      # 一般語（結びついた記事の数に比べて、文中に出すぎる）
        if not cand:
            continue
        rows = db.execute(select(NewsItem.id, NewsItem.published_at).where(NewsItem.id.in_(list(cand)), NewsItem.published_at >= since)).all()
        public_docs = {n for (n,) in db.execute(select(NewsDocument.news_item_id).join(Document, Document.id == NewsDocument.document_id)
                                                .where(NewsDocument.news_item_id.in_([r[0] for r in rows]), Document.sensitivity == "public"))} if rows else set()
        ambiguous = any(_ambiguous(n) for n in names)
        co: set[int] = set()
        by_article: dict[int, set[int]] = {}
        if ambiguous:      # 文脈: この物と一緒に出やすい物（結びついた記事に出る他の物の上位）
            mine = select(ArticleEntity.article_id).where(ArticleEntity.entity_id == ent.id)
            co = {i for (i,) in db.execute(select(ArticleEntity.entity_id).where(ArticleEntity.article_id.in_(mine), ArticleEntity.entity_id != ent.id)
                                           .group_by(ArticleEntity.entity_id).order_by(func.count().desc()).limit(40))}
            for aid, eid in db.execute(select(ArticleEntity.article_id, ArticleEntity.entity_id).where(ArticleEntity.article_id.in_([r[0] for r in rows]))):
                by_article.setdefault(aid, set()).add(eid)
        kept = 0
        for aid, published in sorted(rows, key=lambda r: r[1], reverse=True):
            if aid not in public_docs or texts[aid][4]:
                continue
            o, hay, title_f, src, _ = texts[aid]
            name = cand[aid]
            f = _fold(name)
            if f in src or (_TITLE_TAIL.search(title_f) and f == _fold(_TITLE_TAIL.search(title_f).group(1)).strip()):
                continue      # 取得元の名前・題の末尾の出どころ
            in_title = _occurrences(title_f, unicodedata.normalize("NFKC", o.split("\n")[0]), name, longers[name]) > 0
            if not in_title and _occurrences(hay, o, name, longers[name]) < 2:
                continue      # 題に無く、抜粋に 1 回だけ（更新履歴の羅列など）は言及と言えない
            if _ambiguous(name) and co and aid in by_article and not (by_article[aid] & co):
                continue
            if _ambiguous(name) and aid not in by_article and not in_title:
                continue
            db.add(MentionCandidate(entity_id=ent.id, article_id=aid, matched=name[:200], status="candidate", published_at=published))
            kept += 1
            total += 1
            if kept >= MENTION_PER_ENTITY:
                break
    return {"entities": len(ents), "candidates": total}


def confirm_mention(db: Session, entity_id: int, article_id: int, *, accept: bool) -> dict | None:
    """候補を確定（article_entities へ足す）か却下する。コミットは呼ぶ側（writer.run の中）。無ければ None。"""
    m = db.scalar(select(MentionCandidate).where(MentionCandidate.entity_id == entity_id, MentionCandidate.article_id == article_id))
    if m is None or m.status != "candidate":
        return None
    if not accept:
        m.status = "dismissed"
        return {"status": "dismissed"}
    if db.scalar(select(ArticleEntity.article_id).where(ArticleEntity.entity_id == entity_id, ArticleEntity.article_id == article_id)) is None:
        db.add(ArticleEntity(article_id=article_id, entity_id=entity_id))
        db.flush()
        ent = db.get(Entity, entity_id)
        ent.article_count = int(db.scalar(select(func.count()).select_from(ArticleEntity).where(ArticleEntity.entity_id == entity_id)) or 0)
        art = db.get(NewsItem, article_id)
        if art is not None:
            if art.published_at and art.published_at.replace(tzinfo=None) > (ent.last_seen.replace(tzinfo=None) if ent.last_seen else datetime.min):
                ent.last_seen = art.published_at
            from qnapnewshub import analytics

            analytics.apply_entity_deltas(db, [(entity_id, art.published_at, art.dup_of, art.kind, art.user_id, art.source_key, 1)])
    m.status = "confirmed"
    return {"status": "confirmed"}


# ---- 全体の地図 ----

def _sql_dt(value: datetime) -> str:
    return kg._sql_dt(value)  # noqa: SLF001


def overview(db: Session, scopes: list[str], *, limit: int = OVERVIEW_LIMIT_DEFAULT, isolated: int = OVERVIEW_ISOLATED) -> dict:
    """最初に出す全体の地図。次数＋最近（RECENT_DAYS 日）の線 2 倍の重みの上位 limit 点・その間の線（確かさの順に OVERVIEW_EDGES_MAX 本まで）・まとまり・孤立した点。
    1 回の集計 SQL で次数を数える（物ごとに問い合わせない）。"""
    limit = max(10, min(int(limit), OVERVIEW_LIMIT_MAX))
    conn = db.connection()
    marks = ",".join("?" * len(scopes))
    recent = _sql_dt(_now() - timedelta(days=RECENT_DAYS))
    ranked = conn.exec_driver_sql(
        f"SELECT o.owner, o.deg, o.rec FROM (SELECT owner, count(*) AS deg, sum(CASE WHEN last_at >= ? THEN 1 ELSE 0 END) AS rec FROM "
        f"(SELECT subject_id AS owner, last_at FROM relationships WHERE sensitivity IN ({marks}) UNION ALL SELECT object_id, last_at FROM relationships WHERE sensitivity IN ({marks})) "
        f"GROUP BY owner) o JOIN entities e ON e.id = o.owner WHERE e.sensitivity IN ({marks}) ORDER BY o.deg + 2 * o.rec DESC, o.owner LIMIT ?",
        (recent, *scopes, *scopes, *scopes, limit)).fetchall()
    total_with = int(conn.exec_driver_sql(f"SELECT count(*) FROM (SELECT subject_id AS o FROM relationships WHERE sensitivity IN ({marks}) UNION SELECT object_id FROM relationships WHERE sensitivity IN ({marks}))",
                                          (*scopes, *scopes)).scalar() or 0)
    total_edges = int(conn.exec_driver_sql(f"SELECT count(*) FROM relationships WHERE sensitivity IN ({marks})", tuple(scopes)).scalar() or 0)
    deg = {int(o): (int(d), int(r or 0)) for o, d, r in ranked}
    ids = list(deg)
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(ids)))} if ids else {}
    comm = _community_of(db, ids)
    edges: list[dict] = []
    if ids:
        id_marks = ",".join("?" * len(ids))
        rows = conn.exec_driver_sql(
            f"SELECT {_REL} FROM relationships r WHERE r.sensitivity IN ({marks}) AND r.subject_id IN ({id_marks}) AND r.object_id IN ({id_marks}) "
            f"ORDER BY r.confidence DESC, r.evidence_count DESC, r.id LIMIT ?", (*scopes, *ids, *ids, OVERVIEW_EDGES_MAX + 1)).fetchall()
        truncated_edges = len(rows) > OVERVIEW_EDGES_MAX
        edges = [_edge(r) for r in rows[:OVERVIEW_EDGES_MAX]]
    else:
        truncated_edges = False
    nodes = [_node(ents[i], deg[i], comm.get(i, 0)) for i in ids if i in ents]
    iso: list[dict] = []
    if isolated:
        rows = db.scalars(select(Entity).where(Entity.sensitivity.in_(scopes), ~select(Relationship.id).where(or_(Relationship.subject_id == Entity.id, Relationship.object_id == Entity.id)).exists())
                          .order_by(Entity.article_count.desc(), Entity.id).limit(max(0, min(int(isolated), 500))))
        iso = [_node(e, (0, 0), 0) for e in rows]
    shown = {n["id"] for n in nodes}
    comms = []
    for c in db.scalars(select(Community).order_by(Community.size.desc(), Community.id).limit(60)):
        top = _loads_ids(c.top_ids)
        comms.append({"id": c.id, "name": c.name, "size": c.size, "rep_id": c.rep_id, "top": top[:5]})
    names = {e.id: e.name for e in db.scalars(select(Entity).where(Entity.id.in_({i for c in comms for i in c["top"]})))} if comms else {}
    members_shown: dict[int, int] = {}
    for n in nodes:
        if n["community"]:
            members_shown[n["community"]] = members_shown.get(n["community"], 0) + 1
    for c in comms:
        c["top"] = [{"id": i, "name": names[i]} for i in c["top"] if i in names and (i in shown or True)]
        c["shown"] = members_shown.get(c["id"], 0)
    layout = attach_layout(db, nodes)
    return {"nodes": nodes, "edges": edges, "isolated": iso, "communities": comms, "layout": layout,
            "totals": {"with_relations": total_with, "edges": total_edges, "shown_nodes": len(nodes), "shown_edges": len(edges)},
            "truncated": total_with > len(nodes) or truncated_edges, "limit": limit}


_REL = "r.id, r.subject_id, r.object_id, r.type, r.directed, r.confidence, r.status, r.evidence_count, r.last_at"


def _edge(r) -> dict:
    return {"id": r[0], "source": r[1], "target": r[2], "type": r[3], "directed": bool(r[4]), "confidence": round(float(r[5] or 0), 2), "status": r[6], "evidence_count": int(r[7] or 0),
            "last_at": (str(r[8])[:10] if r[8] else "")}


def _node(e: Entity, d: tuple[int, int], community: int) -> dict:
    return {"id": e.id, "name": e.name, "type": e.type, "article_count": int(e.article_count or 0), "degree": d[0], "recent": d[1], "community": community}


def attach_layout(db: Session, nodes: list[dict]) -> dict | None:
    """保存した座標（夜に ForceAtlas2 で計算）と重要度を、点に x・y・importance として付ける。保存が無い点には付けない（画面が近くから置く）。版（map_layout_ver）と計算した時刻を返す。"""
    ids = [n["id"] for n in nodes]
    got: dict[int, tuple[float, float, float]] = {}
    for i in range(0, len(ids), 800):
        for e, x, y, imp in db.execute(select(MapLayout.entity_id, MapLayout.x, MapLayout.y, MapLayout.importance).where(MapLayout.entity_id.in_(ids[i:i + 800]))):
            got[int(e)] = (float(x), float(y), float(imp))
    for n in nodes:
        if n["id"] in got:
            n["x"], n["y"], n["importance"] = got[n["id"]]
    ver, at = db.get(NewsMeta, "map_layout_ver"), db.get(NewsMeta, "map_layout_at")
    if ver is None or not ver.value:
        return None
    return {"version": int(ver.value) if ver.value.isdigit() else 0, "at": at.value if at else "", "placed": len(got)}


def _loads_ids(raw: str) -> list[int]:
    try:
        return [int(x) for x in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        return []


def _community_of(db: Session, ids: list[int]) -> dict[int, int]:
    out: dict[int, int] = {}
    for i in range(0, len(ids), 800):
        out.update({int(e): int(c) for e, c in db.execute(select(EntityCommunity.entity_id, EntityCommunity.community_id).where(EntityCommunity.entity_id.in_(ids[i:i + 800])))})
    return out


def community_members(db: Session, community_id: int, scopes: list[str], *, limit: int = 300) -> dict | None:
    """まとまりの中の点（次数の多い順 limit 点）と、その間の線。"""
    c = db.get(Community, community_id)
    if c is None:
        return None
    members = [i for (i,) in db.execute(select(EntityCommunity.entity_id).where(EntityCommunity.community_id == community_id))]
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(members), Entity.sensitivity.in_(scopes)))} if members else {}
    from qnapnewshub import kg_view

    deg = kg_view.degrees(db, list(ents), scopes)
    top = sorted(ents, key=lambda i: (-deg[i][0], i))[:max(1, min(limit, 1000))]
    edges: list[dict] = []
    if top:
        marks = ",".join("?" * len(top))
        sm = ",".join("?" * len(scopes))
        rows = db.connection().exec_driver_sql(f"SELECT {_REL} FROM relationships r WHERE r.sensitivity IN ({sm}) AND r.subject_id IN ({marks}) AND r.object_id IN ({marks}) "
                                               f"ORDER BY r.confidence DESC, r.id LIMIT 3000", (*scopes, *top, *top)).fetchall()
        edges = [_edge(r) for r in rows]
    nodes = [_node(ents[i], (deg[i][0], 0), c.id) for i in top]
    attach_layout(db, nodes)
    return {"id": c.id, "name": c.name, "size": c.size, "nodes": nodes, "edges": edges}


# ---- 主題の入口（MOC） ----

def _events_for(db: Session, member_ids: set[int], limit: int = 8, within_days: int = 60) -> list[dict]:
    since = _now() - timedelta(days=within_days)
    out = []
    for ev in db.scalars(select(Event).where(Event.last_at >= since).order_by(Event.last_at.desc()).limit(400)):
        try:
            ents = json.loads(ev.entities or "[]")
        except ValueError:
            continue
        hit = [e for e in ents if e.get("id") in member_ids]
        if hit:
            out.append({"id": ev.id, "title": ev.title, "n_articles": ev.n_articles, "last_at": kg.iso(ev.last_at), "summary": (ev.summary or "")[:160], "things": [e["name"] for e in hit[:3]]})
            if len(out) >= limit:
                break
    return out


def _questions_for(db: Session, ids: list[int], limit: int = 12) -> list[dict]:
    out = []
    if not ids:
        return out
    for eid, name, raw in db.execute(select(EntityCard.entity_id, Entity.name, EntityCard.questions).join(Entity, Entity.id == EntityCard.entity_id).where(EntityCard.entity_id.in_(ids))):
        for q in _loads(raw)[:2]:
            out.append({"entity_id": eid, "entity": name, "question": q})
    rank = {i: n for n, i in enumerate(ids)}
    out.sort(key=lambda r: rank.get(r["entity_id"], 10**6))
    return out[:limit]


def _things_rows(db: Session, ids: list[int], scopes: list[str]) -> list[dict]:
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(ids), Entity.sensitivity.in_(scopes)))} if ids else {}
    defs = definitions(db, list(ents))
    return [{"id": i, "name": ents[i].name, "type": ents[i].type, "article_count": int(ents[i].article_count or 0), "last_seen": kg.iso(ents[i].last_seen)[:10], "definition": defs.get(i, "")}
            for i in ids if i in ents]


def moc_community(db: Session, community_id: int, scopes: list[str]) -> dict | None:
    c = db.get(Community, community_id)
    if c is None:
        return None
    members = [i for (i,) in db.execute(select(EntityCommunity.entity_id).where(EntityCommunity.community_id == community_id))]
    ents = list(db.scalars(select(Entity).where(Entity.id.in_(members), Entity.sensitivity.in_(scopes)).order_by(Entity.article_count.desc(), Entity.id).limit(400)))
    ordered = [e.id for e in ents]
    return {"kind": "community", "id": c.id, "title": f"{c.name} のまとまり", "size": c.size, "things": _things_rows(db, ordered[:20], scopes), "events": _events_for(db, set(ordered)),
            "questions": _questions_for(db, ordered[:60])}


def moc_channel(db: Session, channel: dict, scopes: list[str], days: int = 30) -> dict:
    from qnapnewshub import channels as channels_mod

    since = _now() - timedelta(days=days)
    rows = db.execute(select(NewsItem.id, NewsItem.kind, NewsItem.source_key, NewsItem.title, NewsItem.summary).where(NewsItem.published_at >= since, NewsItem.dup_of.is_(None))
                      .order_by(NewsItem.published_at.desc()).limit(4000)).all()
    ids = [r[0] for r in rows if channels_mod.matches(channel, kind=r[1], source_key=r[2], title=r[3], summary=r[4] or "")]
    counts: dict[int, int] = {}
    for i in range(0, len(ids), 400):
        for eid, n in db.execute(select(ArticleEntity.entity_id, func.count()).where(ArticleEntity.article_id.in_(ids[i:i + 400])).group_by(ArticleEntity.entity_id)):
            counts[eid] = counts.get(eid, 0) + int(n)
    top = sorted(counts, key=lambda i: (-counts[i], i))[:80]
    visible = [t["id"] for t in _things_rows(db, top, scopes)]
    things = _things_rows(db, visible[:20], scopes)
    for t in things:
        t["recent_articles"] = counts[t["id"]]
    events = []
    if ids:
        ev_ids = [e for (e,) in db.execute(select(func.distinct(EventArticle.event_id)).where(EventArticle.article_id.in_(ids[:800])))]
        for ev in db.scalars(select(Event).where(Event.id.in_(ev_ids)).order_by(Event.last_at.desc()).limit(8)):
            events.append({"id": ev.id, "title": ev.title, "n_articles": ev.n_articles, "last_at": kg.iso(ev.last_at), "summary": (ev.summary or "")[:160], "things": []})
    return {"kind": "channel", "id": channel["id"], "title": f"チャンネル「{channel['name']}」", "size": len(ids), "things": things, "events": events, "questions": _questions_for(db, visible[:60])}


def moc_index(db: Session, channel_list: list[dict]) -> dict:
    comms = [{"id": c.id, "name": c.name, "size": c.size} for c in db.scalars(select(Community).order_by(Community.size.desc(), Community.id).limit(30))]
    return {"communities": comms, "channels": [{"id": c["id"], "name": c["name"], "color": c.get("color", "")} for c in channel_list]}
