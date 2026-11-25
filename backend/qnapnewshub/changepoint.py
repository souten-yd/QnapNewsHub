"""変化点（ARCHITECTURE 12 章）。LLM を使わない統計だけ。2 つの種類:

- **Metric の時系列**（`series_change`）: 点の列で、直前までの中央値から急に外れた点。外れ具合は MAD（中央値からの絶対偏差の中央値）の 3.5 倍以上かつ中央値の 20% 以上（系列が平らで MAD が 0 のときの誤検出を防ぐ）。
  直前の点が 4 つ未満なら言わない（少ない点で変化と言わない）。
- **Things の言及の数**（`mention_surge`）: ana_entity（日ごとの記事の数。既存のホットトピックと同じ集計）で、直近 3 日の 1 日あたりの数が、その前 4 週の 1 日あたりより 3 倍以上、かつ直近 3 日で 3 件以上。
  逆（急な減り）は、前の 4 週の 1 日あたりが 1 件以上あり、直近 3 日がその 1/3 以下のとき。
どちらも「変化があったかもしれない印」で、原因は言わない（根拠の記事は watch の変化の要約に別に出る）。
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

MIN_PREV = 4
MAD_K = 3.5
MIN_REL = 0.2
SURGE_RECENT_DAYS = 3
SURGE_BASE_DAYS = 28
SURGE_RATIO = 3.0
SURGE_MIN_RECENT = 3


def series_change(points: list[tuple[datetime, float]]) -> list[dict]:
    """点（時刻, 値）の列から、直前までの中央値から急に外れた点を返す（新しい順に最大 3）。{at, value, base, pct, direction}。"""
    pts = sorted(points, key=lambda p: p[0])
    out = []
    for i in range(MIN_PREV, len(pts)):
        prev = [v for _, v in pts[max(0, i - 14):i]]
        base = statistics.median(prev)
        mad = statistics.median([abs(v - base) for v in prev])
        diff = pts[i][1] - base
        if abs(diff) < MAD_K * 1.4826 * mad or abs(diff) < MIN_REL * abs(base) or diff == 0:
            continue
        out.append({"at": pts[i][0], "value": pts[i][1], "base": base, "pct": round(diff / base * 100, 1) if base else None, "direction": "up" if diff > 0 else "down"})
    return out[::-1][:3]


def mention_surge(db: Session, entity_id: int, now: datetime | None = None) -> dict | None:
    """Things の言及の数の急な増減（ana_entity の日ごと）。無ければ None。"""
    from qnapnewshub import analytics
    from qnapnewshub.db import AnaEntity

    today = analytics._now_local(now).date()      # noqa: SLF001
    first_recent = today - timedelta(days=SURGE_RECENT_DAYS - 1)
    first_base = first_recent - timedelta(days=SURGE_BASE_DAYS)
    rows = db.execute(select(AnaEntity.t, AnaEntity.n).where(AnaEntity.grain == "d", AnaEntity.entity_id == entity_id, AnaEntity.t >= first_base.isoformat(), AnaEntity.t <= today.isoformat())).all()
    recent = sum(n for t, n in rows if t >= first_recent.isoformat())
    base = sum(n for t, n in rows if t < first_recent.isoformat())
    r_day, b_day = recent / SURGE_RECENT_DAYS, base / SURGE_BASE_DAYS
    if recent >= SURGE_MIN_RECENT and r_day >= SURGE_RATIO * max(b_day, 1 / SURGE_BASE_DAYS):
        return {"direction": "up", "recent": recent, "per_day": round(r_day, 2), "base_per_day": round(b_day, 2), "ratio": round(r_day / b_day, 1) if b_day else None}
    if b_day >= 1 and r_day <= b_day / SURGE_RATIO:
        return {"direction": "down", "recent": recent, "per_day": round(r_day, 2), "base_per_day": round(b_day, 2), "ratio": round(r_day / b_day, 2)}
    return None


def metric_changes(db: Session, ent, since: datetime | None = None) -> list[dict]:
    """物の Metric の系列（subject・metric・cond ごと）の変化点。since より新しい点だけ。"""
    import re

    from qnapnewshub.db import MetricPoint
    from qnapnewshub.extract import normalize_key

    if not ent.name:
        return []
    pat = "%" + re.sub(r"([%_\\])", r"\\\1", ent.name)
    series: dict[tuple, list] = {}
    for p in db.scalars(select(MetricPoint).where(MetricPoint.subject.like(pat, escape="\\")).order_by(MetricPoint.at.desc()).limit(2000)):
        if normalize_key(re.split(r"[/:]", p.subject)[-1]) == ent.key:
            series.setdefault((p.subject, p.metric, p.cond), []).append(p)
    out = []
    for (subject, metric, cond), pts in series.items():
        for ch in series_change([(p.at, p.value) for p in pts]):
            at = ch["at"] if ch["at"].tzinfo else ch["at"].replace(tzinfo=timezone.utc)
            if since is not None and at <= (since if since.tzinfo else since.replace(tzinfo=timezone.utc)):
                continue
            out.append({"kind": "metric", "subject": subject, "metric": metric, "cond": cond, **{k: ch[k] for k in ("value", "base", "pct", "direction")}, "at": at.strftime("%Y-%m-%d")})
    return out[:4]


def for_entity(db: Session, ent, since: datetime | None = None, now: datetime | None = None) -> list[dict]:
    """カード・変化の要約に出す変化点（Metric の変化 ＋ 言及の急な増減）。"""
    out = metric_changes(db, ent, since)
    s = mention_surge(db, ent.id, now)
    if s:
        out.append({"kind": "mentions", **s})
    return out


def line(c: dict) -> str:
    """1 行の文（LLM・画面用）。"""
    if c["kind"] == "metric":
        arrow = "上がった" if c["direction"] == "up" else "下がった"
        pct = f"（{c['pct']:+g}%）" if c.get("pct") is not None else ""
        return f"{c['metric']} が直前の水準 {c['base']:g} から {c['value']:g} へ{arrow}{pct} {c['at']}"
    if c["direction"] == "up":
        return f"言及が急に増えた: 直近 {SURGE_RECENT_DAYS} 日で {c['recent']} 件（1 日 {c['per_day']:g} 件。前の 4 週は 1 日 {c['base_per_day']:g} 件）"
    return f"言及が急に減った: 直近 {SURGE_RECENT_DAYS} 日で {c['recent']} 件（1 日 {c['per_day']:g} 件。前の 4 週は 1 日 {c['base_per_day']:g} 件）"
