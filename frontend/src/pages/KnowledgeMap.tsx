import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type Graph from "graphology";
import type Sigma from "sigma";
import { REL_COLOR, REL_LABEL, REL_TYPES, exportZip, neighbors, type GraphEdge, type GraphNode, type RelType } from "../api/kg";
import { ENTITY_TYPE_LABEL, useEntities, type EntityType } from "../api/news";
import { communityMembers, useOverview, type OverviewNode } from "../api/things";
import { fallbackImportance, layoutOverview, placeNear, radiusOf, ringPositions, sizeOf, type Pos } from "../features/knowledge/mapLayout";
import { Card, field, ghost, primary } from "../features/news/settingsUi";

/** 知識の地図（/map）。Obsidian のグラフビューのように、**最初に全体**（サーバーが返す主要な 1,500 点と、まとまりの代表）を出し、近づく（拡大・まとまりを押す）と中を開く。
 *  - 配置は重力のように: 夜にサーバーが ForceAtlas2 で計算して保存した座標（x・y）で出す（毎回形が変わらない）。大きな点は離れ、小さな点は重い点の周りに集まる。
 *    保存が無い（まだ夜の計算が済んでいない）ときは、まとまりごとの塊（mapLayout.layoutOverview）で出す。あとから足す点は、つながる点の近くへ置く。
 *  - 点の大きさ＝重要度（検索で当たった回数・カードを開いた回数・直近の記事での出現・関係の中心性。サーバーが log で縮めて 0〜1 にした値）。色＝種類。線の太さ＝根拠の数・線の色＝関係の型。
 *  - 検索: 語を入れると「強調」（一致は大きく・ほかは薄く）か「絞る」（一致と近傍だけ）を選べ、一致した点へ寄る。全体に無い一致は近傍ごと足す。
 *  - ローカル: 選んだ点の周りを、深さ 1〜3 のつまみで（点を押す → 「ローカルで見る」）。小さな絵なので、画面側で ForceAtlas2 を回して置く。「全体へ戻る」で最初の絵へ。
 *  - 名前の出し方: 重要度の大きい 24 点はいつも、ほかは大きな点から順に、拡大するほど細かく（sigma の名前の間引き）。
 *  描画は sigma.js（WebGL）＋ graphology を地図を開いたときに遅延読み込み（版固定）。 */
const PERIODS: [number, string][] = [[0, "全期間"], [30, "30 日"], [90, "90 日"], [365, "1 年"]];
const CONFS: [number, string][] = [[0, "すべて"], [0.3, "0.3 以上"], [0.5, "0.5 以上"], [0.7, "0.7 以上"]];
const NODE_COLOR: Record<EntityType, string> = {
  model: "#2563eb", software: "#0d9488", org: "#b45309", person: "#be185d", tech: "#7c3aed", product: "#0891b2", game: "#65a30d", policy: "#dc2626", place: "#52525b", other: "#71717a",
};
const LOCAL_LIMIT = [200, 400, 700];
const BIG_LABELS = 24;
const SELECT_COLOR = "#f59e0b";

type Mode = "highlight" | "filter";
interface NodeRec { id: number; name: string; type: EntityType; degree: number; article_count: number; community: number; isolated?: boolean; importance?: number; x?: number; y?: number }
interface EdgeRec { id: number; source: number; target: number; type: RelType; directed: boolean; confidence: number; status: string; evidence_count: number; last_at: string }
interface NodeFlag { off: boolean; hit: boolean; dim: boolean }
interface EdgeFlag { off: boolean; dim: boolean }

function useDark(): boolean {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  useEffect(() => {
    const ob = new MutationObserver(() => setDark(document.documentElement.classList.contains("dark")));
    ob.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => ob.disconnect();
  }, []);
  return dark;
}

const sinceIso = (days: number) => (days ? new Date(Date.now() - days * 86_400_000).toISOString() : null);
const toRec = (n: OverviewNode | GraphNode, iso = false): NodeRec => {
  const o = n as OverviewNode;
  return { id: n.id, name: n.name, type: n.type, degree: "degree" in n ? (n.degree ?? 0) : 0, article_count: n.article_count, community: "community" in n ? n.community : 0, isolated: iso,
    importance: typeof o.importance === "number" ? o.importance : undefined, x: typeof o.x === "number" ? o.x : undefined, y: typeof o.y === "number" ? o.y : undefined };
};
const rgba = (hex: string, a: number) => { const v = parseInt(hex.slice(1), 16); return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${a})`; };
const edgeWeight = (e: EdgeRec) => Math.max(0.05, e.confidence) * (1 + Math.log1p(e.evidence_count));

type Theme = { label: string; dimNode: string; dimEdge: string };
const themeOf = (dark: boolean): Theme => (dark ? { label: "#e4e4e7", dimNode: "#3f3f46", dimEdge: "rgba(161,161,170,0.05)" } : { label: "#27272a", dimNode: "#e4e4e7", dimEdge: "rgba(113,113,122,0.05)" });

function addEdge(g: Graph, e: EdgeRec, local: boolean) {
  const solid = REL_COLOR[e.type] ?? REL_COLOR.other;
  const weak = e.status === "inferred" || e.status === "statistical";      // 推測・集計は薄く（sigma は破線を持たないので、色の濃さで区別する）
  const attrs = { size: 0.6 + Math.min(e.evidence_count, 12) * 0.22, color: rgba(solid, weak ? 0.3 : local ? 0.85 : 0.55), baseColor: solid, type: local && e.directed ? "arrow" : "line",
    rel: e.type, confidence: e.confidence, evidence: e.evidence_count, last_at: e.last_at, weight: edgeWeight(e) };
  const key = `e${e.id}`;
  if (local && e.directed) g.addDirectedEdgeWithKey(key, String(e.source), String(e.target), attrs);
  else g.addUndirectedEdgeWithKey(key, String(e.source), String(e.target), attrs);
}

export default function KnowledgeMapPage() {
  const dark = useDark();
  const [params] = useSearchParams();
  const box = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const nodes = useRef(new Map<number, NodeRec>());      // いま描いている点
  const edges = useRef(new Set<number>());
  const nodeSize = (imp: number) => (sizing.current.world ? radiusOf(imp) : sizeOf(imp) * sizing.current.unit);
  const pos = useRef(new Map<number, Pos>());
  const centers = useRef(new Map<number, Pos & { r: number }>());
  const home = useRef<{ nodes: NodeRec[]; edges: EdgeRec[]; pos: Map<number, Pos>; world: boolean } | null>(null);      // 全体の絵（ローカルから戻るとき）
  const nodeFlags = useRef(new Map<string, NodeFlag>());
  const edgeFlags = useRef(new Map<string, EdgeFlag>());
  const hlEdges = useRef(new Set<string>());
  const selId = useRef<string | null>(null);
  const sizing = useRef({ world: false, unit: 1 });      // 点の大きさの基準（保存した配置＝ワールドの半径・それ以外＝画面の大きさに合わせる）
  const theme = useRef(themeOf(dark));
  theme.current = themeOf(dark);
  const [counts, setCounts] = useState({ n: 0, e: 0, hidden: 0 });
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<NodeRec | null>(null);
  const [msg, setMsg] = useState("");
  const [view, setView] = useState<"overview" | "local">("overview");
  const [depth, setDepth] = useState(1);
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<Mode>("highlight");
  const [hits, setHits] = useState<Set<number>>(new Set());
  const [group, setGroup] = useState<number | null>(null);
  const [period, setPeriod] = useState(0);
  const [kind, setKind] = useState("");
  const [minConf, setMinConf] = useState(0);
  const [showIso, setShowIso] = useState(false);
  const [active, setActive] = useState<Set<RelType>>(new Set(REL_TYPES));
  const [ready, setReady] = useState(false);
  const localRoot = useRef(0);
  const overview = useOverview();
  const found = useEntities({ q, type: "", days: 0, sort: "count" });
  const filters = useRef({ period, kind, minConf, active, showIso });
  filters.current = { period, kind, minConf, active, showIso };
  const marks = useRef({ q: "", mode, hits, group, view });
  marks.current = { q, mode, hits, group, view };

  /** 絞り込み（種類・確かさ・期間・型・孤立）と、検索・まとまりの強調／絞りの印を、点・線の旗に反映して描き直す。 */
  const apply = useCallback(() => {
    const g = graphRef.current, sg = sigmaRef.current;
    if (!g || !sg) return;
    const f = filters.current, m = marks.current;
    const since = f.period ? Date.now() - f.period * 86_400_000 : 0;
    const focus = m.hits.size > 0 ? m.hits : null;
    let hidden = 0, shownEdges = 0;
    edgeFlags.current.clear();
    nodeFlags.current.clear();
    g.forEachEdge((key, a) => {
      const ok = f.active.has(a.rel as RelType) && a.confidence >= f.minConf && (!since || !a.last_at || new Date(a.last_at as string).getTime() >= since);
      if (!ok) hidden += 1; else shownEdges += 1;
      edgeFlags.current.set(key, { off: !ok, dim: false });
    });
    const near = new Set<number>();
    if (focus && m.mode === "filter") focus.forEach((id) => { if (!g.hasNode(String(id))) return; near.add(id); g.forEachNeighbor(String(id), (nb) => { near.add(Number(nb)); }); });
    let visible = 0;
    g.forEachNode((key, a) => {
      const id = Number(key);
      const off = (!!f.kind && a.kind !== f.kind) || (!!a.iso && !f.showIso) || (!!focus && m.mode === "filter" && !near.has(id));
      if (!off) visible += 1;
      nodeFlags.current.set(key, { off, hit: !!focus && focus.has(id), dim: !!focus && m.view === "overview" && m.mode === "highlight" && !focus.has(id) });
    });
    if (focus && m.view === "overview" && m.mode === "highlight") g.forEachEdge((key, _a, s, t) => { if (!(focus.has(Number(s)) && focus.has(Number(t)))) edgeFlags.current.get(key)!.dim = true; });
    setCounts({ n: visible, e: shownEdges + hidden, hidden });
    sg.refresh();
  }, []);

  /** 絵を丸ごと入れ替える（全体 ⇄ ローカル）。local のときは、画面側で ForceAtlas2 を回して置く（点が少ないので短い）。 */
  const draw = useCallback(async (ns: NodeRec[], es: EdgeRec[], where: Map<number, Pos> | null, local: boolean, world: boolean) => {
    const g = graphRef.current, sg = sigmaRef.current;
    if (!g || !sg) return;
    nodes.current = new Map(ns.map((n) => [n.id, n]));
    edges.current = new Set();
    const maxDeg = Math.max(0, ...ns.map((n) => n.degree));
    sizing.current = { world, unit: 1 };
    g.clear();
    selId.current = null;
    hlEdges.current = new Set();
    const order = [...ns].sort((a, b) => b.degree - a.degree || a.id - b.id);
    const seed = new Map(order.map((n, i) => [n.id, { x: Math.sqrt(i + 0.5) * 30 * Math.cos(i * 2.39996), y: Math.sqrt(i + 0.5) * 30 * Math.sin(i * 2.39996) }]));
    for (const n of ns) {
      const p = where?.get(n.id) ?? seed.get(n.id)!;
      const imp = n.importance ?? fallbackImportance(n.degree, maxDeg);
      g.addNode(String(n.id), { x: p.x, y: p.y, imp: n.isolated ? 0 : imp, size: 1, label: n.name, color: NODE_COLOR[n.type] ?? NODE_COLOR.other, kind: n.type, community: n.community, iso: !!n.isolated, degree: n.degree, forceLabel: false });
    }
    for (const e of es) {
      if (!nodes.current.has(e.source) || !nodes.current.has(e.target) || edges.current.has(e.id)) continue;
      edges.current.add(e.id);
      addEdge(g, e, local);
    }
    if (local) {
      const { default: fa2 } = await import("graphology-layout-forceatlas2");
      const n = g.order;
      fa2.assign(g, { iterations: n > 300 ? 120 : 250, settings: { ...fa2.inferSettings(g), linLogMode: true, outboundAttractionDistribution: true, gravity: 1, scalingRatio: 6, slowDown: 2, barnesHutOptimize: n > 200 }, getEdgeWeight: "weight" });
      pos.current = new Map(g.mapNodes((k, a) => [Number(k), { x: a.x as number, y: a.y as number }] as [number, Pos]));
    } else if (where) {
      // 全体の地図: 重要度の大きい点だけ、縮小したままでも名前を出す。ほかは拡大するほど（大きな点から）出す（読めない名前の山にしない）
      const top = [...ns].filter((n) => !n.isolated).sort((a, b) => (b.importance ?? fallbackImportance(b.degree, maxDeg)) - (a.importance ?? fallbackImportance(a.degree, maxDeg)) || a.id - b.id).slice(0, BIG_LABELS);
      for (const n of top) g.setNodeAttribute(String(n.id), "forceLabel", true);
    }
    // 点の大きさ: 保存した配置はワールドの半径（丸が重ならないよう夜に離した距離）。ローカル・保存の無い配置は、全体が画面に入ったときに px で読める大きさに合わせる
    const xs = g.mapNodes((_k, a) => a.x as number), ys = g.mapNodes((_k, a) => a.y as number);
    const extent = Math.max(1, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
    sizing.current = { world, unit: extent / 400 };
    g.forEachNode((k, a) => g.setNodeAttribute(k, "size", nodeSize(a.imp as number)));
    sg.setCustomBBox(null);
    apply();
    sg.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
  }, [apply]);

  // sigma と graphology は地図を開いたときだけ読む（遅延読み込み）
  useEffect(() => {
    let dead = false;
    let sg: Sigma | null = null;
    void Promise.all([import("sigma"), import("graphology")]).then(([sm, gm]) => {
      if (dead || !box.current) return;
      const g = new gm.default({ multi: true, type: "mixed" });
      graphRef.current = g;
      sg = new sm.default(g, box.current, {
        renderEdgeLabels: false, labelRenderedSizeThreshold: 7, labelDensity: 0.7, labelGridCellSize: 110, labelSize: 12, labelFont: "system-ui, sans-serif", labelColor: { color: theme.current.label },
        zIndex: true, minCameraRatio: 0.02, maxCameraRatio: 3, itemSizesReference: "positions", zoomToSizeRatioFunction: (r) => r, defaultEdgeType: "line", enableEdgeEvents: false,
        nodeReducer: (key, a) => {
          const f = nodeFlags.current.get(key);
          const out = { ...a };
          if (f?.off) { out.hidden = true; return out; }
          if (f?.dim) { out.color = theme.current.dimNode; out.label = ""; out.forceLabel = false; out.zIndex = 0; return out; }
          if (f?.hit) { out.size = a.size * 1.5; out.zIndex = 2; out.forceLabel = true; }
          if (key === selId.current) { out.color = SELECT_COLOR; out.size = a.size * 1.7 + 2; out.zIndex = 3; out.forceLabel = true; }
          return out;
        },
        edgeReducer: (key, a) => {
          const f = edgeFlags.current.get(key);
          const out = { ...a };
          if (f?.off) { out.hidden = true; return out; }
          if (f?.dim) { out.color = theme.current.dimEdge; return out; }
          if (hlEdges.current.has(key)) { out.color = a.baseColor as string; out.size = a.size * 2; out.zIndex = 1; }
          return out;
        },
      });
      sigmaRef.current = sg;
      const el = box.current as unknown as { __sigma?: Sigma; __graph?: Graph; __tap?: (id: number) => void };
      el.__sigma = sg;
      el.__graph = g;
      const pick = (key: string | null) => {
        selId.current = key;
        hlEdges.current = new Set(key && g.hasNode(key) ? g.edges(key) : []);
        setSelected(key ? nodes.current.get(Number(key)) ?? null : null);
        sg!.refresh();
      };
      el.__tap = (id: number) => pick(String(id));
      (el as { __stat?: () => Record<string, number> }).__stat = () => {      // 試験用: 旗の数（一致・薄く・隠す）と、いつも名前を出す点の数
        const c = { hit: 0, dim: 0, off: 0, big: 0, sizeMax: 0, sizeMin: 1e9 };
        for (const f of nodeFlags.current.values()) { if (f.hit) c.hit += 1; if (f.dim) c.dim += 1; if (f.off) c.off += 1; }
        g.forEachNode((_k, a) => { if (a.forceLabel) c.big += 1; if (!a.iso) { c.sizeMax = Math.max(c.sizeMax, a.size as number); c.sizeMin = Math.min(c.sizeMin, a.size as number); } });
        return c;
      };
      sg.on("clickNode", ({ node }) => pick(node));
      sg.on("clickStage", () => pick(null));
      sg.on("enterNode", () => { if (box.current) box.current.style.cursor = "pointer"; });
      sg.on("leaveNode", () => { if (box.current) box.current.style.cursor = ""; });
      box.current.setAttribute("data-ready", "true");
      setReady(true);
    });
    return () => { dead = true; sg?.kill(); sigmaRef.current = null; graphRef.current = null; };
  }, []);
  useEffect(() => {
    const sg = sigmaRef.current;
    if (!sg) return;
    sg.setSetting("labelColor", { color: theme.current.label });
    sg.refresh();
  }, [dark, ready]);
  useEffect(() => { apply(); }, [period, kind, minConf, active, showIso, hits, mode, apply]);

  // 全体の地図: サーバーの応答が来て sigma の準備ができたら、保存した配置（無ければまとまりごとの塊）で描く
  const drawn = useRef(false);
  useEffect(() => {
    const o = overview.data;
    if (!ready || !o || drawn.current) return;
    drawn.current = true;
    const ns = o.nodes.map((n) => toRec(n));
    const iso = o.isolated.map((n) => toRec(n, true));
    const saved = ns.filter((n) => n.x !== undefined && n.y !== undefined);
    const all = new Map<number, Pos>();
    if (saved.length > 0) {
      for (const n of saved) all.set(n.id, { x: n.x!, y: n.y! });
      centers.current = new Map();
      const adj = new Map<number, number[]>();
      for (const e of o.edges) { (adj.get(e.source) ?? adj.set(e.source, []).get(e.source)!).push(e.target); (adj.get(e.target) ?? adj.set(e.target, []).get(e.target)!).push(e.source); }
      for (const n of ns) if (!all.has(n.id)) all.set(n.id, placeNear(n.id, adj.get(n.id) ?? [], all));      // 夜のあとに増えた点は、つながる点の近くから
      const reach = Math.max(200, ...[...all.values()].map((p) => Math.hypot(p.x, p.y)));
      for (const [id, p] of ringPositions(iso.map((n) => n.id), reach * 1.12)) all.set(id, p);
    } else {
      const lay = layoutOverview(ns);
      centers.current = lay.centers;
      for (const [id, p] of lay.pos) all.set(id, p);
      for (const [id, p] of ringPositions(iso.map((n) => n.id), Math.max(...[...lay.centers.values()].map((c) => Math.hypot(c.x, c.y) + c.r), 200) + 120)) all.set(id, p);
    }
    pos.current = all;
    home.current = { nodes: [...ns, ...iso], edges: o.edges, pos: all, world: saved.length > 0 };
    void draw([...ns, ...iso], o.edges, all, false, saved.length > 0).then(() => {
      const focus = Number(params.get("focus"));
      const comm = Number(params.get("community"));
      if (Number.isFinite(focus) && focus > 0) void enterLocal(focus, 1);
      else if (Number.isFinite(comm) && comm > 0) void openGroup(comm);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, overview.data]);

  /** 全体の絵に点・線を足す（検索の一致・まとまりの中・「周りを広げる」）。足した点は、保存した座標があればそこへ、無ければつながる点の近くへ置く。 */
  const merge = useCallback((ns: NodeRec[], es: EdgeRec[], local: boolean) => {
    const g = graphRef.current;
    if (!g) return 0;
    const adj = new Map<number, number[]>();
    for (const e of es) { (adj.get(e.source) ?? adj.set(e.source, []).get(e.source)!).push(e.target); (adj.get(e.target) ?? adj.set(e.target, []).get(e.target)!).push(e.source); }
    const fresh = ns.filter((n) => !nodes.current.has(n.id));
    const maxDeg = Math.max(0, ...[...nodes.current.values()].map((n) => n.degree), ...ns.map((n) => n.degree));
    for (const n of fresh) {
      nodes.current.set(n.id, n);
      const c = n.community ? centers.current.get(n.community) : undefined;
      const p = n.x !== undefined && n.y !== undefined && !local ? { x: n.x, y: n.y } : placeNear(n.id, adj.get(n.id) ?? [], pos.current, c);
      pos.current.set(n.id, p);
      const imp = n.importance ?? fallbackImportance(n.degree, maxDeg);
      g.addNode(String(n.id), { x: p.x, y: p.y, imp, size: nodeSize(imp), label: n.name, color: NODE_COLOR[n.type] ?? NODE_COLOR.other, kind: n.type, community: n.community, iso: false, degree: n.degree, forceLabel: false });
    }
    for (const e of es) if (nodes.current.has(e.source) && nodes.current.has(e.target) && !edges.current.has(e.id)) { edges.current.add(e.id); addEdge(g, e, local); }
    return fresh.length;
  }, []);

  const guard = async (job: () => Promise<void>) => {
    setBusy(true);
    setMsg("");
    try { await job(); } catch { setMsg("地図を読み込めませんでした。"); } finally { setBusy(false); }
  };
  /** 一致した点へカメラを寄せる（枠に収まる大きさまで。寄りすぎない）。 */
  const fitTo = (ids: Set<number>) => {
    const sg = sigmaRef.current, g = graphRef.current;
    if (!sg || !g || ids.size === 0) return;
    const pts: Pos[] = [];
    for (const id of ids) {
      const key = String(id);
      if (!g.hasNode(key) || nodeFlags.current.get(key)?.off) continue;
      const a = g.getNodeAttributes(key);
      pts.push(sg.viewportToFramedGraph(sg.graphToViewport({ x: a.x as number, y: a.y as number })));
    }
    if (pts.length === 0) return;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const w = Math.max(...xs) - Math.min(...xs), h = Math.max(...ys) - Math.min(...ys);
    void sg.getCamera().animate({ x: (Math.max(...xs) + Math.min(...xs)) / 2, y: (Math.max(...ys) + Math.min(...ys)) / 2, ratio: Math.min(1, Math.max(0.06, Math.max(w, h) * 1.35 + 0.04)) }, { duration: 250 });
  };

  const fromNeighbors = (res: Awaited<ReturnType<typeof neighbors>>): { ns: NodeRec[]; es: GraphEdge[] } => ({
    ns: [...res.seeds, ...res.levels.flatMap((l) => l.nodes)].map((n) => toRec(n)), es: res.levels.flatMap((l) => l.edges) });

  /** ローカル: 選んだ点の周りを深さ 1〜3 で。全体の絵とは別に、その周りだけを描く。 */
  const enterLocal = useCallback((id: number, d: number) => guard(async () => {
    const f = filters.current;
    const res = await neighbors({ ids: [id], depth: d, limit: LOCAL_LIMIT[d - 1], types: f.active.size === REL_TYPES.length ? [] : [...f.active], min_confidence: f.minConf, since: sinceIso(f.period) });
    const { ns, es } = fromNeighbors(res);
    if (res.seeds.length === 0) { setMsg("見つからないThingsか、読めない区分のThingsです。"); return; }
    localRoot.current = id;
    setView("local");
    setDepth(d);
    setHits(new Set([id]));
    setMode("highlight");
    setGroup(null);
    await draw(ns, es, null, true, false);
    setSelected(nodes.current.get(id) ?? null);
    selId.current = String(id);      // 始めの点を選んだ印（色と、つながる線の強調）
    hlEdges.current = new Set(graphRef.current?.hasNode(String(id)) ? graphRef.current.edges(String(id)) : []);
    sigmaRef.current?.refresh();
    if (res.truncated) setMsg(`上限（${LOCAL_LIMIT[d - 1]} 本）で切りました。深さを下げるか、絞り込んでください。`);
    else if (es.length === 0) setMsg("この条件で、つながるThingsはありません。");
  }), [draw]);

  const backToOverview = () => {
    if (!home.current) return;
    setView("overview");
    setHits(new Set());
    setGroup(null);
    setMsg("");
    setSelected(null);
    pos.current = new Map(home.current.pos);
    void draw(home.current.nodes, home.current.edges, home.current.pos, false, home.current.world);
  };

  /** まとまりを押す → 中を開く（全体に無い点を足して、その塊へ寄る。ほかは薄く）。 */
  const openGroup = useCallback((id: number) => guard(async () => {
    if (marks.current.view === "local") backToOverview();
    const res = await communityMembers(id);
    merge(res.nodes.map((n) => toRec(n)), res.edges, false);
    setGroup(id);
    setHits(new Set(res.nodes.map((n) => n.id)));
    setMode("highlight");
    setQ("");
    apply();
    fitTo(new Set(res.nodes.map((n) => n.id)));
  }), [merge, apply]);

  // 検索: 一致した点を強調／絞り、寄る。全体に無い一致は近傍ごと足す
  useEffect(() => {
    const items = found.data?.items ?? [];
    if (!q.trim()) { if (marks.current.group === null && marks.current.view === "overview") { setHits(new Set()); } return; }
    if (!ready || found.isFetching) return;
    const ids = items.map((i) => i.id);
    void guard(async () => {
      const missing = ids.filter((i) => !nodes.current.has(i)).slice(0, 30);
      if (missing.length) {
        const res = await neighbors({ ids: missing, depth: 1, limit: 200 });
        const { ns, es } = fromNeighbors(res);
        merge(ns, es, marks.current.view === "local");
      }
      const present = new Set(ids.filter((i) => nodes.current.has(i)));
      setGroup(null);
      setHits(present);
      apply();
      fitTo(present);
      if (items.length === 0) setMsg("見つかりません。");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, found.data, found.isFetching, ready]);

  const expand = useCallback((id: number) => guard(async () => {
    const f = filters.current;
    const res = await neighbors({ ids: [id], depth: 1, limit: 80, types: f.active.size === REL_TYPES.length ? [] : [...f.active], min_confidence: f.minConf, since: sinceIso(f.period) });
    const { ns, es } = fromNeighbors(res);
    merge(ns, es, marks.current.view === "local");
    apply();
    if (res.truncated) setMsg("上限（80 本）で切りました。絞り込むか、ローカルで見てください。");
    else if (es.length === 0) setMsg("この条件で、つながるThingsはありません。");
  }), [merge, apply]);

  const toggle = (t: RelType) => setActive((s) => { const n = new Set(s); if (n.has(t)) n.delete(t); else n.add(t); return n; });
  const results = (found.data?.items ?? []).slice(0, 6);
  const o = overview.data;
  const used = useMemo(() => [...new Set([...nodes.current.values()].map((n) => n.type))], [counts.n, view]);

  return (
    <div className="mx-auto min-w-0 max-w-5xl space-y-3 p-2 md:p-6" data-testid="map-page">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">知識の地図</h1>
        <p className="text-xs leading-relaxed text-zinc-500">最初は全体です（関係の多い Things を主な 1,500 点まで。配置は夜に計算して保存した重力のような並びで、大きな点は離れ、小さな点は関係の強い大きな点の周りに集まります）。語で探すと一致を強調するか絞り込み、まとまりや点を押すと中を開きます。色は種類、点の大きさは重要度（検索・カードを開いた回数、最近の記事、関係の中心）、線の太さは根拠の数、線の色は関係の型です（破線は「推測」「一緒に出る集計」）。</p>
      </header>

      <section className="space-y-2 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900" aria-label="探す">
        <div className="flex flex-wrap items-center gap-2">
          <input type="search" aria-label="Things を探す" data-testid="map-search" placeholder="語で探す（例: Qwen）" value={q} onChange={(e) => setQ(e.target.value)} className={`${field} min-w-0 flex-1`} />
          <div role="radiogroup" aria-label="探し方" className="flex gap-1" data-testid="map-mode">
            {([["highlight", "強調"], ["filter", "絞る"]] as const).map(([id, label]) => (
              <button key={id} type="button" role="radio" aria-checked={mode === id} data-testid={`mode-${id}`} onClick={() => setMode(id)}
                className={`min-h-9 rounded-lg border px-3 text-xs ${mode === id ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{label}</button>
            ))}
          </div>
        </div>
        {q.trim() && <p className="num text-[11px] text-zinc-500" data-testid="map-hits" role="status">{found.isFetching ? "探しています…" : `一致 ${found.data?.total ?? 0} 件${(found.data?.total ?? 0) > 100 ? "（上位 100 件を表示）" : ""}${mode === "filter" ? "・一致と近傍だけを表示" : "・一致を強調"}`}</p>}
        {q.trim() && results.length > 0 && (
          <ul className="flex flex-wrap gap-1.5" data-testid="map-results">
            {results.map((r) => <li key={r.id}><button type="button" data-testid="map-result" onClick={() => void enterLocal(r.id, 1)} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{r.name}<span className="num text-zinc-500">{r.article_count}</span></button></li>)}
          </ul>
        )}
        {view === "overview" && (o?.communities.length ?? 0) > 0 && (
          <div className="flex items-center gap-1.5 overflow-x-auto pb-1" data-testid="map-groups"><span className="shrink-0 whitespace-nowrap text-[11px] text-zinc-500">まとまり:</span>
            {o!.communities.slice(0, 14).map((c) => (
              <button key={c.id} type="button" data-testid="map-group" aria-pressed={group === c.id} onClick={() => void openGroup(c.id)}
                className={`inline-flex min-h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 text-xs hover:border-accent-600 ${group === c.id ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{c.name}<span className="num text-zinc-500">{c.size}</span></button>
            ))}
            <Link to="/moc" className="min-h-9 shrink-0 whitespace-nowrap content-center px-2 text-xs text-accent-700 underline dark:text-accent-300">主題の入口へ</Link>
          </div>
        )}
        {view === "local" && (
          <div className="flex flex-wrap items-center gap-3" data-testid="map-local">
            <button type="button" data-testid="map-back" onClick={backToOverview} className={ghost}>全体へ戻る</button>
            <label className="flex items-center gap-2 text-xs">深さ <input type="range" min={1} max={3} step={1} value={depth} data-testid="map-depth" aria-label="ローカルの深さ" disabled={busy}
              onChange={(e) => { const d = Number(e.target.value); void enterLocal(localRoot.current, d); }} className="w-28" /><span className="num w-4">{depth}</span></label>
          </div>
        )}
      </section>

      <details className="rounded-2xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900" data-testid="map-filters-box">
      <summary className="flex min-h-11 cursor-pointer select-none items-center px-3 text-sm font-semibold" data-testid="map-filters-toggle">絞り込み・凡例（種類・確かさ・期間・関係の型・孤立した点）</summary>
      <section className="space-y-2 p-3 pt-0" aria-label="絞り込みと凡例" data-testid="map-filters">
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="text-[11px] text-zinc-500">期間（線の最後の記事）<select aria-label="期間" value={period} onChange={(e) => setPeriod(Number(e.target.value))} className={`${field} mt-0.5 w-full`}>{PERIODS.map(([n, l]) => <option key={n} value={n}>{l}</option>)}</select></label>
          <label className="text-[11px] text-zinc-500">Things の種類<select aria-label="Things の種類" value={kind} onChange={(e) => setKind(e.target.value)} className={`${field} mt-0.5 w-full`}><option value="">すべて</option>{(Object.keys(ENTITY_TYPE_LABEL) as EntityType[]).map((t) => <option key={t} value={t}>{ENTITY_TYPE_LABEL[t]}</option>)}</select></label>
          <label className="text-[11px] text-zinc-500">確かさ<select aria-label="確かさ" value={minConf} onChange={(e) => setMinConf(Number(e.target.value))} className={`${field} mt-0.5 w-full`}>{CONFS.map(([n, l]) => <option key={n} value={n}>{l}</option>)}</select></label>
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="関係の型" data-testid="map-legend">
          {REL_TYPES.map((t) => (
            <button key={t} type="button" aria-pressed={active.has(t)} data-testid={`legend-${t}`} onClick={() => toggle(t)}
              className={`inline-flex min-h-9 items-center gap-1.5 rounded-lg border px-2.5 text-xs ${active.has(t) ? "border-zinc-400 font-semibold dark:border-zinc-500" : "border-zinc-200 text-zinc-400 line-through dark:border-zinc-800"}`}>
              <span aria-hidden className="inline-block h-0.5 w-4" style={{ backgroundColor: REL_COLOR[t], borderTop: t === "co_occurs" ? "2px dashed" : undefined }} />{REL_LABEL[t]}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="map-node-legend" aria-label="点の色（種類）">
          {used.map((t) => <span key={t} className="inline-flex items-center gap-1 text-[11px] text-zinc-600 dark:text-zinc-300"><span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: NODE_COLOR[t] }} />{ENTITY_TYPE_LABEL[t] ?? t}</span>)}
          <label className="ml-auto inline-flex min-h-9 items-center gap-2 text-xs"><input type="checkbox" data-testid="map-iso" checked={showIso} onChange={(e) => setShowIso(e.target.checked)} />孤立した点も表示（{o?.isolated.length ?? 0}）</label>
        </div>
      </section>
      </details>

      <div className="relative rounded-2xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
        <div ref={box} data-testid="map-canvas" data-nodes={counts.n} data-edges={counts.e} data-view={view} data-big={BIG_LABELS} data-hits={hits.size} data-mode={mode} role="img" aria-label={`知識の地図（点 ${counts.n}・線 ${counts.e}）`} className="h-[58vh] min-h-80 w-full touch-none" />
        {overview.isLoading && <p className="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-zinc-500" data-testid="map-loading">全体の地図を読み込み中…</p>}
        {o && o.nodes.length === 0 && <p className="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-zinc-500" data-testid="map-empty">まだ関係がありません。記事から Things と関係が取り出されると、ここに全体の地図が出ます。</p>}
        {overview.isError && <p role="alert" className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm" data-testid="map-error">全体の地図を読み込めませんでした。</p>}
        {busy && <span role="status" className="absolute right-3 top-3 rounded-md bg-zinc-100 px-2 py-1 text-[11px] dark:bg-zinc-800">読み込み中…</span>}
      </div>
      <p className="num text-xs text-zinc-500" data-testid="map-count" role="status">点 {counts.n} · 線 {counts.e}{counts.hidden ? `（絞り込みで ${counts.hidden} 本を隠しています）` : ""}
        {view === "overview" && o ? ` · 関係のある Things ${o.totals.with_relations} 件のうち、主要な ${o.totals.shown_nodes} 点${o.truncated ? "（上限で切っています。まとまり・検索で中を開けます）" : ""}` : ""}</p>
      {msg && <p role="status" className="text-xs text-amber-700 dark:text-amber-400" data-testid="map-msg">{msg}</p>}

      {selected && (
        <div className="space-y-2 rounded-2xl border border-accent-600 bg-white p-3 dark:bg-zinc-900" data-testid="map-selected">
          <p className="break-words text-sm font-semibold">{selected.name} <span className="ml-1 rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">{ENTITY_TYPE_LABEL[selected.type] ?? selected.type}</span>
            <span className="num ml-2 text-[11px] font-normal text-zinc-500">記事 {selected.article_count} 件 · 関係 {selected.degree}</span></p>
          <div className="flex flex-wrap gap-2">
            <Link to={`/entities/${selected.id}`} data-testid="map-open-card" className={`${primary} inline-flex items-center`}>知識カードを開く</Link>
            <button type="button" data-testid="map-local-open" disabled={busy} onClick={() => void enterLocal(selected.id, view === "local" ? depth : 1)} className={ghost}>ローカルで見る</button>
            <button type="button" data-testid="map-expand" disabled={busy} onClick={() => void expand(selected.id)} className={ghost}>周りを広げる</button>
          </div>
        </div>
      )}

      <ExportPanel ids={() => [...nodes.current.keys()]} hasMap={counts.n > 0} />
    </div>
  );
}

function ExportPanel({ ids, hasMap }: { ids: () => number[]; hasMap: boolean }) {
  const [scope, setScope] = useState<"map" | "range">("range");
  const [kind, setKind] = useState("");
  const [days, setDays] = useState(0);
  const [minArticles, setMin] = useState(3);
  const [conf, setConf] = useState(false);
  const [notes, setNotes] = useState(true);
  const [state, setState] = useState<{ busy: boolean; text: string; err: boolean }>({ busy: false, text: "", err: false });
  const run = async () => {
    setState({ busy: true, text: "", err: false });
    try {
      const { blob, files } = await exportZip({ ids: scope === "map" ? ids() : [], types: kind ? [kind] : [], since: sinceIso(days), min_articles: scope === "map" ? 0 : minArticles, limit: 500, include_notes: notes, confidential: conf });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "knowledge.zip";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      setState({ busy: false, text: `${files} 件の物を書き出しました（Things ごとの .md と _MOC/・README.md を zip にまとめています）。`, err: false });
    } catch {
      setState({ busy: false, text: "書き出せませんでした。", err: true });
    }
  };
  return (
    <Card id="map-export" title="Obsidian へ書き出す（脳の vault）" hint="押したときだけ、範囲の物を 1 つずつの .md（front matter・[[関係する物]]・根拠の URL つき）にして zip で受け取ります。常には書き出しません。">
      <div className="grid gap-2 sm:grid-cols-2" data-testid="export-form">
        <fieldset className="space-y-1 text-xs"><legend className="text-[11px] text-zinc-500">範囲</legend>
          <label className="flex min-h-9 items-center gap-2"><input type="radio" name="scope" checked={scope === "range"} onChange={() => setScope("range")} />条件で選ぶ</label>
          <label className="flex min-h-9 items-center gap-2"><input type="radio" name="scope" checked={scope === "map"} disabled={!hasMap} onChange={() => setScope("map")} />いま地図にある物</label>
        </fieldset>
        {scope === "range" && (
          <div className="grid gap-2">
            <select aria-label="書き出す種類" value={kind} onChange={(e) => setKind(e.target.value)} className={field}><option value="">すべての種類</option>{(Object.keys(ENTITY_TYPE_LABEL) as EntityType[]).map((t) => <option key={t} value={t}>{ENTITY_TYPE_LABEL[t]}</option>)}</select>
            <select aria-label="書き出す期間" value={days} onChange={(e) => setDays(Number(e.target.value))} className={field}>{PERIODS.map(([n, l]) => <option key={n} value={n}>{n ? `最近 ${l}に出た物` : "全期間"}</option>)}</select>
            <select aria-label="記事の数" value={minArticles} onChange={(e) => setMin(Number(e.target.value))} className={field}>{[[0, "記事の数を問わない"], [3, "記事 3 件以上"], [10, "記事 10 件以上"]].map(([n, l]) => <option key={n} value={n}>{l}</option>)}</select>
          </div>
        )}
      </div>
      <label className="flex min-h-9 items-center gap-2 text-xs"><input type="checkbox" checked={notes} onChange={(e) => setNotes(e.target.checked)} />自分のメモも含める</label>
      <label className="flex min-h-9 items-center gap-2 text-xs"><input type="checkbox" data-testid="export-confidential" checked={conf} onChange={(e) => setConf(e.target.checked)} />機密の区分（confidential）も含める（既定は公開だけ）</label>
      <button type="button" data-testid="export-button" disabled={state.busy} onClick={() => void run()} className={primary}>{state.busy ? "書き出し中…" : "zip で書き出す"}</button>
      {state.text && <p role={state.err ? "alert" : "status"} data-testid="export-state" className="text-xs">{state.text}</p>}
    </Card>
  );
}
