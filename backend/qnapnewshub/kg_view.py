"""知識カード・知識の地図（neighbors）・Obsidian の書き出し（v0.5）。読むだけ（メモの保存だけ書く）。ARCHITECTURE 6 章。

- すべて scopes（読んでよい機密の区分。既定は public だけ）で絞る。confidential は管理者（ログイン）だけが指定できる（kg_router）。
- **neighbors は段ごとにまとめて返す**: 物の id の一覧から、辺の型・確かさ・期間・物の種類で絞った辺を確かさの順に上限つきで読み、新しく出た物を次の段の入り口にする（深さ最大 5。既定の検索は 1・地図のローカルは 1〜5）。
  1 段 1 回の SQL（IN）で、物ごとに 1 回ずつ問い合わせない。一度に全部を送らず、辺の合計は limit（最大 1,000）まで。v0.6 で ControlDeck の MPKR が使う道具にもなる。
- 利用者のメモ（entity_notes）は AI の部分（entity_cards）と別の表。再分析・カードの作り直しは触れない。
- Obsidian の書き出しは範囲を指定したときだけ（常に書き出さない）。物ごとに 1 つの .md（front matter・[[関係する物]]・根拠の URL）を zip にする。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from qnapnewshub import entities as entities_mod
from qnapnewshub import kg
from qnapnewshub.db import ArticleEntity, Claim, Document, Entity, EntityAlias, EntityCard, EntityNote, Evidence, NewsItem, Relationship, utcnow
from qnapnewshub.extract import ENTITY_TYPES

NEIGHBOR_LIMIT_DEFAULT, NEIGHBOR_LIMIT_MAX, NEIGHBOR_IDS_MAX, NEIGHBOR_DEPTH_MAX = 200, 1000, 50, 5
NOTE_MAX = 20000
EXPORT_LIMIT_MAX = 2000
STATUS_LABEL = {"extracted": "AI の取り出し（原文に引用あり）", "inferred": "推測", "statistical": "一緒に出る集計", "confirmed": "確認済み"}


def node_dict(e: Entity) -> dict:
    return {"id": e.id, "name": e.name, "type": e.type, "article_count": int(e.article_count or 0), "sensitivity": e.sensitivity, "last_seen": kg.iso(e.last_seen)}


def edge_dict(r: Relationship) -> dict:
    return {"id": r.id, "source": r.subject_id, "target": r.object_id, "type": r.type, "directed": bool(r.directed), "confidence": round(float(r.confidence or 0), 3),
            "status": r.status, "evidence_count": int(r.evidence_count or 0), "first_at": kg.iso(r.first_at), "last_at": kg.iso(r.last_at), "sensitivity": r.sensitivity}


# ---- 地図（neighbors）と辺の読み出し（PPR も使う） ----

REL_COLS = "r.id, r.subject_id, r.object_id, r.type, r.directed, r.confidence, r.status, r.evidence_count, r.first_at, r.last_at, r.sensitivity"
ORDERS = {"weight": "{p}confidence DESC, {p}evidence_count DESC, {p}id", "recent": "{p}last_at IS NULL, {p}last_at DESC, {p}confidence DESC, {p}id",
          "evidence": "{p}evidence_count DESC, {p}confidence DESC, {p}id"}
DIRECTIONS = ("both", "out", "in")
HUB_K_MAX = 500
_CHUNK = 150      # 1 回の SQL に入れる物の数（SQLite の複合 SELECT の上限 500 の内側。UNION ALL の部品は物 1 つにつき out・in の 2 つ）


def _sql_dt(value: datetime) -> str:
    return kg.iso(value).replace("T", " ").replace("Z", ".000000")      # DateTime 列と同じ書式の文字列（datetime をそのまま束縛しない: Python 3.12 の非推奨を避ける）


def _iso_str(raw) -> str:
    s = str(raw or "")
    return f"{s[:10]}T{s[11:19]}Z" if len(s) >= 19 else ""


def edge_from_row(row, via: int | None = None) -> dict:
    """REL_COLS の順の行 → edge_dict と同じ形（ORM を作らずに速く）。"""
    out = {"id": row[0], "source": row[1], "target": row[2], "type": row[3], "directed": bool(row[4]), "confidence": round(float(row[5] or 0), 3), "status": row[6],
           "evidence_count": int(row[7] or 0), "first_at": _iso_str(row[8]), "last_at": _iso_str(row[9]), "sensitivity": row[10]}
    if via is not None:
        out["via"] = via
    return out


def direction_plan(types: list[str], direction: str, type_directions: dict[str, str] | None) -> tuple[list[str], list[str]]:
    """辺の型ごとの向き（物から見て out＝その物が主語・in＝その物が目的語・both）を、(out の側で許す型, in の側で許す型) にする。
    向きの無い型（co_occurs・competes・contradicts）と directed=0 の辺は、向きの指定に関わらず両方の側で許す（SQL の `directed = 0`）。"""
    td = type_directions or {}
    use = [t for t in (types or kg.REL_TYPES) if t in kg.REL_TYPES]
    out_ok, in_ok = [], []
    for t in use:
        d = td.get(t, direction)
        if d in ("out", "both"):
            out_ok.append(t)
        if d in ("in", "both"):
            in_ok.append(t)
    return out_ok, in_ok


def _part(col: str, n: int, allowed: list[str], all_types: list[str], extra: str) -> tuple[str, list]:
    """物の列（subject_id か object_id）が n 個の物のどれかである辺の WHERE。向きの条件は、allowed が全部の型なら付けない。引数の並び: 物の値, 向きの型, extra の値。"""
    where = f"r.{col} IN ({','.join('?' * n)})"
    args: list = []
    if set(allowed) != set(all_types):
        where += f" AND (r.directed = 0 OR r.type IN ({','.join('?' * len(allowed))}))" if allowed else " AND r.directed = 0"
        args = list(allowed)
    return f"{where} AND {extra}", args


def _filters(scopes, types, direction, type_directions, min_confidence, since, until):
    """辺の絞り込み（型・向き・確かさ・期間・区分）の SQL の部品: (使う型, out 側で許す型, in 側で許す型, 共通の WHERE, その引数)。"""
    use = [t for t in (types or kg.REL_TYPES) if t in kg.REL_TYPES]
    out_ok, in_ok = direction_plan(use, direction, type_directions)
    extra = f"r.sensitivity IN ({','.join('?' * len(scopes))}) AND r.confidence >= ?"
    extra_args: list = [*scopes, float(min_confidence)]
    if set(use) != set(kg.REL_TYPES):
        extra += f" AND r.type IN ({','.join('?' * len(use))})"
        extra_args += use
    if since:
        extra += " AND (r.last_at IS NULL OR r.last_at >= ?)"
        extra_args.append(_sql_dt(since))
    if until:
        extra += " AND (r.first_at IS NULL OR r.first_at <= ?)"
        extra_args.append(_sql_dt(until))
    return use, out_ok, in_ok, extra, extra_args


def adjacency(db: Session, owners: list[int], *, scopes: list[str], k: int | None, types: list[str] | None = None, direction: str = "both",
              type_directions: dict[str, str] | None = None, min_confidence: float = 0.0, since: datetime | None = None, until: datetime | None = None) -> dict[int, list[tuple[int, float]]]:
    """PPR 用の軽い隣接リスト {物: [(隣の物, 確かさ)]（確かさの高い順・k 本まで）}。必要な列だけ読む。
    次数が 2k 以下の物はまとめて 1 回の SQL で読んで Python で k 本に絞り、それより大きい物（ハブ）は物ごとに索引の順で k 本で止める（全部は読まない）。"""
    use, out_ok, in_ok, extra, extra_args = _filters(scopes, types, direction, type_directions, min_confidence, since, until)
    owners = list(dict.fromkeys(int(o) for o in owners))
    out: dict[int, list[tuple[int, float]]] = {o: [] for o in owners}
    if not use or not owners:
        return out
    conn = db.connection()
    deg = degrees(db, owners, scopes) if k else {}
    big = [o for o in owners if k and deg.get(o, (0, ""))[0] > 2 * k]
    small = [o for o in owners if o not in set(big)]
    for i in range(0, len(small), 400):
        chunk = small[i:i + 400]
        parts, args = [], []
        for col, other, allowed in (("subject_id", "object_id", out_ok), ("object_id", "subject_id", in_ok)):
            where, a = _part(col, len(chunk), allowed, use, extra)
            parts.append(f"SELECT r.{col}, r.{other}, r.confidence FROM relationships r WHERE {where}")
            args += [*chunk, *a, *extra_args]
        for owner, nb, conf in conn.exec_driver_sql(" UNION ALL ".join(parts), tuple(args)).fetchall():
            out[owner].append((nb, conf))
    for o in big:
        for col, other, allowed in (("subject_id", "object_id", out_ok), ("object_id", "subject_id", in_ok)):
            where, a = _part(col, 1, allowed, use, extra)
            sql = f"SELECT r.{other}, r.confidence FROM relationships r WHERE {where} ORDER BY r.confidence DESC LIMIT ?"
            out[o] += [(nb, conf) for nb, conf in conn.exec_driver_sql(sql, tuple([o, *a, *extra_args, k])).fetchall()]
    for o in owners:
        rows = out[o]
        rows.sort(key=lambda t: (-(t[1] or 0), t[0]))
        if k and len(rows) > k:
            del rows[k:]
    return out


def edge_rows(db: Session, owners: list[int], *, scopes: list[str], types: list[str] | None = None, direction: str = "both", type_directions: dict[str, str] | None = None,
              min_confidence: float = 0.0, since: datetime | None = None, until: datetime | None = None, order: str = "weight", per_owner: int | None = None,
              total: int = 1000) -> list[tuple[int, tuple]]:
    """物の一覧（owners）につく辺を [(owner, 行)] で返す。owner はその辺の「入り口の側の物」（辺 1 本が両端とも owners なら 2 回出る）。
    - 辺の型で絞る（types）・型ごとの向き（direction・type_directions）・確かさ・期間。区分（scopes）の外の辺は出ない。
    - per_owner（ハブの抑制）を渡すと、物ごとに order の良い順に per_owner 本までだけ読む（索引の順で止まる＝次数の大きい物でも全部は読まない）。
    - per_owner が無いときは、全体で order の良い順に total 本まで 1 回の SQL で読む。
    読み出しの上限は呼ぶ側の total（per_owner のときは per_owner × 物の数）。"""
    if not owners:
        return []
    use, out_ok, in_ok, extra, extra_args = _filters(scopes, types, direction, type_directions, min_confidence, since, until)
    if not use:
        return []
    order_sql = ORDERS.get(order, ORDERS["weight"])
    conn = db.connection()
    found: list[tuple[int, tuple]] = []
    owners = list(dict.fromkeys(int(o) for o in owners))
    if per_owner:
        k = max(1, min(int(per_owner), HUB_K_MAX))
        for i in range(0, len(owners), _CHUNK):
            parts, args = [], []
            for o in owners[i:i + _CHUNK]:
                for col, allowed in (("subject_id", out_ok), ("object_id", in_ok)):
                    where, a = _part(col, 1, allowed, use, extra)
                    parts.append(f"SELECT * FROM (SELECT {REL_COLS}, ? AS owner FROM relationships r WHERE {where} ORDER BY {order_sql.format(p='r.')} LIMIT ?)")
                    args += [o, o, *a, *extra_args, k]
            found += [(row[-1], row[:-1]) for row in conn.exec_driver_sql(" UNION ALL ".join(parts), tuple(args)).fetchall()]
        found.sort(key=lambda t: _row_key(order, t[1]))      # 物 1 つにつき（出る辺・入る辺を合わせて）k 本まで
        count: dict[int, int] = {}
        kept = []
        for owner, row in found:
            count[owner] = count.get(owner, 0) + 1
            if count[owner] <= k:
                kept.append((owner, row))
        found = kept
    else:
        cap = max(1, int(total))
        for i in range(0, len(owners), 400):
            chunk = owners[i:i + 400]
            parts, args = [], []
            for col, allowed in (("subject_id", out_ok), ("object_id", in_ok)):
                where, a = _part(col, len(chunk), allowed, use, extra)
                parts.append(f"SELECT {REL_COLS}, r.{col} AS owner FROM relationships r WHERE {where}")
                args += [*chunk, *a, *extra_args]
            sql = f"SELECT * FROM ({' UNION ALL '.join(parts)}) r ORDER BY {order_sql.format(p='r.')} LIMIT ?"
            found += [(row[-1], row[:-1]) for row in conn.exec_driver_sql(sql, tuple(args + [cap])).fetchall()]
    return found


def degrees(db: Session, ids: list[int], scopes: list[str]) -> dict[int, tuple[int, str]]:
    """{物の id: (次数＝区分の範囲の中の辺の数, 最後の辺の日時)}。辺の無い物は (0, "")。索引（subject_id・sensitivity・last_at）だけで数える。"""
    out: dict[int, tuple[int, str]] = {int(i): (0, "") for i in ids}
    conn_sql = ",".join("?" * len(scopes))
    ids = list(out)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        sql = (f"SELECT owner, count(*), max(last_at) FROM (SELECT subject_id AS owner, last_at FROM relationships WHERE subject_id IN ({marks}) AND sensitivity IN ({conn_sql}) "
               f"UNION ALL SELECT object_id, last_at FROM relationships WHERE object_id IN ({marks}) AND sensitivity IN ({conn_sql})) GROUP BY owner")
        for owner, n, last in db.connection().exec_driver_sql(sql, tuple([*chunk, *scopes, *chunk, *scopes])):
            out[int(owner)] = (int(n), _iso_str(last))
    return out


def node_with(e: Entity, deg: dict[int, tuple[int, str]]) -> dict:
    d = node_dict(e)
    n, last = deg.get(e.id, (0, ""))
    d["degree"], d["last_edge_at"] = n, last
    return d


def neighbors(db: Session, ids: list[int], *, types: list[str] | None = None, depth: int = 1, limit: int = NEIGHBOR_LIMIT_DEFAULT, scopes: list[str] | None = None,
              min_confidence: float = 0.0, kinds: list[str] | None = None, since: datetime | None = None, until: datetime | None = None,
              direction: str = "both", type_directions: dict[str, str] | None = None, order: str = "weight", only_contradicts: bool = False,
              hub_k: int | None = None, deadline: float | None = None) -> dict:
    """{"seeds": [物], "levels": [{"depth", "nodes": [新しい物], "edges": [辺]}], "truncated": 上限で切ったか, "suppressed": [ハブ], "stopped": 時間で止めた段}。
    見えない（区分の範囲外の）物・辺は出さない。物には degree（区分の範囲の辺の数）・last_edge_at（最後の辺の日時）が付く。

    - **向き**: direction（既定 both）か、型ごと type_directions {"uses": "out", "part_of": "in"}。out＝入り口の物が主語（その物から出る）、in＝入り口の物が目的語（逆向きにたどる）。
      向きの無い型（co_occurs・competes・contradicts）は常に両方。
    - **時系列**: order=recent は最後の日時の新しい順（既定 weight は確かさ→根拠の数の順）。since/until と合わせて「最近の変化」を取る。
    - **矛盾だけ**: only_contradicts は型を contradicts に固定する。
    - **ハブの抑制**: hub_k を渡すと、入り口の物 1 つにつき order の良い順に hub_k 本までしか読まない（読み捨てた物は suppressed に次数と読んだ数を出す）。
    - deadline（time.monotonic の時刻）を過ぎたら 2 段目以降へ行かず、いまの結果を返す（stopped に段）。"""
    import time

    scopes = scopes or ["public"]
    ids = list(dict.fromkeys(int(i) for i in ids))[:NEIGHBOR_IDS_MAX]
    depth = max(1, min(int(depth), NEIGHBOR_DEPTH_MAX))
    limit = max(1, min(int(limit), NEIGHBOR_LIMIT_MAX))
    if only_contradicts:
        types = ["contradicts"]
    hub = max(1, min(int(hub_k), HUB_K_MAX)) if hub_k else None
    seeds = [e for e in db.scalars(select(Entity).where(Entity.id.in_(ids), Entity.sensitivity.in_(scopes)))] if ids else []
    known: dict[int, Entity] = {e.id: e for e in seeds}
    deg = degrees(db, list(known), scopes) if known else {}
    frontier = list(known)
    seen_edges: set[int] = set()
    levels: list[dict] = []
    suppressed: list[dict] = []
    truncated = False
    stopped = 0
    remaining = limit
    for d in range(1, depth + 1):
        if not frontier or remaining <= 0:
            break
        if d > 1 and deadline is not None and time.monotonic() > deadline:      # 1 段目は必ず返す（期限は次の段へ進むかの判断）
            stopped, truncated = d, True
            break
        rows = edge_rows(db, frontier, scopes=scopes, types=types, direction=direction, type_directions=type_directions, min_confidence=min_confidence, since=since, until=until,
                         order=order, per_owner=hub, total=remaining * 3 + 50)
        if hub:
            suppressed += [{"id": f, "degree": deg[f][0], "kept": hub} for f in frontier if deg.get(f, (0, ""))[0] > hub]
        rows.sort(key=lambda t: _row_key(order, t[1]))
        other_ids = {x for _, r in rows for x in (r[1], r[2])} - set(known)
        fetched = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(other_ids), Entity.sensitivity.in_(scopes)))} if other_ids else {}
        edges, nodes = [], {}
        for owner, r in rows:
            if r[0] in seen_edges:
                continue
            ends = [known.get(x) or fetched.get(x) for x in (r[1], r[2])]
            if not all(ends):
                continue
            new = [e for e in ends if e.id not in known and e.id not in nodes]
            if any(kinds and e.type not in kinds for e in ends if e.id not in known):
                continue
            if len(edges) >= remaining:
                truncated = True
                break
            seen_edges.add(r[0])
            edges.append(edge_from_row(r, via=owner))
            for e in new:
                nodes[e.id] = e
        known.update(nodes)
        remaining -= len(edges)
        deg.update(degrees(db, list(nodes), scopes) if nodes else {})
        levels.append({"depth": d, "nodes": [node_with(e, deg) for e in nodes.values()], "edges": edges})
        frontier = list(nodes)
    return {"seeds": [node_with(e, deg) for e in seeds], "levels": levels, "truncated": truncated, "suppressed": suppressed, "stopped": stopped}


def _row_key(order: str, r: tuple):
    """edge_rows の order と同じ並び（複数の塊・物ごとの読みをまとめ直すとき）。r は REL_COLS の順の行。"""
    conf, ev, last = -float(r[5] or 0), -int(r[7] or 0), r[9]
    if order == "recent":
        return (last is None, "" if last is None else _neg(last), conf, r[0])
    if order == "evidence":
        return (ev, conf, r[0])
    return (conf, ev, r[0])


def _neg(s: str) -> str:
    """文字列の降順のための反転（日時の文字列は同じ長さの数字と記号。各文字を反転して昇順に並べ直す）。"""
    return "".join(chr(0x10FFFF - ord(c)) for c in str(s))


def seeds(db: Session, *, limit: int = 12, scopes: list[str] | None = None) -> list[dict]:
    """地図の入り口の候補: 関係をもつ物のうち、記事の多い順。"""
    scopes = scopes or ["public"]
    with_rel = select(Relationship.subject_id).union(select(Relationship.object_id))
    rows = db.scalars(select(Entity).where(Entity.id.in_(with_rel), Entity.sensitivity.in_(scopes)).order_by(Entity.article_count.desc(), Entity.id).limit(max(1, min(limit, 50))))
    return [node_dict(e) for e in rows]


# ---- 知識カード ----

def _topics_of(db: Session, entity_id: int) -> list[str]:
    counts: dict[str, int] = {}
    for (raw,) in db.execute(select(NewsItem.topics).join(ArticleEntity, ArticleEntity.article_id == NewsItem.id).where(ArticleEntity.entity_id == entity_id)
                             .order_by(NewsItem.published_at.desc()).limit(60)):
        try:
            for t in json.loads(raw or "[]"):
                counts[str(t)] = counts.get(str(t), 0) + 1
        except ValueError:
            pass
    return [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:8]]


def evidence_rank(evs: list[dict]) -> int:
    """主張の根拠の強さ（小さいほど強い）。根拠の段階 tier（exact 0・paraphrase 1・none 2）の列があればその最強、無ければ原文に照合できた根拠（grounded）が 1 つでもあれば 0・根拠があるが照合できなければ 1・根拠なしは 2。
    カードの「主張と根拠」・context の facts の並べ替えに使う（強い根拠が先）。"""
    if any(e.get("grounded") for e in evs):
        return 0
    tiers = [e["tier"] for e in evs if e.get("tier") in kg.TIER_RANK]
    if tiers:
        return 2 - max(kg.TIER_RANK[t] for t in tiers)
    return 1 if evs else 2


def _claims_with_evidence(db: Session, entity_id: int, scopes: list[str], limit: int = 50) -> list[dict]:
    """主張と根拠。並びは根拠の段階（一致＞言い換え＞無し）→新しい順（2026-11-25）。各主張に tier・stated_at（日付の既定は記事の公開日）を付ける。"""
    claims = db.scalars(select(Claim).where(Claim.entity_id == entity_id, Claim.sensitivity.in_(scopes)).order_by(Claim.id.desc()).limit(limit * 3)).all()
    evs: dict[int, list] = {}
    if claims:
        for ev, d in db.execute(select(Evidence, Document).join(Document, Document.id == Evidence.document_id).where(Evidence.claim_id.in_([c.id for c in claims]))):
            if d.sensitivity in scopes:
                evs.setdefault(ev.claim_id, []).append({"document": {"id": d.id, "kind": d.kind, "title": d.title, "url": d.url, "source": d.source, "published_at": kg.iso(d.published_at)}, "quote": ev.quote, "start": ev.start, "end": ev.end,
                                                         "page": ev.page, "quote_hash": ev.quote_hash, "grounded": bool(ev.grounded), **({"tier": ev.tier} if ev.tier else {})})      # tier は段階の列に値があるときだけ（古い行は grounded だけで並べる）

    def tier(c: Claim) -> str:
        best = max((kg.TIER_RANK[kg.tier_of(bool(e["grounded"]), e.get("tier", ""))] for e in evs.get(c.id, [])), default=0)
        return {2: "exact", 1: "paraphrase", 0: "none"}[best]

    rows = sorted(claims, key=lambda c: (-kg.TIER_RANK[tier(c)], -c.id))[:limit]
    out = []
    for c in rows:
        docs = evs.get(c.id, [])
        out.append({"id": c.id, "kind": c.kind, "text": c.text, "value": c.value, "ai_extracted": bool(c.ai_extracted), "model": c.model, "sensitivity": c.sensitivity,
                    "tier": tier(c), "stated_at": kg.iso(c.stated_at) or (docs[0]["document"]["published_at"] if docs else ""), "evidences": docs})
    return out


def relations_of(db: Session, entity_id: int, scopes: list[str], limit: int = 60) -> list[dict]:
    rows = db.scalars(select(Relationship).where(or_(Relationship.subject_id == entity_id, Relationship.object_id == entity_id), Relationship.sensitivity.in_(scopes))
                      .order_by(Relationship.confidence.desc(), Relationship.evidence_count.desc(), Relationship.id).limit(limit)).all()
    others = {r.object_id if r.subject_id == entity_id else r.subject_id for r in rows}
    ents = {e.id: e for e in db.scalars(select(Entity).where(Entity.id.in_(others), Entity.sensitivity.in_(scopes)))} if others else {}
    out = []
    for r in rows:
        other = ents.get(r.object_id if r.subject_id == entity_id else r.subject_id)
        if other is None:
            continue
        out.append({**edge_dict(r), "direction": "both" if not r.directed else ("out" if r.subject_id == entity_id else "in"), "other": {"id": other.id, "name": other.name, "type": other.type},
                    "status_label": STATUS_LABEL.get(r.status, r.status)})
    return out


def card(db: Session, entity_id: int, scopes: list[str] | None = None) -> dict | None:
    scopes = scopes or ["public"]
    ent = db.get(Entity, entity_id)
    if ent is None or ent.sensitivity not in scopes:
        return None
    base = entities_mod.detail(db, entity_id, limit=30, scopes=scopes) or {}
    cd = db.get(EntityCard, entity_id)
    note = db.get(EntityNote, entity_id)
    from qnapnewshub import things

    rels = relations_of(db, entity_id, scopes)
    claims = _claims_with_evidence(db, entity_id, scopes)
    claims.sort(key=lambda c: evidence_rank(c["evidences"]))      # カードの「主張と根拠」は、強い根拠（原文に照合できたもの）が先
    return {"id": ent.id, "name": ent.name, "type": ent.type, "sensitivity": ent.sensitivity, "ai_use": ent.ai_use,
            "overview": {"text": cd.summary, "model": cd.model, "at": kg.iso(cd.at), "ai": True} if cd and cd.summary else None,
            "elements": {"aliases": base.get("aliases", []), "first_seen": kg.iso(ent.first_seen), "last_seen": kg.iso(ent.last_seen), "article_count": ent.article_count,
                         "topics": _topics_of(db, entity_id)},
            "timeline": base.get("timeline", []), "claims": claims, "relations": rels,
            "brief": things.brief(db, ent), "groups": things.relation_groups(rels, things.definitions(db, [r["other"]["id"] for r in rels])),
            "questions": things._loads(cd.questions) if cd else [], "opposing": things._loads(cd.opposing) if cd else [],
            "metrics": things.metric_rows(db, ent, claims), "mentions": things.mentions_of(db, entity_id),
            "history": kg.history(db, entity_id, scopes), "note": {"body": note.body if note else "", "updated_at": kg.iso(note.updated_at) if note else ""}}


def save_note(db: Session, entity_id: int, body: str, user_id: int) -> dict | None:
    """利用者のメモを保存する（コミットは呼ぶ側。writer.run の中）。AI の部分には触れない。長すぎれば ValueError。"""
    if len(body) > NOTE_MAX:
        raise ValueError(f"メモは {NOTE_MAX} 文字までです")
    if db.get(Entity, entity_id) is None:
        return None
    note = db.get(EntityNote, entity_id)
    if note is None:
        note = EntityNote(entity_id=entity_id, body=body, updated_by=user_id)
        db.add(note)
    else:
        note.body, note.updated_by, note.updated_at = body, user_id, utcnow()
    db.flush()
    return {"body": note.body, "updated_at": kg.iso(note.updated_at)}


# ---- Obsidian の書き出し ----

_BAD = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')
REL_LABEL = {"uses": "使う", "supports": "対応する", "replaces": "置き換える", "competes": "競う", "part_of": "一部", "implements": "実装する", "cites": "引用する",
             "contradicts": "矛盾する", "made_by": "作った組織", "develops": "開発する", "co_occurs": "一緒に出る", "other": "関係"}


def safe_name(name: str) -> str:
    return (_BAD.sub(" ", name).strip(" .") or "名前なし")[:80]


def select_entities(db: Session, *, ids: list[int] | None, types: list[str] | None, since: datetime | None, until: datetime | None, min_articles: int,
                    scopes: list[str], limit: int) -> list[Entity]:
    conds = [Entity.sensitivity.in_(scopes)]
    if ids:
        conds.append(Entity.id.in_(ids))
    if types:
        conds.append(Entity.type.in_([t for t in types if t in ENTITY_TYPES]))
    if since:
        conds.append(Entity.last_seen >= since)
    if until:
        conds.append(Entity.first_seen <= until)
    if min_articles:
        conds.append(Entity.article_count >= min_articles)
    return list(db.scalars(select(Entity).where(*conds).order_by(Entity.article_count.desc(), Entity.id).limit(max(1, min(limit, EXPORT_LIMIT_MAX)))))


def _yaml_list(items: list) -> str:
    return json.dumps(items, ensure_ascii=False)


def _sources_for_pair(db: Session, a: int, b: int, n: int = 3) -> list[tuple[str, str]]:
    a1, a2 = ArticleEntity.__table__.alias("a1"), ArticleEntity.__table__.alias("a2")
    rows = db.execute(select(NewsItem.title, NewsItem.url).join(a1, a1.c.article_id == NewsItem.id).join(a2, and_(a2.c.article_id == NewsItem.id, a2.c.entity_id == b))
                      .where(a1.c.entity_id == a).order_by(NewsItem.published_at.desc()).limit(n))
    return [(t, u) for t, u in rows if u]


README_VAULT = """# このフォルダについて（vault の使い方）

QNAP ニュースハブの Things（知識の単位）を、Obsidian で開ける Markdown にしたものです。LLM に読ませるときは、まずこのファイルを読ませてください。

## 規約
- 1 つの Things が 1 ファイル（`種類/名前.md`）。先頭の `---` で囲んだ部分（frontmatter）に、type（種類）・status（状態: official 正式公開・experimental 実験的・deprecated 廃止・unknown 不明）・aliases（別名）・confidence（確かさ 0〜1。根拠の文書の数だけで決めた値）・updated（最新の根拠の日）・sources（根拠の URL）があります。
- `## Connections` は関係を型ごとに分けた節です（上位・下位・仲間・使う・使われる・置き換える・置き換えられる・矛盾・その他）。`[[名前]]` で相手へ移れます。関係の後ろの「推測」は、原文に根拠が見つからなかった関係です。確定として扱わないでください。
- `## Open Questions` は未解決の問い、`## Opposing Views` は反対の見方です。ここに書かれていないことを、事実として補わないでください。
- `_MOC/` は主題の入口（まとまりごと）です。主要な Things・最近の出来事・未解決の問いが並びます。調べ始めるときの入口に使います。
- `[AI]` の付いた記述は AI が取り出したもので、確かめた事実ではありません。根拠の URL を開いて確かめてください。
- 既定では public（公開）の Things だけを書き出しています。

## LLM の使い方の目安
1. `_MOC/` から主題を選ぶ。2. 気になる Things を開き、frontmatter の status と confidence を見る。3. `## Connections` で関係をたどる。4. 断定する前に根拠の URL と Open Questions を確かめる。
"""


def _frontmatter(e: Entity, c: dict, exported: str, srcs: list[str]) -> list[str]:
    b = c["brief"]
    fm = ["---", f"id: {e.id}", f"name: {json.dumps(e.name, ensure_ascii=False)}", f"type: {e.type}", f"status: {b['state']['key']}", f"aliases: {_yaml_list(c['elements']['aliases'])}",
          f"confidence: {b['confidence']}", f"updated: {json.dumps((b['latest_evidence_at'] or '')[:10])}", f"sources: {_yaml_list(srcs)}",
          f"first_seen: {json.dumps(c['elements']['first_seen'])}", f"last_seen: {json.dumps(c['elements']['last_seen'])}", f"article_count: {e.article_count}",
          f"sensitivity: {e.sensitivity}", f"ai_use: {e.ai_use}", f"exported_at: {json.dumps(exported)}", "tags: [qnapnewshub]", "---", ""]
    return fm


def export_zip(db: Session, entities: list[Entity], scopes: list[str], *, include_notes: bool = True) -> tuple[bytes, dict]:
    """物ごとの .md と、_MOC/（まとまりごとの入口）・README.md（vault の使い方）を zip にする。リンクは範囲の中の物だけ（範囲外の機密の物の名前を漏らさない）。(zip の中身, {"files", "scopes"}) を返す。
    各ファイル: frontmatter（type・status・aliases・confidence・updated・sources）・`## Connections`（型ごと）・`## Open Questions`・根拠。"""
    from qnapnewshub import things
    from qnapnewshub.db import Community, EntityCommunity

    names: dict[int, str] = {}
    used: set[str] = set()
    for e in entities:
        n = safe_name(e.name)
        if n.casefold() in used:
            n = f"{n} ({e.id})"
        used.add(n.casefold())
        names[e.id] = n
    exported = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", README_VAULT)
        for e in entities:
            c = card(db, e.id, scopes)
            if c is None:
                continue
            srcs = [u for (u,) in db.execute(select(NewsItem.url).join(ArticleEntity, ArticleEntity.article_id == NewsItem.id).where(ArticleEntity.entity_id == e.id, NewsItem.url != "")
                                             .order_by(NewsItem.published_at.desc()).limit(5))]
            fm = _frontmatter(e, c, exported, srcs)
            body = [f"# {e.name}", ""]
            if c["brief"]["definition"]:
                body += [f"> {c['brief']['definition']} [AI]", ""]
            if c["overview"]:
                body += ["## 概要（AI が作った要約）", c["overview"]["text"], f"（{c['overview']['model']}。確かめた事実ではありません）", ""]
            if c["groups"]:
                body += ["## Connections"]
                for g in c["groups"]:
                    body.append(f"### {g['label']}")
                    for r in g["items"]:
                        other = r["other"]
                        link = names.get(other["id"]) or safe_name(other["name"])
                        arrow = {"out": "→", "in": "←", "both": "↔"}[r["direction"]]
                        line = f"- {arrow} {REL_LABEL.get(r['type'], r['type'])} [[{link}]]（確かさ {r['confidence']}・{r['status_label']}・根拠 {r['evidence_count']} 件）"
                        if other.get("definition"):
                            line += f" — {other['definition']}"
                        if r["type"] == "co_occurs":
                            src = _sources_for_pair(db, e.id, other["id"])
                            line += "".join(f"\n  - [{t}]({u})" for t, u in src)
                        body.append(line)
                body.append("")
            if c["questions"]:
                body += ["## Open Questions", *[f"- {q} [AI]" for q in c["questions"]], ""]
            if c["opposing"]:
                body += ["## Opposing Views", *[f"- {q} [AI]" for q in c["opposing"]], ""]
            if c["metrics"]:
                body += ["## Key Numbers", *[f"- {m['label']}: {m['value']}{m['unit']}" for m in c["metrics"]], ""]
            if c["history"]["series"]:
                body += ["## 変化と矛盾"]
                for g in c["history"]["series"]:
                    for en in g["entries"]:
                        mark = {"change": "変化", "contradiction": "矛盾", "cond_diff": "条件違い", "first": "最初", "same": "同じ"}.get(en["mark"], "")
                        body.append(f"- {en['t'][:10]} {g.get('metric') or g['kind']}: {en['value']}（{mark}{'・前は ' + en['from'] if en.get('from') else ''}）" + (f" [{en['title']}]({en['url']})" if en["url"] else ""))
                body.append("")
            if c["claims"]:
                body += ["## 主張と根拠"]
                for cl in c["claims"]:
                    body.append(f"- {cl['text']}" + (f"（{cl['value']}）" if cl["value"] else "") + ("［AI の取り出し］" if cl["ai_extracted"] else ""))
                    for ev in cl["evidences"]:
                        d = ev["document"]
                        pos = f" p.{ev['page']}" if ev["page"] else ""
                        body.append(f"  - 根拠: {d['title'] or d['kind']}{pos} {d['url']}" + (f" 「{ev['quote']}」" if ev["quote"] else ""))
                body.append("")
            if include_notes and c["note"]["body"]:
                body += ["## 自分のメモ", c["note"]["body"], ""]
            z.writestr(f"{e.type}/{names[e.id]}.md", "\n".join(fm + body))
        # 主題の入口（MOC）: 書き出す物が 2 つ以上入っているまとまりだけ。リンクは書き出した物だけ
        by_comm: dict[int, list[int]] = {}
        for eid, cid in db.execute(select(EntityCommunity.entity_id, EntityCommunity.community_id).where(EntityCommunity.entity_id.in_([e.id for e in entities]))):
            by_comm.setdefault(cid, []).append(eid)
        order = {e.id: n for n, e in enumerate(entities)}
        moc_names: set[str] = set()
        for cid, members in sorted(by_comm.items(), key=lambda kv: -len(kv[1]))[:30]:
            if len(members) < 2:
                continue
            c = db.get(Community, cid)
            if c is None:
                continue
            moc = things.moc_community(db, cid, scopes) or {}
            members.sort(key=lambda i: order[i])
            title = safe_name(f"MOC {c.name}")
            if title.casefold() in moc_names:
                title = f"{title} ({cid})"
            moc_names.add(title.casefold())
            lines = ["---", "type: moc", f"size: {c.size}", f"exported_at: {json.dumps(exported)}", "tags: [qnapnewshub, moc]", "---", "", f"# {c.name} のまとまり", "", "## 主要な Things"]
            lines += [f"- [[{names[i]}]]" for i in members[:30]]
            if moc.get("events"):
                lines += ["", "## 最近の出来事", *[f"- {ev['last_at'][:10]} {ev['title']}" for ev in moc["events"]]]
            qs = [q for q in moc.get("questions", []) if q["entity_id"] in names]
            if qs:
                lines += ["", "## Open Questions", *[f"- [[{names[q['entity_id']]}]]: {q['question']} [AI]" for q in qs]]
            z.writestr(f"_MOC/{title}.md", "\n".join(lines) + "\n")
    return buf.getvalue(), {"files": len(entities), "scopes": scopes}
