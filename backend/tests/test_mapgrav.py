"""目的: 知識の地図の「重力」の配置（ForceAtlas2・夜に計算して保存）と、点の大きさ＝重要度の試験。外へ通信しない・LLM は呼ばない。
  ①重要度（検索で当たった回数・カードを開いた回数・直近の記事・関係の中心性を log で縮め、設定の重みで混ぜ、最大を 1 に）②使われ方の数え（メモリで数え、同じ点を 60 秒以内に数え直さず、
  まとめ書きで日ごとの集計の表へ足す。API の検索・カードが数える）③配置（保存される・再計算しても大きく動かない・同じ入力は同じ座標・機密の物は入らない・全体の地図の API が x・y・重要度を返す）
  ⑥丸の大きさの上限と比・葉の放射状・配置の式の版（2026-11-25 の銀河化）④重い点の周りに小さな点が集まる（重い点からつながる点までの距離の平均が、つながらない点までより短い）⑤Barnes-Hut が総当たりに近い・1,500 点が数秒で終わる。
根拠: 利用者の指示（2026-11-25「丸のサイズや距離感が等距離ではなく、重力の法則みたいに、検索数が多いものの丸が大きくなり、付随するものは大きなものに引きつけられる位置にいる」）・
ARCHITECTURE 6 章（配置は夜に計算して保存。形を毎回変えない）・Write Coordinator（数えるのはメモリ、書くのはまとめ書き）。
"""
from __future__ import annotations

import math
import random
import time

from sqlalchemy import func, select

from qnapnewshub import mapgrav, things, writer
from qnapnewshub import db as news_db
from qnapnewshub.db import Entity, EntityUsage, MapLayout, NewsMeta
from tests.test_kg import art, ent_id
from tests.test_things import _link, _rel


def _star_graph(hubs: int = 4, leaves: int = 12) -> dict[int, dict[int, float]]:
    """重い点（hubs 個）それぞれに小さな点が leaves 個つながり、重い点どうしは細くつながる。"""
    adj: dict[int, dict[int, float]] = {}
    nid = 0
    hub_ids = []
    for _ in range(hubs):
        nid += 1
        hub_ids.append(nid)
        adj[nid] = {}
    for h in hub_ids:
        for _ in range(leaves):
            nid += 1
            adj[nid] = {h: 1.0}
            adj[h][nid] = 1.0
    for a, b in zip(hub_ids, hub_ids[1:] + hub_ids[:1]):
        adj[a][b] = adj[b][a] = 0.4
    return adj


def _mass(adj: dict[int, dict[int, float]]) -> dict[int, float]:
    return {i: len(n) + 1.0 for i, n in adj.items()}


def _dist(pos, a, b) -> float:
    return math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1])


# ---- ① 重要度 ----

def test_importance_mixes_log_shrunk_components_by_weights():
    """目的: 重要度は、4 つの成分を log(1+x)/log(1+最大) に縮め、設定の重みで混ぜ、最大を 1 にする。信号が無い点は 0。重みを 0 にした成分は効かない。
    根拠: 利用者の指示（検索・カード・直近の記事・中心性を混ぜる。対数で縮める。係数は設定で）。"""
    ids = [1, 2, 3, 4]
    adj = {1: {2: 1.0, 3: 1.0, 4: 1.0}, 2: {1: 1.0}, 3: {1: 1.0}, 4: {1: 1.0}}
    cfg = {"weights": {"search": 1.0, "card": 0.0, "recent": 0.0, "centrality": 0.0}}
    imp = mapgrav.importance(ids, adj, {2: 100.0, 3: 10.0}, {4: 999.0}, {}, cfg)
    assert imp[2] == 1.0 and 0 < imp[3] < imp[2] and imp[4] == 0.0 and imp[1] == 0.0      # card の重みは 0 → 効かない
    assert abs(imp[3] - math.log1p(10) / math.log1p(100)) < 1e-9      # 対数で縮める（10 回は 100 回の半分ではなく約 0.52）
    cfg2 = {"weights": {"search": 0.0, "card": 0.0, "recent": 0.0, "centrality": 1.0}}
    pr = mapgrav.importance(ids, adj, {}, {}, {}, cfg2)
    assert pr[1] == 1.0 and all(pr[i] < 1.0 for i in (2, 3, 4))      # 中心性: 3 つの点が向く中心が最大
    assert max(mapgrav.importance(ids, adj, {}, {}, {}, {"weights": {"search": 1, "card": 1, "recent": 1, "centrality": 0}}).values()) == 0.0


def test_map_settings_are_validated_and_have_defaults(client):
    """目的: 設定の map.weights（各 0〜1）と map.window_days（1〜365）の検査。既定は既存の設定にも入る。根拠: 利用者の指示（係数は設定で）。"""
    from qnapnewshub import settings

    cfg = settings.load()
    assert cfg["map"]["window_days"] == 30 and set(cfg["map"]["weights"]) == {"search", "card", "recent", "centrality"}
    bad = {**cfg, "map": {"window_days": 30, "weights": {"search": 2.0}}}
    try:
        settings.normalize(bad)
        raise AssertionError("範囲外の重みを通した")
    except settings.ConfigError:
        pass
    assert settings.normalize({**cfg, "map": {"window_days": 7, "weights": {"search": 0.5}}})["map"]["window_days"] == 7


# ---- ② 数え ----

def test_usage_counted_in_memory_deduped_and_written_by_batch(client):
    """目的: 検索で当たった回数・カードを開いた回数はメモリに積み、まとめ書きまで DB に書かない。同じ点を 60 秒以内に数え直さない（打鍵ごとの検索で膨らまない）。
    まとめ書きで日ごとの表へ足し、次のまとめ書きでは同じ行へ加算する。API の検索（q つき）とカードが数える。
    根拠: 利用者の指示（検索とカードを開いた回数は、メモリで数えて Write Coordinator のまとめ書きで保存）・AGENTS（書き込みを増やさない）。"""
    art(1, [("Qwen", "model")])
    eid = ent_id("Qwen")
    mapgrav._seen.clear()  # noqa: SLF001
    assert mapgrav.note_search([eid, eid], now=1000.0) == 1      # 同じ呼び出しの中の重複も 1 回
    assert mapgrav.note_search([eid], now=1030.0) == 0           # 60 秒以内は数えない
    assert mapgrav.note_search([eid], now=1061.0) == 1
    with news_db.session() as db:
        assert db.scalar(select(func.count()).select_from(EntityUsage)) == 0      # まだ書いていない（メモリだけ）
    assert writer.get().buf.usage[(mapgrav._today(), "search", eid)] == 2  # noqa: SLF001
    writer.flush("t")
    with news_db.session() as db:
        row = db.get(EntityUsage, (mapgrav._today(), eid, "search"))  # noqa: SLF001
        assert row is not None and row.n == 2
    mapgrav._seen.clear()  # noqa: SLF001
    assert client.get(f"/api/v1/entities?q=Qwen").status_code == 200
    assert client.get(f"/api/v1/things/{eid}/card").status_code == 200
    assert client.get(f"/api/v1/kg/entities/{eid}/card").status_code == 200      # 60 秒以内なので 2 回目は数えない
    assert client.get("/api/v1/entities?q=").status_code == 200      # q が空なら数えない
    writer.flush("t")
    with news_db.session() as db:
        assert db.get(EntityUsage, (mapgrav._today(), eid, "search")).n == 3      # 加算
        assert db.get(EntityUsage, (mapgrav._today(), eid, "card")).n == 1


# ---- ③ 配置の保存と安定 ----

def test_layout_saved_versioned_stable_and_returned_by_overview(client):
    """目的: 夜の計算が座標・重要度を map_layout に保存し、版（news_meta.map_layout_ver）を 1 つ上げる。全体の地図の API が x・y・importance と layout の版を返す。
    記事が増えて再計算しても、前回の座標から短く整えるだけなので、前からある点は大きく動かない（全体の広がりの 15% 以内の平均。広がりをそろえる拡大縮小の分を含む）。機密の物は配置に入らない。
    根拠: 利用者の指示（形を毎回変えない。配置は夜に計算して保存・座標の表・版）。"""
    for i in range(1, 9):
        _link("Core", "uses", f"Leaf{i}", i)
    _link("Other", "uses", "Leaf1", 1)
    for h in range(5):      # ほかの塊（記事が増えても前の形が保たれることを見るため、ある程度の大きさにする）
        for k in range(7):
            _link(f"Hub{h}", "uses", f"Sat{h}x{k}", 1)
    _link("Secret", "uses", "Core", 1)
    with news_db.session() as db:
        e = db.get(Entity, ent_id("Secret"))
        e.sensitivity, e.ai_use = "confidential", "local_only"
        db.execute(news_db.Relationship.__table__.update().where(news_db.Relationship.subject_id == e.id).values(sensitivity="confidential"))
        db.commit()
    first = mapgrav.compute_and_save()
    assert first["version"] == 1 and first["nodes"] >= 40
    with news_db.session() as db:
        placed = {int(i): (x, y, imp) for i, x, y, imp in db.execute(select(MapLayout.entity_id, MapLayout.x, MapLayout.y, MapLayout.importance))}
        assert ent_id("Secret") not in placed and db.get(NewsMeta, "map_layout_ver").value == "1"
    assert max(v[2] for v in placed.values()) == 1.0
    o = client.get("/api/v1/things/map/overview?limit=50").json()
    core = next(n for n in o["nodes"] if n["name"] == "Core")
    assert (core["x"], core["y"]) == tuple(round(v, 2) for v in placed[ent_id("Core")][:2]) or abs(core["x"] - placed[ent_id("Core")][0]) < 0.01
    assert 0 < core["importance"] <= 1 and o["layout"]["version"] == 1 and o["layout"]["placed"] >= 40
    for i in range(20, 22):      # 新しい点が 2 つ増える
        _link("Core", "uses", f"New{i}", 1)
    second = mapgrav.compute_and_save()
    assert second["version"] == 2
    with news_db.session() as db:
        again = {int(i): (x, y) for i, x, y in db.execute(select(MapLayout.entity_id, MapLayout.x, MapLayout.y))}
    extent = max(math.hypot(x, y) for x, y, _ in placed.values())
    moved = [math.hypot(again[i][0] - placed[i][0], again[i][1] - placed[i][1]) for i in placed]
    assert sum(moved) / len(moved) < 0.15 * extent
    assert ent_id("New20") in again      # 新しい点にも座標がつく


def test_forceatlas2_is_deterministic_and_warm_start_keeps_shape():
    """目的: 同じ入力は同じ座標（乱数なし）。前回の座標から始めた短い再計算は、同じグラフなら形をほとんど変えず、新しい点は、つながる最も重い点の近くから始まる。
    根拠: 利用者の指示（形を毎回変えない・新しい点はつながる最も重い点の近くから）。"""
    adj = _star_graph(3, 8)
    mass = _mass(adj)
    ids = list(adj)
    a = mapgrav.forceatlas2(ids, adj, mass)
    assert a == mapgrav.forceatlas2(ids, adj, mass)
    ext = max(math.hypot(*p) for p in a.values())
    b = mapgrav.forceatlas2(ids, adj, mass, a, iterations=20)
    assert sum(math.hypot(a[i][0] - b[i][0], a[i][1] - b[i][1]) for i in ids) / len(ids) < 0.08 * ext
    hub = 1
    new = max(adj) + 1
    adj2 = {**{k: dict(v) for k, v in adj.items()}, new: {hub: 1.0}}
    adj2[hub][new] = 1.0
    c = mapgrav.forceatlas2(list(adj2), adj2, _mass(adj2), a, iterations=0)      # 0 回なら始めの位置そのもの
    assert _dist(c, new, hub) < 70 and all(math.hypot(c[i][0] - a[i][0], c[i][1] - a[i][1]) < 3 for i in ids)      # 全体の重心を戻すだけのずれ


# ---- ④ 重力: 重い点の周りに小さな点が集まる ----

def test_light_nodes_gather_around_their_heavy_node():
    """目的: 重い点からつながる点までの距離の平均が、つながらない点までの距離の平均より短い（小さな点は、つながる重い点の周りに集まる）。重い点どうしは、つながる小さな点より離れる。
    根拠: 利用者の指示（付随するものは大きなものに引きつけられる位置にいる・大きな点どうしは離し、小さな点は周りに）。"""
    adj = _star_graph(4, 14)
    mass = _mass(adj)
    pos = mapgrav.forceatlas2(list(adj), adj, mass)
    hubs = [1, 2, 3, 4]
    near = [_dist(pos, h, j) for h in hubs for j in adj[h] if j not in hubs]
    far = [_dist(pos, h, j) for h in hubs for j in adj if j not in hubs and j not in adj[h]]
    assert sum(near) / len(near) < 0.75 * sum(far) / len(far)
    hub_gap = [_dist(pos, a, b) for a in hubs for b in hubs if a < b]
    assert sum(hub_gap) / len(hub_gap) > 1.2 * sum(near) / len(near)


# ---- ⑤ Barnes-Hut の精度と速さ ----

def test_barnes_hut_close_to_brute_force_and_fast():
    """目的: 四分木の近似の反発が総当たりに近い（相対誤差の平均 15% 以下）。1,500 点・160 回の反復が、遅い機械でも数十秒に収まる目安（ここでは 20 秒以内）で終わる。
    根拠: 利用者の指示（Barnes-Hut の近似。1,500〜5,000 点で数十秒以内を目標。測って報告）。"""
    r = random.Random(3)
    n = 250
    px = [r.random() * 500 for _ in range(n)]
    py = [r.random() * 500 for _ in range(n)]
    m = [1 + r.random() * 5 for _ in range(n)]
    fx, fy = [0.0] * n, [0.0] * n
    mapgrav._repulse(px, py, m, fx, fy, 1.0, mapgrav.THETA)  # noqa: SLF001
    errs = []
    for a in range(n):
        bx = by = 0.0
        for b in range(n):
            if a != b:
                dx, dy = px[a] - px[b], py[a] - py[b]
                k = m[a] * m[b] / (dx * dx + dy * dy)
                bx += dx * k
                by += dy * k
        errs.append(math.hypot(fx[a] - bx, fy[a] - by) / math.hypot(bx, by))
    assert sum(errs) / n < 0.15
    rr = random.Random(1)
    adj: dict[int, dict[int, float]] = {i: {} for i in range(1, 1501)}
    targets = [1, 2]
    for i in range(3, 1501):
        j = rr.choice(targets)
        adj[i][j] = adj[j][i] = 1.0
        targets += [i, j]
    t0 = time.monotonic()
    pos = mapgrav.forceatlas2(list(adj), adj, _mass(adj), iterations=160)
    assert time.monotonic() - t0 < 20 and len(pos) == 1500 and all(math.isfinite(v) for p in pos.values() for v in p)


def test_circles_do_not_overlap_and_radius_is_capped_and_matches_screen_formula():
    """目的: 夜の配置のあと、丸（半径は重要度から）が重ならない。半径は 2〜9 に収まり（最大は最小の約 4.5 倍）、重要度は平方根で縮め、上位 5% の値で頭打ち（それ以上は同じ大きさ）。
    半径の式は画面（mapLayout.ts の radiusOf）と同じ（式を変えたら両方を直す）。
    根拠: 利用者の指示（2026-11-25「丸の大きいサイズが大きすぎる」「丸の大きさはある程度の所を最大にして大小を決める」）・実データで、大きな丸の中に小さな丸が重なって見えた不具合。"""
    adj = _star_graph(5, 12)
    ids = list(adj)
    imp = mapgrav.importance(ids, adj, {}, {}, {i: float(len(adj[i])) for i in ids})
    out = mapgrav.compute({"ids": ids, "adj": adj, "search": {}, "card": {}, "recent": {i: float(len(adj[i])) for i in ids}, "old": {}, "cfg": mapgrav.map_config()})
    pos = out["pos"]
    cap = mapgrav.size_cap(out["importance"].values())
    rad = {i: mapgrav.radius(out["importance"][i], cap) for i in ids}
    over = [(a, b) for a in ids for b in ids if a < b and _dist(pos, a, b) < rad[a] + rad[b] - 0.5]
    assert over == [] and imp
    assert min(rad.values()) >= 2.0 and max(rad.values()) <= 9.0 and 1.5 < max(rad.values()) / min(rad.values()) <= 4.5
    assert mapgrav.radius(0) == 2.0 and mapgrav.radius(1) == 9.0
    assert mapgrav.radius(0.5, 0.5) == mapgrav.radius(1.0, 0.5) == 9.0      # 上位 5% の値で頭打ち
    assert mapgrav.radius(0.25) == 2.0 + 7.0 * 0.5      # 平方根
    vals = [i / 100 for i in range(1, 101)]
    assert mapgrav.size_cap(vals) == 0.96
    from pathlib import Path

    ts = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "features" / "knowledge" / "mapLayout.ts").read_text(encoding="utf-8")
    assert "RADIUS_MIN = 2" in ts and "RADIUS_MAX = 9" in ts and "Math.sqrt(Math.max(0, importance) / cap)" in ts


def test_leaves_are_placed_radially_around_their_parent():
    """目的: 次数 1 の点（葉）が、つながる点（親）の周りに放射状に並ぶ。角度は等間隔で重ならず、丸どうしも重ならず、距離は短い（親の縁から数点分）。親の座標は変えない。次数 1 でない点は動かさない。多すぎて 1 周に入らなければ輪を重ねる。
    根拠: 利用者の指示（2026-11-25「大きな集合体に対しては放射状に関連する Things がつながる」）・Obsidian のグラフ（葉が親の周りに放射状に付く）。"""
    adj: dict[int, dict[int, float]] = {1: {}, 2: {}}
    for k in range(3, 11):      # 親 1 に葉 8 枚
        adj[k] = {1: 1.0}
        adj[1][k] = 1.0
    adj[1][2] = adj[2][1] = 1.0      # 親 1 は点 2 ともつながる（葉ではない）
    adj[2][99] = 1.0
    adj[99] = {2: 1.0}
    adj[1][2] = 1.0
    pos = {i: (float(i * 3), float(-i)) for i in adj}
    rad = {i: 3.0 for i in adj}
    rad[1] = 9.0
    out = mapgrav.radial_leaves(adj, pos, rad)
    assert out[1] == pos[1] and out[2] == pos[2]
    leaves = list(range(3, 11))
    ang = sorted(math.atan2(out[k][1] - pos[1][1], out[k][0] - pos[1][0]) % (2 * math.pi) for k in leaves)
    gaps = [(ang[(j + 1) % 8] - ang[j]) % (2 * math.pi) for j in range(8)]
    assert min(gaps) > 0.99 * 2 * math.pi / 8      # 等間隔・角度の重なりなし
    assert all(_dist(out, 1, k) <= rad[1] + rad[k] + mapgrav.LEAF_GAP + 3.0 for k in leaves)      # 短い
    assert all(_dist(out, a, b) >= rad[a] + rad[b] for a in leaves for b in leaves if a < b)      # 丸も重ならない
    many: dict[int, dict[int, float]] = {1: {}, 2: {2: 1.0}}
    many = {1: {2: 1.0}, 2: {1: 1.0}}
    for k in range(3, 63):
        many[k] = {1: 1.0}
        many[1][k] = 1.0
    r2 = {i: 3.0 for i in many}
    o2 = mapgrav.radial_leaves(many, {i: (0.0, 0.0) if i == 1 else (5.0, 5.0) for i in many}, r2)
    ls = list(range(3, 63))
    assert all(_dist(o2, a, b) >= 5.9 for a in ls for b in ls if a < b)      # 輪を重ねても重ならない


def test_layout_algo_version_rebuilds_from_scratch(client):
    """目的: 配置の式の版（LAYOUT_ALGO）が保存した版（news_meta.map_layout_algo）と違えば、前回の座標を使わず最初から作り直す。同じなら前回の座標から続ける。
    根拠: 利用者の指示（配置の値を変えたら版を上げて作り直す）。"""
    for i in range(1, 9):
        _link("Core", "uses", f"Leaf{i}", i)
    mapgrav.compute_and_save()
    with news_db.session() as db:
        assert db.get(NewsMeta, "map_layout_algo").value == str(mapgrav.LAYOUT_ALGO)
    assert mapgrav._read_inputs(mapgrav.datetime.now(mapgrav._UTC))["old"]  # noqa: SLF001
    with news_db.session() as db:
        db.get(NewsMeta, "map_layout_algo").value = "0"
        db.commit()
    assert mapgrav._read_inputs(mapgrav.datetime.now(mapgrav._UTC))["old"] == {}  # noqa: SLF001


def test_night_layout_failure_does_not_stop_aggregate(monkeypatch):
    """目的: 配置の計算が例外で落ちても、夜の集計を止めない（前回の配置が残る）。根拠: AGENTS「取得の失敗でアプリ全体を落とさない」・夜の仕事は 1 つの失敗で止めない。"""
    from qnapnewshub import kg_job

    monkeypatch.setattr(mapgrav, "compute_and_save", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert kg_job._layout()["error"] is True  # noqa: SLF001
    art(1, [("A1", "tech"), ("B1", "tech")], relations=[_rel("A1", "uses", "B1")])
    assert things.compute_communities is not None
