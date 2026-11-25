"""知識の地図の「重力」の配置（ForceAtlas2）と点の大きさ（重要度）。夜にサーバーで計算して保存し、画面は保存した座標で出す。ARCHITECTURE 6 章。

- **使い方の数え**: 検索で当たった回数・カードを開いた回数は、メモリで数えて Write Coordinator のまとめ書きで保存する（entity_usage_daily＝日ごとの集計の表）。
  `note_search` / `note_card` はメモリに積むだけ（同じ点を 60 秒以内に数え直さない＝打鍵ごとの検索で膨らまない）。
- **重要度**（0〜1）: 直近 window_days 日の「検索で当たった回数」「カードを開いた回数」「記事での出現」と、関係の中心性（PageRank）を、それぞれ log(1+x) で縮めて最大で割り、
  設定（map.weights）の重みで混ぜる。最後に最大を 1 に合わせる。点の大きさ＝重要度（画面が決める）。
- **配置**: ForceAtlas2（Jacomy ら 2014）を依存なしの Python で書く。反発は点の重さ（（次数＋1）×（0.5＋重要度））の積に比例、引力は線の重み（根拠の数×確かさ）の LinLog、
  重力で中心へ（全点同じ強さ。重さに比例させると重い点が中心に固まって離れない）、outbound attraction distribution（点ごとの引力を（重さ／平均）^0.5 で割る＝大きな点どうしは離れ、小さな点は重い点の周りに集まる）。反発は Barnes-Hut（四分木の葉ごとに遠い塊を 1 点に畳む）。
- **丸が重ならない**: 点の半径は重要度から（radius。画面の radiusOf と同じ式で、座標と同じ単位＝拡大すると丸も大きくなる）。ForceAtlas2 のあと、重なっている組を離す（大きな点ほど動かさない）。
- **形を毎回変えない**: 前回の座標から始めて（新しい点は、つながる最も重い点の近くから）、短く整えるだけ。座標の表（map_layout）と版（news_meta の map_layout_ver）に保存する。
- 外へ通信しない。LLM を呼ばない。機密（confidential）の物は計算に入れない（public だけ）。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select

from qnapnewshub import db as news_db
from qnapnewshub import settings, writer
from qnapnewshub.db import ArticleEntity, Entity, EntityUsage, MapLayout, NewsItem, NewsMeta, Relationship

logger = logging.getLogger("qnapnewshub.mapgrav")
_UTC = timezone.utc

LAYOUT_MAX = 5000            # 配置する点の上限（次数＋最近の線の多い順）
DEDUP_SECONDS = 60           # 同じ（種類・点）を数え直さない間隔
USAGE_KEEP_DAYS = 120        # 日ごとの集計を残す日数
COLD_ITER, WARM_ITER, WARM_SPEED = 300, 60, 0.1
THETA = 1.2                  # Barnes-Hut の「遠い」の判定（葉の大きさ / 距離）
LEAF = 6
GRAVITY, SCALING, OUTBOUND, GMASS = 1.0, 0.3, 0.5, 0.0
DEFAULT_MAP = {"window_days": 30, "weights": {"search": 0.25, "card": 0.15, "recent": 0.35, "centrality": 0.25}}


def map_config() -> dict:
    try:
        cfg = settings.load().get("map") or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    w = {**DEFAULT_MAP["weights"], **(cfg.get("weights") or {})}
    return {"window_days": int(cfg.get("window_days", DEFAULT_MAP["window_days"])), "weights": {k: float(w[k]) for k in DEFAULT_MAP["weights"]}}


# ---- 数える（メモリ → まとめ書き） ----

_seen: dict[tuple[str, int], float] = {}
_seen_lock = threading.Lock()


def _today() -> str:
    return datetime.now(_UTC).strftime("%Y-%m-%d")


def _note(kind: str, ids: list[int], *, now: float | None = None) -> int:
    t = time.monotonic() if now is None else now
    fresh: list[int] = []
    with _seen_lock:
        if len(_seen) > 20000:
            for k in [k for k, v in _seen.items() if t - v > DEDUP_SECONDS]:
                del _seen[k]
        for i in dict.fromkeys(ids):
            if t - _seen.get((kind, i), -1e9) >= DEDUP_SECONDS:
                _seen[(kind, i)] = t
                fresh.append(int(i))
    if fresh:
        writer.get().stage_usage(_today(), kind, fresh)
    return len(fresh)


def note_search(ids: list[int], *, now: float | None = None) -> int:
    """検索で当たった点（上位の数件）を数える。"""
    return _note("search", ids[:10], now=now)


def note_card(entity_id: int, *, now: float | None = None) -> int:
    """カードを開いた点を数える。"""
    return _note("card", [entity_id], now=now)


def apply_usage(db, usage: dict[tuple[str, str, int], int]) -> None:
    """まとめ書きの中で、日ごとの集計の表へ足す（同じ day・kind・点の行へ加算）。"""
    for (day, kind, eid), n in usage.items():
        row = db.get(EntityUsage, (day, eid, kind))
        if row is None:
            if db.get(Entity, eid) is None:
                continue
            db.add(EntityUsage(day=day, entity_id=eid, kind=kind, n=int(n)))
        else:
            row.n = int(row.n) + int(n)
    db.flush()
    db.execute(delete(EntityUsage).where(EntityUsage.day < (datetime.now(_UTC) - timedelta(days=USAGE_KEEP_DAYS)).strftime("%Y-%m-%d")))


# ---- 重要度 ----

def _norm(vals: dict[int, float]) -> dict[int, float]:
    top = max(vals.values(), default=0.0)
    if top <= 0:
        return {}
    d = math.log1p(top)
    return {i: math.log1p(v) / d for i, v in vals.items() if v > 0}


def pagerank(adj: dict[int, dict[int, float]], damping: float = 0.85, iters: int = 30) -> dict[int, float]:
    n = len(adj)
    if n == 0:
        return {}
    rank = {i: 1.0 / n for i in adj}
    out = {i: sum(d.values()) for i, d in adj.items()}
    for _ in range(iters):
        nxt = {i: (1 - damping) / n for i in adj}
        dangling = sum(rank[i] for i in adj if out[i] <= 0)
        for i, nbrs in adj.items():
            if out[i] > 0:
                share = damping * rank[i] / out[i]
                for j, w in nbrs.items():
                    nxt[j] += share * w
        spread = damping * dangling / n
        rank = {i: v + spread for i, v in nxt.items()}
    return rank


def importance(ids: list[int], adj: dict[int, dict[int, float]], search: dict[int, float], card: dict[int, float], recent: dict[int, float], cfg: dict | None = None) -> dict[int, float]:
    """点ごとの重要度 0〜1（最大が 1）。各成分は log(1+x)/log(1+最大) に縮め、重みで混ぜる。成分が全部 0 の点は 0。"""
    w = (cfg or map_config())["weights"]
    comps = {"search": _norm(search), "card": _norm(card), "recent": _norm(recent), "centrality": _norm(pagerank(adj))}
    total = sum(max(0.0, v) for v in w.values()) or 1.0
    raw = {i: sum(max(0.0, w[k]) * comps[k].get(i, 0.0) for k in comps) / total for i in ids}
    top = max(raw.values(), default=0.0)
    return {i: (v / top if top > 0 else 0.0) for i, v in raw.items()}


# ---- ForceAtlas2（Barnes-Hut） ----

def _golden_start(ids: list[int], mass: dict[int, float]) -> dict[int, tuple[float, float]]:
    """前回の座標が無いときの始め: 重い点ほど中心に近いひまわりの並び（決まった順なので毎回同じ）。"""
    order = sorted(ids, key=lambda i: (-mass[i], i))
    ga = 2.399963229728653
    return {i: (math.sqrt(k + 0.5) * 18 * math.cos(k * ga), math.sqrt(k + 0.5) * 18 * math.sin(k * ga)) for k, i in enumerate(order)}


def _jitter(i: int) -> tuple[float, float]:
    a = ((i * 2654435761) % 3600) / 3600 * 2 * math.pi
    r = 25 + (i * 40503) % 20
    return r * math.cos(a), r * math.sin(a)


def forceatlas2(ids: list[int], adj: dict[int, dict[int, float]], mass: dict[int, float], init: dict[int, tuple[float, float]] | None = None, *, iterations: int | None = None,
                gravity: float = GRAVITY, scaling: float = SCALING, theta: float = THETA, start_speed: float = 1.0, outbound: float = OUTBOUND, gmass: float = GMASS, rad: dict[int, float] | None = None) -> dict[int, tuple[float, float]]:
    """ForceAtlas2。{点: (x, y)}。init が空でなければ（前回の座標）その続きから短く整える。同じ入力・同じ結果（乱数なし）。
    init に無い点は、つながる最も重い点（init にある）の近くから始める。"""
    n = len(ids)
    if n == 0:
        return {}
    idx = {i: k for k, i in enumerate(ids)}
    warm = bool(init) and any(i in init for i in ids)
    iters = iterations if iterations is not None else (WARM_ITER if warm else COLD_ITER)
    start = _golden_start(ids, mass)
    px, py = [0.0] * n, [0.0] * n
    for i in sorted(ids, key=lambda i: (-mass[i], i)):
        k = idx[i]
        if init and i in init:
            px[k], py[k] = init[i]
        elif warm:
            anchor = None
            for j, w in sorted(adj.get(i, {}).items(), key=lambda t: (-mass.get(t[0], 0.0), t[0])):
                if init and j in init:
                    anchor = j
                    break
            if anchor is not None:
                jx, jy = _jitter(i)
                px[k], py[k] = init[anchor][0] + jx, init[anchor][1] + jy
            else:
                px[k], py[k] = start[i]
        else:
            px[k], py[k] = start[i]
    m = [mass[i] for i in ids]
    r = [rad.get(i, 0.0) for i in ids] if rad else None      # 丸の半径（近い組の反発は、丸の縁の間の距離で測る＝丸が重ならない向きに働く）
    nbr: list[list[tuple[int, float]]] = [[(idx[j], w) for j, w in adj.get(i, {}).items() if j in idx and j != i] for i in ids]
    avg_mass = sum(m) / n
    avg_mass = avg_mass or 1.0
    fx0, fy0 = [0.0] * n, [0.0] * n
    speed, speed_eff = (min(start_speed, WARM_SPEED) if warm else start_speed), 1.0
    for _ in range(iters):
        fx, fy = [0.0] * n, [0.0] * n
        _repulse(px, py, m, fx, fy, scaling, theta, r)
        for a in range(n):
            xa, ya, ma = px[a], py[a], m[a]
            sx = sy = 0.0
            comp = (ma / avg_mass) ** outbound      # 重い点ほど 1 本あたりの引力を小さく（大きな点どうしが離れ、小さな点が周りに残る）
            for b, w in nbr[a]:
                dx, dy = px[b] - xa, py[b] - ya
                d = math.hypot(dx, dy)
                if d > 1e-9:
                    f = w * math.log1p(d) / d / comp      # LinLog + outbound attraction distribution
                    sx += dx * f
                    sy += dy * f
            fx[a] += sx
            fy[a] += sy
            d0 = math.hypot(xa, ya)
            if d0 > 1e-9:
                g = gravity * ma ** gmass / d0      # 中心へ（重さの gmass 乗。0 なら全点同じ強さ）
                fx[a] -= xa * g
                fy[a] -= ya * g
        swing = traction = 0.0
        sw = [0.0] * n
        for a in range(n):
            sw[a] = m[a] * math.hypot(fx[a] - fx0[a], fy[a] - fy0[a])
            swing += sw[a]
            traction += m[a] * math.hypot(fx[a] + fx0[a], fy[a] + fy0[a]) / 2
        jt = max(math.sqrt(0.05 * math.sqrt(n)), min(10.0, 0.05 * math.sqrt(n) * traction / (n * n)))      # FA2 の揺れの許容
        if swing > jt * traction and speed_eff > 0.05:
            speed_eff *= 0.5
        target = float("inf") if swing == 0 else jt * speed_eff * traction / swing
        speed = speed + min(target - speed, 0.5 * speed)
        for a in range(n):
            factor = speed / (1.0 + math.sqrt(speed * sw[a]))
            dxm, dym = fx[a] * factor, fy[a] * factor
            dl = math.hypot(dxm, dym)
            if dl > 200.0:
                dxm, dym = dxm * 200.0 / dl, dym * 200.0 / dl
            px[a] += dxm
            py[a] += dym
        fx0, fy0 = fx, fy
    cx = sum(px[a] * m[a] for a in range(n)) / sum(m)
    cy = sum(py[a] * m[a] for a in range(n)) / sum(m)
    return {ids[a]: (round(px[a] - cx, 2), round(py[a] - cy, 2)) for a in range(n)}


def _repulse(px: list[float], py: list[float], m: list[float], fx: list[float], fy: list[float], kr: float, theta: float, rad: list[float] | None = None) -> None:
    """反発（kr × m_i × m_j / d）。四分木の葉ごとに、遠い塊は 1 点（重さ・重心）に畳み、近い葉は点どうしを直接。"""
    n = len(px)
    minx, maxx, miny, maxy = min(px), max(px), min(py), max(py)
    size = max(maxx - minx, maxy - miny, 1.0) * 1.0001
    cells: list[list] = []      # [mass, cx, cy, size, children(list|None), members(list|None)]

    def build(members: list[int], x0: float, y0: float, s: float, depth: int) -> int:
        mm = sum(m[a] for a in members)
        cxm = sum(px[a] * m[a] for a in members) / mm
        cym = sum(py[a] * m[a] for a in members) / mm
        cid = len(cells)
        if len(members) <= LEAF or depth >= 24:
            cells.append([mm, cxm, cym, s, None, members])
            return cid
        cells.append([mm, cxm, cym, s, [], None])
        h = s / 2
        quad: list[list[int]] = [[], [], [], []]
        for a in members:
            quad[(1 if px[a] >= x0 + h else 0) + (2 if py[a] >= y0 + h else 0)].append(a)
        kids = cells[cid][4]
        for q in range(4):
            if quad[q]:
                kids.append(build(quad[q], x0 + (h if q & 1 else 0.0), y0 + (h if q & 2 else 0.0), h, depth + 1))
        return cid

    build(list(range(n)), minx, miny, size, 0)
    leaves = [c for c in range(len(cells)) if cells[c][5] is not None]
    for lf in leaves:
        lm, lx, ly, _s, _k, members = cells[lf]
        sx = sy = 0.0
        near: list[list[int]] = []
        stack = [0]
        while stack:
            c = stack.pop()
            cm, cx, cy, cs, kids, mem = cells[c]
            if c == lf:
                near.append(mem)
                continue
            dx, dy = lx - cx, ly - cy
            d2 = dx * dx + dy * dy
            if kids is None:
                near.append(mem)
            elif cs * cs < theta * theta * d2:
                inv = cm / d2 if d2 > 1e-12 else 0.0
                sx += dx * inv
                sy += dy * inv
            else:
                stack.extend(kids)
        for a in members:
            xa, ya, ma = px[a], py[a], m[a]
            ax = sx * ma * kr
            ay = sy * ma * kr
            for grp in near:
                for b in grp:
                    if b == a:
                        continue
                    dx, dy = xa - px[b], ya - py[b]
                    d2 = dx * dx + dy * dy
                    if d2 < 1e-6:
                        dx, dy, d2 = 0.01 * ((a - b) % 7 - 3 or 1), 0.01, 0.0001
                    if rad is not None:      # 縁の間の距離（重なっていれば距離に依らない強い反発）
                        d = math.sqrt(d2)
                        gap = d - rad[a] - rad[b]
                        k = kr * ma * m[b] / (gap * d) if gap > 1.0 else kr * ma * m[b] * 10.0 / d
                    else:
                        k = kr * ma * m[b] / d2
                    ax += dx * k
                    ay += dy * k
            fx[a] += ax
            fy[a] += ay


# ---- 丸が重ならない（半径は重要度から。画面と同じ式） ----

R_MIN, R_SPAN, R_POW = 5.0, 55.0, 1.6      # 画面（mapLayout.ts の radiusOf）と同じ式。座標と同じ単位（ワールド）で、拡大すると丸も一緒に大きくなる
OVERLAP_GAP = 2.0


def radius(importance: float) -> float:
    """点の半径（座標と同じ単位）。重要度の 1.6 乗で、少数の大きな点と多数の小さな点にする。"""
    return R_MIN + R_SPAN * max(0.0, min(1.0, importance)) ** R_POW


def remove_overlap(ids: list[int], pos: dict[int, tuple[float, float]], rad: dict[int, float], passes: int = 40, gap: float = OVERLAP_GAP) -> dict[int, tuple[float, float]]:
    """丸どうしが重ならないように、重なっている組を離す（面積の大きい点ほど動かさない）。格子で近い組だけ見る（大きな点は別に全体と見る）。決まった順で乱数なし。"""
    if not ids:
        return {}
    big_cut = 20.0
    cell = 2 * big_cut + gap
    x = {i: pos[i][0] for i in ids}
    y = {i: pos[i][1] for i in ids}
    bigs = [i for i in ids if rad[i] > big_cut]
    small = [i for i in ids if rad[i] <= big_cut]

    def push(a: int, b: int) -> int:
        dx, dy = x[a] - x[b], y[a] - y[b]
        d = math.hypot(dx, dy)
        need = rad[a] + rad[b] + gap
        if d >= need:
            return 0
        if d < 1e-6:      # 同じ場所なら、決まった向きへ
            ang = ((a * 2654435761 + b) % 3600) / 3600 * 2 * math.pi
            dx, dy, d = math.cos(ang), math.sin(ang), 1.0
        wa, wb = rad[b] ** 2, rad[a] ** 2
        move = (need - d) * 1.02
        x[a] += dx / d * move * wa / (wa + wb)
        y[a] += dy / d * move * wa / (wa + wb)
        x[b] -= dx / d * move * wb / (wa + wb)
        y[b] -= dy / d * move * wb / (wa + wb)
        return 1

    for _ in range(passes):
        moved = 0
        grid: dict[tuple[int, int], list[int]] = {}
        for i in small:
            grid.setdefault((int(x[i] // cell), int(y[i] // cell)), []).append(i)
        for i in small:      # 小さな点どうし: 隣の格子まで
            cx, cy = int(x[i] // cell), int(y[i] // cell)
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for j in grid.get((gx, gy), ()):
                        if j > i:
                            moved += push(i, j)
        reach = {b: int(math.ceil((rad[b] + big_cut + gap) / cell)) for b in bigs}
        for b in bigs:      # 大きな点: 近くの小さな点と、ほかの大きな点
            cx, cy, w = int(x[b] // cell), int(y[b] // cell), reach[b]
            for gx in range(cx - w, cx + w + 1):
                for gy in range(cy - w, cy + w + 1):
                    for j in grid.get((gx, gy), ()):
                        moved += push(b, j)
            for o in bigs:
                if o > b:
                    moved += push(b, o)
        if not moved:
            break
    return {i: (round(x[i], 2), round(y[i], 2)) for i in ids}


# ---- 夜の計算 ----

def _read_inputs(now: datetime) -> dict:
    cfg = map_config()
    since_usage = (now - timedelta(days=cfg["window_days"])).strftime("%Y-%m-%d")
    since_art = now - timedelta(days=cfg["window_days"])
    with news_db.session() as db:
        public = {i for (i,) in db.execute(select(Entity.id).where(Entity.sensitivity == "public"))}
        rows = db.execute(select(Relationship.subject_id, Relationship.object_id, Relationship.confidence, Relationship.evidence_count, Relationship.type, Relationship.last_at)
                          .where(Relationship.sensitivity == "public")).all()
        score: dict[int, float] = {}
        edges: list[tuple[int, int, float]] = []
        recent_cut = now - timedelta(days=cfg["window_days"])
        for a, b, c, ev, t, last in rows:
            if a == b or a not in public or b not in public:
                continue
            w = float(c or 0.1) * (1.0 + math.log1p(int(ev or 0))) * (0.6 if t == "co_occurs" else 1.0)
            edges.append((a, b, w))
            rec = 2 if (last is not None and (last if last.tzinfo else last.replace(tzinfo=_UTC)) >= recent_cut) else 0
            for x in (a, b):
                score[x] = score.get(x, 0) + 1 + rec
        ids = [i for i, _ in sorted(score.items(), key=lambda t: (-t[1], t[0]))[:LAYOUT_MAX]]
        keep = set(ids)
        usage: dict[str, dict[int, float]] = {"search": {}, "card": {}}
        for eid, kind, n in db.execute(select(EntityUsage.entity_id, EntityUsage.kind, func.sum(EntityUsage.n)).where(EntityUsage.day >= since_usage).group_by(EntityUsage.entity_id, EntityUsage.kind)):
            if eid in keep and kind in usage:
                usage[kind][eid] = float(n)
        recent: dict[int, float] = {}
        for eid, n in db.execute(select(ArticleEntity.entity_id, func.count()).join(NewsItem, NewsItem.id == ArticleEntity.article_id)
                                 .where(NewsItem.published_at >= since_art).group_by(ArticleEntity.entity_id)):
            if eid in keep:
                recent[eid] = float(n)
        old = {int(e): (float(x), float(y)) for e, x, y in db.execute(select(MapLayout.entity_id, MapLayout.x, MapLayout.y))}
        ver = db.get(NewsMeta, "map_layout_ver")
    adj: dict[int, dict[int, float]] = {i: {} for i in ids}
    for a, b, w in edges:
        if a in keep and b in keep:
            adj[a][b] = adj[a].get(b, 0.0) + w
            adj[b][a] = adj[b].get(a, 0.0) + w
    return {"ids": ids, "adj": adj, "search": usage["search"], "card": usage["card"], "recent": recent, "old": old, "ver": int(ver.value) if ver and ver.value.isdigit() else 0, "cfg": cfg}


def compute(inputs: dict, *, iterations: int | None = None) -> dict:
    ids, adj = inputs["ids"], inputs["adj"]
    imp = importance(ids, adj, inputs["search"], inputs["card"], inputs["recent"], inputs["cfg"])
    mass = {i: (len(adj[i]) + 1) * (0.5 + imp[i]) for i in ids}
    rad = {i: radius(imp[i]) for i in ids}
    pos = forceatlas2(ids, adj, mass, inputs["old"], iterations=iterations, rad=rad)
    pos = remove_overlap(ids, pos, rad)      # 丸が重ならない（大きな点の中に小さな点が入らない）
    return {"pos": pos, "importance": imp, "mass": mass}


def save(result: dict, ver: int, seconds: float) -> None:
    pos, imp = result["pos"], result["importance"]

    def do() -> None:
        with news_db.session() as db:
            db.execute(delete(MapLayout))
            if pos:
                db.execute(insert(MapLayout), [{"entity_id": i, "x": x, "y": y, "importance": round(imp.get(i, 0.0), 4)} for i, (x, y) in pos.items()])
            for key, value in (("map_layout_ver", str(ver)), ("map_layout_at", datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
                               ("map_layout_stat", json.dumps({"nodes": len(pos), "seconds": round(seconds, 1)}))):
                row = db.get(NewsMeta, key)
                if row is None:
                    db.add(NewsMeta(key=key, value=value))
                else:
                    row.value = value
            db.commit()

    writer.run("news.map_layout", do, user=False)


def compute_and_save(now: datetime | None = None) -> dict:
    """夜: 重要度と配置を計算して保存する（計算は書き手の外。保存だけ書き手で 1 トランザクション）。"""
    started = time.monotonic()
    inputs = _read_inputs(now or datetime.now(_UTC))
    result = compute(inputs)
    secs = time.monotonic() - started
    save(result, inputs["ver"] + 1, secs)
    logger.info("地図の配置を計算しました（%d 点・%.1f 秒・版 %d）", len(result["pos"]), secs, inputs["ver"] + 1)
    return {"nodes": len(result["pos"]), "seconds": round(secs, 1), "version": inputs["ver"] + 1}
