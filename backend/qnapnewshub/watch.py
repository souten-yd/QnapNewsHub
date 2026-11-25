"""ウォッチリストと「前回から何が変わったか」（ARCHITECTURE 12 章）。**LLM は使わない**（機械的に集める。LLM の解説は用途 trend の枠で後から足せる形にしてあるが、この版では呼ばない）。

- ウォッチリスト（`watch_items`）は監視する Things。**既定は空**で、画面（/watch）か API で足す。seen_at は前回見た時刻で、変化の要約の起点（`since` を渡せば上書き）。
  「前に見た」を進めるのは利用者の操作（`mark_seen`）だけ。変化の要約を読むだけでは進まない（LLM が何度引いても同じ範囲を返す）。
- 時刻は「世の中で新しくなった時刻」（主張は stated_at＝記事の公開日・無ければ取り出した時刻、関係は first_at、文書は公開日）。古い記事を後から取り出しても変化にしない。
- 変化の要約（`changes`）は、監視の Things（または `thing=` の指定）ごとに、起点より新しい: 主張（新しく知った。数値・条件つき）・数値の変化（Metric の前の値との差）・新しい関係・版の更新（release・model・repo の文書）・
  イベント（その Things が出るまとまり）・変化点（Metric の急な変化・言及の急な増減）。根拠には出どころの印（things.doc_label）を付ける。
- 区分: 読む範囲は scopes（public 既定）。主張・関係・文書は区分で絞り、機密を public の読み手へ出さない。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from qnapnewshub import bench, changepoint, kg, things
from qnapnewshub.db import Claim, Document, Entity, Evidence, Event, MetricPoint, Relationship, WatchItem
from qnapnewshub.extract import normalize_key

PER_KIND = 6
WATCH_MAX = 50
DEFAULT_DAYS = 7
VERSION_KINDS = ("release", "model", "repo")
_UTC = timezone.utc


def _aware(d: datetime | None) -> datetime | None:
    return None if d is None else (d if d.tzinfo else d.replace(tzinfo=_UTC))


def _now() -> datetime:
    return datetime.now(_UTC)


def parse_since(raw: str, default: datetime | None, now: datetime) -> datetime | None:
    """`since` の指定: ISO の日時・日付（YYYY-MM-DD）・`7d`／`12h`（今から遡る）。空なら default。読めなければ ValueError。"""
    raw = (raw or "").strip()
    if not raw:
        return default
    m = re.fullmatch(r"(\d{1,4})([dh])", raw)
    if m:
        return now - (timedelta(days=int(m.group(1))) if m.group(2) == "d" else timedelta(hours=int(m.group(1))))
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError("since は日時（2026-11-20T00:00:00Z）・日付・7d・12h のどれかです") from e
    return _aware(d)


# ---- ウォッチリスト ----

def listing(db: Session, scopes: list[str]) -> list[dict]:
    rows = db.execute(select(WatchItem, Entity).join(Entity, Entity.id == WatchItem.entity_id).where(Entity.sensitivity.in_(scopes)).order_by(WatchItem.id)).all()
    return [{"id": w.id, "entity": {"id": e.id, "name": e.name, "type": e.type}, "created_at": kg.iso(w.created_at), "seen_at": kg.iso(w.seen_at)} for w, e in rows]


def add(db: Session, entity_id: int, scopes: list[str]) -> dict | None:
    """監視に足す（同じ物は 1 つ。上限 WATCH_MAX）。物が無い・読めないなら None。コミットは呼ぶ側。"""
    ent = db.get(Entity, entity_id)
    if ent is None or ent.sensitivity not in scopes:
        return None
    w = db.scalar(select(WatchItem).where(WatchItem.entity_id == entity_id))
    if w is None:
        if db.scalar(select(func.count(WatchItem.id))) >= WATCH_MAX:
            raise ValueError(f"監視は {WATCH_MAX} 件までです")
        w = WatchItem(entity_id=entity_id)
        db.add(w)
        db.flush()
    return {"id": w.id, "entity": {"id": ent.id, "name": ent.name, "type": ent.type}, "created_at": kg.iso(w.created_at), "seen_at": kg.iso(w.seen_at)}


def remove(db: Session, entity_id: int) -> bool:
    w = db.scalar(select(WatchItem).where(WatchItem.entity_id == entity_id))
    if w is None:
        return False
    db.delete(w)
    return True


def mark_seen(db: Session, entity_ids: list[int] | None, now: datetime | None = None) -> int:
    """前回見た時刻を now へ進める（entity_ids が None なら全部）。進めた数。"""
    when = now or _now()
    q = select(WatchItem)
    if entity_ids is not None:
        q = q.where(WatchItem.entity_id.in_(entity_ids))
    n = 0
    for w in db.scalars(q):
        w.seen_at = when
        n += 1
    return n


# ---- 変化の集め ----

def _doc_label_for_claims(db: Session, claim_ids: list[int], scopes: list[str]) -> dict[int, dict]:
    if not claim_ids:
        return {}
    out: dict[int, dict] = {}
    rows = db.execute(select(Evidence.claim_id, Evidence.grounded, Evidence.tier, Document.id, Document.source, Document.published_at, Document.url)
                      .join(Document, Document.id == Evidence.document_id).where(Evidence.claim_id.in_(claim_ids), Document.sensitivity.in_(scopes)).order_by(Evidence.confidence.desc(), Evidence.id))
    for cid, grounded, tier, did, source, pub, url in rows:
        if cid not in out:
            out[cid] = {"label": things.doc_label(source, kg.iso(pub), did), "url": url or "", "grounded": bool(grounded) or tier == "exact", "doc": did}
    return out


def _claims(db: Session, ent: Entity, cutoff: datetime, scopes: list[str]) -> list[dict]:
    rows = list(db.scalars(select(Claim).where(Claim.entity_id == ent.id, func.coalesce(Claim.stated_at, Claim.created_at) > cutoff, Claim.sensitivity.in_(scopes)).order_by(func.coalesce(Claim.stated_at, Claim.created_at).desc(), Claim.id.desc()).limit(PER_KIND * 3)))
    ev = _doc_label_for_claims(db, [c.id for c in rows], scopes)
    out = []
    for c in sorted(rows, key=lambda c: (not c.value, 0)):      # 数値のある主張が先
        row = {"id": c.id, "kind": c.kind, "text": c.text[:140], "at": kg.iso(c.stated_at or c.created_at)[:10]}
        if c.value:
            row["value"] = c.value
            cond = bench.cond_of(c.cond, c.text)
            if cond:
                row["cond"] = cond
        e = ev.get(c.id)
        if e:
            row["src"] = e["label"]
            row["doc"] = e["doc"]
            if e["grounded"]:
                row["grounded"] = True
        out.append(row)
    return out[:PER_KIND]


def _metrics(db: Session, ent: Entity, cutoff: datetime) -> list[dict]:
    if not ent.name:
        return []
    pat = "%" + re.sub(r"([%_\\])", r"\\\1", ent.name)
    naive = cutoff.astimezone(_UTC).replace(tzinfo=None)
    groups: dict[tuple, list[MetricPoint]] = {}
    for p in db.scalars(select(MetricPoint).where(MetricPoint.subject.like(pat, escape="\\")).order_by(MetricPoint.at.desc()).limit(1500)):
        if normalize_key(re.split(r"[/:]", p.subject)[-1]) == ent.key:
            groups.setdefault((p.subject, p.metric, p.cond), []).append(p)
    out = []
    for (subject, metric, cond), pts in groups.items():
        latest = pts[0]
        if _aware(latest.at).astimezone(_UTC).replace(tzinfo=None) <= naive:
            continue      # 起点より新しい点が無い
        before = next((p for p in pts if _aware(p.at).astimezone(_UTC).replace(tzinfo=None) <= naive), None)
        row = {"subject": subject, "metric": metric, "value": latest.value, "unit": latest.unit, "at": latest.at.strftime("%Y-%m-%d"), **({"cond": cond} if cond else {})}
        if before is not None:
            row.update(previous=before.value, delta=round(latest.value - before.value, 6), pct=round((latest.value - before.value) / before.value * 100, 1) if before.value else None)
        out.append(row)
    out.sort(key=lambda r: -abs(r.get("pct") or 0))
    return out[:PER_KIND]


def _relations(db: Session, ent: Entity, cutoff: datetime, scopes: list[str]) -> list[dict]:
    rows = db.execute(select(Relationship).where(or_(Relationship.subject_id == ent.id, Relationship.object_id == ent.id), Relationship.type != "co_occurs", Relationship.first_at > cutoff,
                                                  Relationship.sensitivity.in_(scopes)).order_by(Relationship.first_at.desc()).limit(PER_KIND)).scalars().all()
    names = {e.id: e.name for e in db.scalars(select(Entity).where(Entity.id.in_({x for r in rows for x in (r.subject_id, r.object_id)})))} if rows else {}
    return [{"id": r.id, "type": r.type, "subject": names.get(r.subject_id, ""), "object": names.get(r.object_id, ""), "conf": round(r.confidence, 2), "status": r.status, "at": kg.iso(r.first_at)[:10]} for r in rows]


def _versions(db: Session, ent: Entity, cutoff: datetime, scopes: list[str]) -> list[dict]:
    if len(ent.name) < 3:
        return []
    like = "%" + re.sub(r"([%_\\])", r"\\\1", ent.name) + "%"
    rows = db.scalars(select(Document).where(Document.kind.in_(VERSION_KINDS), Document.title.like(like, escape="\\"), Document.sensitivity.in_(scopes),
                                             or_(Document.published_at > cutoff, Document.created_at > cutoff)).order_by(Document.published_at.desc()).limit(PER_KIND * 3))
    out = []
    for d in rows:
        if normalize_key(ent.name) not in normalize_key(d.title):
            continue
        out.append({"doc": d.id, "kind": d.kind, "title": d.title[:100], "version": d.version, "at": kg.iso(d.published_at or d.created_at)[:10], "src": things.doc_label(d.source, kg.iso(d.published_at), d.id), "url": d.url})
    return out[:PER_KIND]


def _events(db: Session, ent: Entity, cutoff: datetime) -> list[dict]:
    out = []
    for e in db.scalars(select(Event).where(Event.last_at > cutoff).order_by(Event.last_at.desc()).limit(400)):
        try:
            ids = {int(x.get("id")) for x in json.loads(e.entities or "[]") if isinstance(x, dict)}
        except (ValueError, TypeError):
            continue
        if ent.id in ids:
            out.append({"id": e.id, "title": e.title[:120], "n_articles": e.n_articles, "n_sources": e.n_sources, "has_primary": bool(e.has_primary), "last_at": kg.iso(e.last_at)[:10]})
        if len(out) >= PER_KIND:
            break
    return out


def thing_changes(db: Session, ent: Entity, cutoff: datetime, scopes: list[str], now: datetime) -> dict:
    claims, metrics, rels, versions, events = (_claims(db, ent, cutoff, scopes), _metrics(db, ent, cutoff), _relations(db, ent, cutoff, scopes), _versions(db, ent, cutoff, scopes), _events(db, ent, cutoff))
    points = [{**c, "line": changepoint.line(c)} for c in changepoint.for_entity(db, ent, cutoff, now)]
    return {"thing": {"id": ent.id, "name": ent.name, "type": ent.type}, "since": kg.iso(cutoff), "claims": claims, "metrics": metrics, "relations": rels, "versions": versions, "events": events,
            "change_points": points, "quiet": not (claims or metrics or rels or versions or events or points)}


def changes(db: Session, scopes: list[str], *, since: datetime | None = None, entity_ids: list[int] | None = None, now: datetime | None = None) -> dict:
    """変化の要約。entity_ids があればその物（監視でなくてよい。起点は since か既定 7 日前）、無ければ監視の物（起点は since か各自の seen_at）。"""
    now = now or _now()
    targets: list[tuple[Entity, datetime]] = []
    if entity_ids is not None:
        for i in entity_ids:
            ent = db.get(Entity, i)
            if ent is not None and ent.sensitivity in scopes:
                targets.append((ent, since or now - timedelta(days=DEFAULT_DAYS)))
    else:
        for w, ent in db.execute(select(WatchItem, Entity).join(Entity, Entity.id == WatchItem.entity_id).where(Entity.sensitivity.in_(scopes)).order_by(WatchItem.id)):
            targets.append((ent, since or _aware(w.seen_at) or _aware(w.created_at)))
    items = [thing_changes(db, ent, cutoff, scopes, now) for ent, cutoff in targets]
    return {"generated_at": kg.iso(now), "watched": entity_ids is None, "items": items, "changed": sum(1 for i in items if not i["quiet"]), "llm": False}


def text(c: dict) -> str:
    """LLM に渡す短い Markdown（空の節は出さない・変化の無い物は 1 行）。"""
    lines: list[str] = []
    for it in c["items"]:
        t = it["thing"]
        if it["quiet"]:
            lines.append(f"# {t['name']} (#{t['id']}): {it['since'][:10]} 以降の変化なし")
            continue
        lines.append(f"# {t['name']} (#{t['id']}): {it['since'][:10]} 以降の変化")
        for p in it["change_points"]:
            lines.append(f"- 変化点: {p['line']}")
        if it["claims"]:
            lines.append("## 新しい主張")
            for x in it["claims"]:
                cond = " / ".join(f"{bench.COND_LABEL[k]} {v}" for k, v in x.get("cond", {}).items())
                lines.append(f"- {'◎ ' if x.get('grounded') else ''}{x['text']}" + (f" = {x['value']}" if x.get("value") else "") + (f"（{cond}）" if cond else "") + (f" <{x['src']}>" if x.get("src") else ""))
        if it["metrics"]:
            lines.append("## 数値の変化")
            for m in it["metrics"]:
                prev = f"（前 {m['previous']:g}・{m['pct']:+g}%）" if m.get("pct") is not None else (f"（前 {m['previous']:g}）" if "previous" in m else "（新しい系列）")
                lines.append(f"- {m['metric']}{'（' + m['cond'] + '）' if m.get('cond') else ''}: {m['value']:g}{m['unit']}{prev} {m['at']}")
        if it["relations"]:
            lines.append("## 新しい関係")
            lines += [f"- {r['subject']} -{r['type']}-> {r['object']} ({r['conf']}・{r['status']})" for r in it["relations"]]
        if it["versions"]:
            lines.append("## 版・リリース")
            lines += [f"- {v['title']}" + (f" {v['version']}" if v["version"] else "") + f" {v['at']} <{v['src']}>" for v in it["versions"]]
        if it["events"]:
            lines.append("## イベント")
            lines += [f"- {e['title']}（記事 {e['n_articles']}・出どころ {e['n_sources']}{'・一次情報あり' if e['has_primary'] else ''}）#event{e['id']}" for e in it["events"]]
    if not lines:
        lines.append("監視の物がありません。")
    return "\n".join(lines)
