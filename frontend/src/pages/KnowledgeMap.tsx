import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type Graph from "graphology";
import type Sigma from "sigma";
import { REL_COLOR, REL_LABEL, REL_TYPES, exportZip, neighbors, type GraphEdge, type GraphNode, type RelType } from "../api/kg";
import { ENTITY_TYPE_LABEL, useEntities, type EntityType } from "../api/news";
import { communityMembers, useOverview, type OverviewNode } from "../api/things";
import { CLUSTER_DARK, CLUSTER_LIGHT, NO_CLUSTER, fallbackImportance, layoutOverview, placeNear, radiusOf, scatterOutside, sizeCap, type Pos } from "../features/knowledge/mapLayout";
import { Card, field, ghost, primary } from "../features/news/settingsUi";

/** 知識の地図（/map）。Obsidian のグラフビューのように、**最初に全体**（サーバーが返す主要な 1,500 点と、まとまりの代表）を出し、近づく（拡大・まとまりを押す）と中を開く。
 *  - 配置は重力のように: 夜にサーバーが ForceAtlas2 で計算して保存した座標（x・y）で出す（毎回形が変わらない）。大きな点は離れ、小さな点は重い点の周りに集まる。
 *    保存が無い（まだ夜の計算が済んでいない）ときは、まとまりごとの塊（mapLayout.layoutOverview）で出す。あとから足す点は、つながる点の近くへ置く。
 *  - 銀河の見た目（2026-11-25）: 点の半径は 2〜9（重要度を平方根で縮め上位 5% で頭打ち）。色＝既定はまとまりごと（Nebula のパレット）・設定で種類ごと。背景はダークが深い紺・ライトが白に近い灰。
 *    線は 1 色の灰青・半透明で細く、背景の canvas に描く（sigma は透明度を無視するため）。触れた点（ホバー・タップ）と選んだ点は、つながる点と線を明るく（線は関係の型の色）・ほかを薄く。
 *    重要度の高い点はやわらかく光る（canvas・加算の合成）。左上の「設定」の枠: 表示の型（銀河・関係を読む・自分で保存）・表示・色・絞り込み・力（変えると画面で短く再配置）。型・設定は端末の localStorage。
 *  - 検索: 語を入れると「強調」（一致は大きく・ほかは薄く）か「絞る」（一致と近傍だけ）を選べ、一致した点へ寄る。全体に無い一致は近傍ごと足す。
 *  - ローカル: 選んだ点の周りを、深さ 1〜3 のつまみで（点を押す → 「ローカルで見る」）。小さな絵なので、画面側で ForceAtlas2 を回して置く。「全体へ戻る」で最初の絵へ。
 *  - 名前の出し方: 重要度の大きい 24 点はいつも、ほかは大きな点から順に、拡大するほど細かく（sigma の名前の間引き）。
 *  描画は sigma.js（WebGL）＋ graphology を地図を開いたときに遅延読み込み（版固定）。 */
const PERIODS: [number, string][] = [[0, "全期間"], [30, "30 日"], [90, "90 日"], [365, "1 年"]];
const CONFS: [number, string][] = [[0, "すべて"], [0.3, "0.3 以上"], [0.5, "0.5 以上"], [0.7, "0.7 以上"]];
/** 種類ごとの色。落ち着いた色合い（彩度を少し下げる）。ダークは明るめ・ライトは少し濃く。 */
const NODE_COLOR_DARK: Record<EntityType, string> = {
  model: "#7aa2f7", software: "#4fd1c5", org: "#e0a458", person: "#f28fb8", tech: "#b79cff", product: "#5ec8e0", game: "#a3d977", policy: "#f08080", place: "#9ca3af", other: "#8b93a7",
};
const NODE_COLOR: Record<EntityType, string> = {
  model: "#3b64d9", software: "#0f8f84", org: "#b0741f", person: "#c2477f", tech: "#6d4fd1", product: "#0c8aa6", game: "#5a9a1f", policy: "#cc4a4a", place: "#5b6170", other: "#6b7285",
};
const LOCAL_LIMIT = [200, 400, 700];
const BIG_LABELS = 16;      // 名前をいつも出す点の上限（「名前の量」で増減）
const SELECT_COLOR = "#f59e0b";

type Mode = "highlight" | "filter";
/** 表示の設定（左上の枠）。型（プリセット）として端末に保存する。 */
interface MapCfg { nodeScale: number; edgeAlpha: number; labels: number; color: "cluster" | "kind"; glow: boolean; center: number; repel: number; linkDist: number }
const GALAXY: MapCfg = { nodeScale: 1, edgeAlpha: 0.22, labels: 0.5, color: "cluster", glow: true, center: 0.3, repel: 4, linkDist: 1 };
const READING: MapCfg = { nodeScale: 1.15, edgeAlpha: 0.35, labels: 0.85, color: "kind", glow: false, center: 0.3, repel: 2.5, linkDist: 1 };
const BUILTIN: [string, MapCfg][] = [["銀河", GALAXY], ["関係を読む", READING]];
const LS_CFG = "newshub.map.cfg", LS_PRESETS = "newshub.map.presets";
const loadJson = <T,>(key: string, fallback: T): T => { try { const v = localStorage.getItem(key); return v ? (JSON.parse(v) as T) : fallback; } catch { return fallback; } };
const saveJson = (key: string, v: unknown) => { try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* 保存できなくても動く */ } };
const clampNum = (v: unknown, lo: number, hi: number, d: number) => (typeof v === "number" && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d);
const sanitizeCfg = (c: Partial<MapCfg> | null | undefined): MapCfg => ({ nodeScale: clampNum(c?.nodeScale, 0.5, 2.5, 1), edgeAlpha: clampNum(c?.edgeAlpha, 0.03, 0.6, GALAXY.edgeAlpha), labels: clampNum(c?.labels, 0, 1, 0.5),
  color: c?.color === "kind" ? "kind" : "cluster", glow: c?.glow !== false, center: clampNum(c?.center, 0.05, 2, 0.3), repel: clampNum(c?.repel, 0.5, 12, 4), linkDist: clampNum(c?.linkDist, 0.5, 3, 1) });
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

type Theme = { label: string; dimNode: string; dimEdge: number; bg: string; edge: string; dark: boolean };
/** sigma の線は rgba の透明度を無視して不透明に塗る（実測）。透明に見せるには、背景と混ぜた不透明の色を渡す。 */
const mixOnBg = (fg: string, a: number, bg: string) => {
  const f = fg.startsWith("#") ? [parseInt(fg.slice(1, 3), 16), parseInt(fg.slice(3, 5), 16), parseInt(fg.slice(5, 7), 16)] : fg.split(",").map(Number);
  const b = [parseInt(bg.slice(1, 3), 16), parseInt(bg.slice(3, 5), 16), parseInt(bg.slice(5, 7), 16)];
  return `#${f.map((v, i) => Math.round(b[i] + (v - b[i]) * Math.max(0, Math.min(1, a))).toString(16).padStart(2, "0")).join("")}`;
};
/** 背景: ダークは深い紺・ライトは白に近い灰。線は 1 色の灰青（関係の型の色は、触れた点・選んだ点の線だけ）。名前は小さく淡く。 */
const themeOf = (dark: boolean): Theme => (dark
  ? { label: "#aeb4d6", dimNode: "#1a1c3a", dimEdge: 0.04, bg: "#0b0b1e", edge: "150,165,215", dark }
  : { label: "#4a5070", dimNode: "#e1e3ee", dimEdge: 0.06, bg: "#f3f4f8", edge: "90,100,135", dark });

function addEdge(g: Graph, e: EdgeRec, local: boolean) {
  const solid = REL_COLOR[e.type] ?? REL_COLOR.other;
  const weak = e.status === "inferred" || e.status === "statistical";      // 推測・集計は薄く（sigma は破線を持たないので、色の濃さで区別する）
  const attrs = { size: 0.55 + Math.min(e.evidence_count, 12) * 0.04, color: rgba(solid, 0.5), baseColor: solid, weak, local, type: local && e.directed ? "arrow" : "line",
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
  const nodeSize = (imp: number) => radiusOf(imp, sizing.current.cap) * sizing.current.unit;
  const pos = useRef(new Map<number, Pos>());
  const centers = useRef(new Map<number, Pos & { r: number }>());
  const home = useRef<{ nodes: NodeRec[]; edges: EdgeRec[]; pos: Map<number, Pos>; world: boolean } | null>(null);      // 全体の絵（ローカルから戻るとき）
  const nodeFlags = useRef(new Map<string, NodeFlag>());
  const edgeFlags = useRef(new Map<string, EdgeFlag>());
  const hlEdges = useRef(new Set<string>());
  const selId = useRef<string | null>(null);
  const sizing = useRef({ world: false, unit: 1, cap: 1, edgeK: 1 });      // 点の大きさの基準（半径は世界の単位で 2〜9。画面の幅で倍率 unit をかける。cap＝重要度の上位 5% の値）
  const [cfg, setCfg] = useState<MapCfg>(() => sanitizeCfg(loadJson<Partial<MapCfg> | null>(LS_CFG, null)));
  const cfgRef = useRef(cfg);
  cfgRef.current = cfg;
  const [presets, setPresets] = useState<[string, MapCfg][]>(() => loadJson<[string, Partial<MapCfg>][]>(LS_PRESETS, []).filter((p) => Array.isArray(p) && typeof p[0] === "string").map(([n, c]) => [n, sanitizeCfg(c)]));
  const [presetName, setPresetName] = useState("");
  const [panel, setPanel] = useState(false);
  const rank = useRef(new Map<number, number>());      // まとまり → 色の順番（大きいまとまりから）
  const hover = useRef<string | null>(null);
  const focusCache = useRef<{ key: string | null; nodes: Set<string>; edges: Set<string>; lab: Set<string> }>({ key: null, nodes: new Set(), edges: new Set(), lab: new Set() });
  const baseMeta = useRef({ pos: new Map<string, Pos>() });      // 夜の配置（「配置を戻す」用）
  const glowKeys = useRef<string[]>([]);
  const glowCanvas = useRef<HTMLCanvasElement | null>(null);
  const perf = useRef({ frame: 0, glow: 0, frameMax: 0, glowMax: 0, n: 0, edges: 0 });
  const relayoutTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  /** 点の色（まとまりごと／種類ごと）。まとまりの色は、大きいまとまりから順に Nebula のパレットを割り当てる。 */
  const colorOf = (a: Record<string, unknown>) => {
    const dark = theme.current.dark;
    if (a.iso) return dark ? NO_CLUSTER.dark : NO_CLUSTER.light;
    if (cfgRef.current.color === "kind") return (dark ? NODE_COLOR_DARK : NODE_COLOR)[a.kind as EntityType] ?? (dark ? NODE_COLOR_DARK.other : NODE_COLOR.other);
    const c = (a.cc as number | undefined) ?? (a.community as number);
    if (!c) return dark ? NO_CLUSTER.dark : NO_CLUSTER.light;
    const pal = dark ? CLUSTER_DARK : CLUSTER_LIGHT;
    return pal[(rank.current.get(c) ?? Math.abs(c) * 5) % pal.length];      // 大きいまとまりは順に別の色・小さなまとまりは番号から
  };

  /** 触れた点（ホバー）か選んだ点の、つながる点と線。ほかを薄くするのに使う。 */
  const getFocus = () => {
    const key = hover.current ?? selId.current;
    const fc = focusCache.current;
    const g = graphRef.current;
    if (fc.key === key) return fc;
    const next = { key, nodes: new Set<string>(), edges: new Set<string>(), lab: new Set<string>() };
    if (key && g && g.hasNode(key)) {
      next.nodes.add(key);
      g.forEachNeighbor(key, (nb) => { next.nodes.add(nb); });
      g.forEachEdge(key, (e) => { next.edges.add(e); });
      next.lab = new Set([key, ...[...next.nodes].filter((k) => k !== key).sort((x, y) => (g.getNodeAttribute(y, "imp") as number) - (g.getNodeAttribute(x, "imp") as number)).slice(0, 3)]);      // 名前は、触れた点と重要なつながり 3 点だけ（重ならない）
    }
    focusCache.current = next;
    return next;
  };

  /** いつも名前を出す点（「名前の量」の分だけ重要度の上位。全体のときだけ）と、光らせる点（重要度の上位 10%・最大 100）を決める。 */
  const markBig = (local: boolean) => {
    const g = graphRef.current;
    if (!g) return;
    const arr: [string, number][] = [];
    g.forEachNode((k, a) => { if (!a.iso) arr.push([k, a.imp as number]); });
    arr.sort((x, y) => y[1] - x[1] || Number(x[0]) - Number(y[0]));
    const big = new Set(local ? [] : arr.slice(0, Math.round(BIG_LABELS * cfgRef.current.labels)).map(([k]) => k));
    g.forEachNode((k) => g.setNodeAttribute(k, "forceLabel", big.has(k)));
    glowKeys.current = arr.slice(0, Math.min(100, Math.ceil(arr.length * 0.1))).filter(([, v]) => v > 0).map(([k]) => k);
  };

  /** 光（グロー）: 重要度の高い点のうしろに、やわらかい円を 2D の canvas で重ねる（sigma の描画の下）。ダークは加算の合成・ライトは薄い色の暈し。1 コマの時間を perf に残す。 */
  const drawGlow = () => {
    const sg = sigmaRef.current, g = graphRef.current, cv = glowCanvas.current, host = box.current;
    if (!sg || !g || !cv || !host) return;
    const t0 = performance.now();
    const dpr = window.devicePixelRatio || 1;
    const w = host.clientWidth, h = host.clientHeight;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const c = cfgRef.current;
    const fo = getFocus();
    // 線（全体）: 1 色の灰青・半透明・細い。触れた点・選んだ点がある間は、つながらない線をさらに薄く（つながる線は sigma が型の色で描く）
    const vp = new Map<string, [number, number]>();
    const at = (k: string): [number, number] => { let v = vp.get(k); if (!v) { const na = g.getNodeAttributes(k); const p = sg.graphToViewport({ x: na.x as number, y: na.y as number }); v = [p.x, p.y]; vp.set(k, v); } return v; };
    const norm = new Path2D(), faint = new Path2D();
    let drawn = 0;
    g.forEachEdge((key, a, s, t) => {
      if (a.local) return;
      const f = edgeFlags.current.get(key);
      if (f?.off || nodeFlags.current.get(s)?.off || nodeFlags.current.get(t)?.off) return;
      if (fo.key && fo.edges.has(key)) return;
      const path = fo.key || f?.dim ? faint : norm;
      const [x1, y1] = at(s), [x2, y2] = at(t);
      path.moveTo(x1, y1);
      path.lineTo(x2, y2);
      drawn += 1;
    });
    perf.current.edges = drawn;
    ctx.globalCompositeOperation = "source-over";
    ctx.lineWidth = 0.8;
    ctx.strokeStyle = `rgba(${theme.current.edge},${c.edgeAlpha})`;
    ctx.stroke(norm);
    ctx.strokeStyle = `rgba(${theme.current.edge},${theme.current.dimEdge})`;
    ctx.stroke(faint);
    if (c.glow) {
      const dark = theme.current.dark;
      ctx.globalCompositeOperation = dark ? "lighter" : "source-over";
      for (const k of glowKeys.current) {
        if (!g.hasNode(k) || nodeFlags.current.get(k)?.off) continue;
        if (fo.key && !fo.nodes.has(k)) continue;
        const a = g.getNodeAttributes(k);
        const p = sg.graphToViewport({ x: a.x as number, y: a.y as number });
        if (p.x < -60 || p.y < -60 || p.x > w + 60 || p.y > h + 60) continue;
        const r = sg.scaleSize((a.size as number) * c.nodeScale) * 3.2 + 5;
        const gr = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
        const col = colorOf(a);
        gr.addColorStop(0, rgba(col, dark ? 0.26 : 0.2));
        gr.addColorStop(1, rgba(col, 0));
        ctx.fillStyle = gr;
        ctx.fillRect(p.x - r, p.y - r, 2 * r, 2 * r);
      }
    }
    const dt = performance.now() - t0;
    perf.current.glow = dt;
    perf.current.glowMax = Math.max(perf.current.glowMax, dt);
  };

  /** 力（中心・反発・リンクの距離）を変えたら、いまの位置から画面で短く再配置する（夜の配置は変えない。「配置を戻す」で夜の配置へ）。 */
  const relayout = useCallback(async () => {
    const g = graphRef.current, sg = sigmaRef.current;
    if (!g || !sg || g.order < 2) return;
    const { default: fa2 } = await import("graphology-layout-forceatlas2");
    const c = cfgRef.current;
    fa2.assign(g, { iterations: g.order > 600 ? 40 : 70, settings: { ...fa2.inferSettings(g), linLogMode: false, outboundAttractionDistribution: true, gravity: c.center, scalingRatio: c.repel, edgeWeightInfluence: 1 / c.linkDist, slowDown: 2, barnesHutOptimize: g.order > 200 }, getEdgeWeight: "weight" });
    pos.current = new Map(g.mapNodes((k, a) => [Number(k), { x: a.x as number, y: a.y as number }] as [number, Pos]));
    sg.refresh();
  }, []);
  const scheduleRelayout = () => { if (relayoutTimer.current) clearTimeout(relayoutTimer.current); relayoutTimer.current = setTimeout(() => { void relayout(); }, 350); };
  const resetLayout = () => {
    const g = graphRef.current;
    if (!g) return;
    for (const [k, p] of baseMeta.current.pos) if (g.hasNode(k)) { g.setNodeAttribute(k, "x", p.x); g.setNodeAttribute(k, "y", p.y); }
    pos.current = new Map([...baseMeta.current.pos].map(([k, p]) => [Number(k), p]));
    sigmaRef.current?.refresh();
  };

  /** 絵を丸ごと入れ替える（全体 ⇄ ローカル）。local のときは、画面側で ForceAtlas2 を回して置く（点が少ないので短い）。 */
  const draw = useCallback(async (ns: NodeRec[], es: EdgeRec[], where: Map<number, Pos> | null, local: boolean, world: boolean) => {
    const g = graphRef.current, sg = sigmaRef.current;
    if (!g || !sg) return;
    nodes.current = new Map(ns.map((n) => [n.id, n]));
    edges.current = new Set();
    const maxDeg = Math.max(0, ...ns.map((n) => n.degree));
    sizing.current = { world, unit: 1, cap: sizeCap(ns.filter((n) => !n.isolated).map((n) => n.importance ?? fallbackImportance(n.degree, maxDeg))), edgeK: 1 };
    g.clear();
    focusCache.current = { key: "#", nodes: new Set(), edges: new Set(), lab: new Set() };
    selId.current = null;
    hlEdges.current = new Set();
    const order = [...ns].sort((a, b) => b.degree - a.degree || a.id - b.id);
    const seed = new Map(order.map((n, i) => [n.id, { x: Math.sqrt(i + 0.5) * 30 * Math.cos(i * 2.39996), y: Math.sqrt(i + 0.5) * 30 * Math.sin(i * 2.39996) }]));
    for (const n of ns) {
      const p = where?.get(n.id) ?? seed.get(n.id)!;
      const imp = n.importance ?? fallbackImportance(n.degree, maxDeg);
      g.addNode(String(n.id), { x: p.x, y: p.y, imp: n.isolated ? 0 : imp, size: 1, label: n.name, color: "#888", kind: n.type, community: n.community, iso: !!n.isolated, degree: n.degree, forceLabel: false });
    }
    for (const e of es) {
      if (!nodes.current.has(e.source) || !nodes.current.has(e.target) || edges.current.has(e.id)) continue;
      edges.current.add(e.id);
      addEdge(g, e, local);
    }
    g.forEachNode((k, a) => {      // まとまりに入らなかった点は、つながる点のまとまりの色（無ければ、つながる最も関係の多い点の番号から）。すべての点に色がつく
      if (a.community) return;
      let best = -(Number(k)), bd = a.degree as number;
      g.forEachNeighbor(k, (nb, na) => { if (na.community) { if (best < 0 || (na.degree as number) > bd) { best = na.community as number; bd = na.degree as number; } } else if (best < 0 && (na.degree as number) > bd) { best = -Number(nb); bd = na.degree as number; } });
      g.setNodeAttribute(k, "cc", best);
    });
    if (local) {
      const { default: fa2 } = await import("graphology-layout-forceatlas2");
      const n = g.order;
      fa2.assign(g, { iterations: n > 300 ? 120 : 250, settings: { ...fa2.inferSettings(g), linLogMode: true, outboundAttractionDistribution: true, gravity: 1, scalingRatio: 6, slowDown: 2, barnesHutOptimize: n > 200 }, getEdgeWeight: "weight" });
      pos.current = new Map(g.mapNodes((k, a) => [Number(k), { x: a.x as number, y: a.y as number }] as [number, Pos]));
    }
    // 点の大きさ: 半径は世界の単位（保存した配置は丸が重ならないよう夜に離した距離）。幅が狭い画面（スマホ）では、全体が入ったとき小さくなりすぎないよう倍率をかける。
    // ローカル・保存の無い配置は、座標の広がりを夜の配置の広がり（約 1,200）に合わせて同じ見た目にする
    const xs = g.mapNodes((_k, a) => a.x as number), ys = g.mapNodes((_k, a) => a.y as number);
    const extent = Math.max(1, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
    const w = box.current?.clientWidth ?? 800;
    const viewK = w < 500 ? 2 : w < 900 ? 1.4 : 1.0;
    const unit = (world ? 1 : extent / 1200) * viewK;
    sizing.current = { ...sizing.current, world, unit, edgeK: unit * 0.5 };
    g.forEachNode((k, a) => g.setNodeAttribute(k, "size", nodeSize(a.imp as number)));
    baseMeta.current = { pos: new Map(g.mapNodes((k, a) => [k, { x: a.x as number, y: a.y as number }] as [string, Pos])) };
    markBig(local);
    // 最初に見える範囲: 中心から 97% の点が入る範囲（遠くに散らばった少数の点で、全体が小さくならない。外側へは動かして見に行ける）
    const live = g.filterNodes((_k, a) => !a.iso);
    const rs = live.map((k) => Math.hypot(g.getNodeAttribute(k, "x") as number, g.getNodeAttribute(k, "y") as number)).sort((a, b) => a - b);
    const lim = rs.length > 30 ? rs[Math.floor(rs.length * 0.97)] : Infinity;
    const inner = live.filter((k) => Math.hypot(g.getNodeAttribute(k, "x") as number, g.getNodeAttribute(k, "y") as number) <= lim);
    if (world && inner.length > 30) {
      const ix = inner.map((k) => g.getNodeAttribute(k, "x") as number), iy = inner.map((k) => g.getNodeAttribute(k, "y") as number);
      const pad = 20;
      sg.setCustomBBox({ x: [Math.min(...ix) - pad, Math.max(...ix) + pad], y: [Math.min(...iy) - pad, Math.max(...iy) + pad] });
    } else sg.setCustomBBox(null);
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
        renderEdgeLabels: false, labelRenderedSizeThreshold: 9 - 7 * cfgRef.current.labels, labelDensity: 0.4 + 0.6 * cfgRef.current.labels, labelGridCellSize: 120, labelSize: 11, labelFont: "system-ui, sans-serif", labelColor: { color: theme.current.label },
        zIndex: true, minCameraRatio: 0.02, maxCameraRatio: 3, itemSizesReference: "positions", zoomToSizeRatioFunction: (r) => r, defaultEdgeType: "line", enableEdgeEvents: false,
        nodeReducer: (key, a) => {
          const f = nodeFlags.current.get(key);
          const out = { ...a };
          if (f?.off) { out.hidden = true; return out; }
          out.color = colorOf(a);
          out.size = (a.size as number) * cfgRef.current.nodeScale;
          if (f?.dim) { out.color = theme.current.dimNode; out.label = ""; out.forceLabel = false; out.zIndex = 0; return out; }
          const fo = getFocus();
          if (fo.key) {      // 触れた点・選んだ点: つながる点を明るく、ほかを薄く
            if (!fo.nodes.has(key)) { out.color = theme.current.dimNode; out.label = ""; out.forceLabel = false; out.zIndex = 0; }
            else { out.zIndex = 2; if (fo.lab.has(key)) out.forceLabel = true; else out.label = ""; }
          }
          if (f?.hit) { out.size = out.size * 1.5; out.zIndex = 2; out.forceLabel = true; }
          if (key === selId.current) { out.color = SELECT_COLOR; out.size = out.size * 1.5 + 1; out.zIndex = 3; out.forceLabel = true; }
          return out;
        },
        edgeReducer: (key, a) => {
          const f = edgeFlags.current.get(key);
          const out = { ...a };
          if (f?.off) { out.hidden = true; return out; }
          out.size = (a.size as number) * sizing.current.edgeK;
          const fo = getFocus();
          if (a.local) {      // ローカルの線（矢印あり）は sigma で描く
            if (f?.dim) { out.color = mixOnBg(theme.current.edge, theme.current.dimEdge, theme.current.bg); return out; }
            if (fo.key && fo.edges.has(key)) { out.color = mixOnBg(a.baseColor as string, 0.9, theme.current.bg); out.size = out.size * 1.8; out.zIndex = 1; return out; }
            out.color = mixOnBg(theme.current.edge, fo.key ? theme.current.dimEdge : Math.min(0.9, cfgRef.current.edgeAlpha * 3), theme.current.bg);
            return out;
          }
          // 全体の線: sigma は透明度を無視するので、既定の線は背景の canvas に半透明で描く（drawGlow）。sigma に残すのは、触れた点・選んだ点の線（関係の型の色）だけ
          if (fo.key && fo.edges.has(key)) { out.color = mixOnBg(a.baseColor as string, 0.9, theme.current.bg); out.size = out.size * 1.8; out.zIndex = 1; } else out.hidden = true;
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
        focusCache.current = { key: "#", nodes: new Set(), edges: new Set(), lab: new Set() };
        setSelected(key ? nodes.current.get(Number(key)) ?? null : null);
        sg!.refresh();
      };
      el.__tap = (id: number) => pick(String(id));
      (el as { __stat?: () => Record<string, number> }).__stat = () => {      // 試験用: 旗の数（一致・薄く・隠す）と、いつも名前を出す点の数
        const c = { hit: 0, dim: 0, off: 0, big: 0, sizeMax: 0, sizeMin: 1e9, edgeColors: 0, isoShown: 0, glowNodes: glowKeys.current.length };
        const er = sigmaRef.current?.getSetting("edgeReducer");
        if (er && !selId.current && !hover.current) { const cs = new Set<string>(); g.forEachEdge((k, a) => { const o = er(k, a); if (!o.hidden) cs.add(String(o.color)); }); if (perf.current.edges > 0) cs.add("canvas"); c.edgeColors = cs.size; }      // sigma が描く線の色の数＋背景の canvas（1 色）
        g.forEachNode((k, a) => { if (a.iso && !nodeFlags.current.get(k)?.off) c.isoShown += 1; });
        for (const f of nodeFlags.current.values()) { if (f.hit) c.hit += 1; if (f.dim) c.dim += 1; if (f.off) c.off += 1; }
        g.forEachNode((_k, a) => { if (a.forceLabel) c.big += 1; if (!a.iso) { c.sizeMax = Math.max(c.sizeMax, a.size as number); c.sizeMin = Math.min(c.sizeMin, a.size as number); } });
        return c;
      };
      const gl = document.createElement("canvas");
      gl.setAttribute("data-testid", "map-glow");
      gl.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none";
      box.current.insertBefore(gl, box.current.firstChild);
      glowCanvas.current = gl;
      let tFrame = 0;
      sg.on("beforeRender", () => { tFrame = performance.now(); });
      sg.on("afterRender", () => {
        drawGlow();
        const dt = performance.now() - tFrame;
        perf.current.frame = dt;
        perf.current.frameMax = Math.max(perf.current.frameMax, dt);
        perf.current.n += 1;
      });
      (el as { __perf?: () => Record<string, number> }).__perf = () => ({ ...perf.current });
      sg.on("clickNode", ({ node }) => pick(node));
      sg.on("clickStage", () => pick(null));
      sg.on("enterNode", ({ node }) => { if (box.current) box.current.style.cursor = "pointer"; hover.current = node; sg!.refresh(); });
      sg.on("leaveNode", () => { if (box.current) box.current.style.cursor = ""; hover.current = null; sg!.refresh(); });
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
  // 表示の設定: 端末に保存し、名前の量（しきい値・密度・いつも出す点の数）と色・線・光を反映する
  useEffect(() => {
    saveJson(LS_CFG, cfg);
    const sg = sigmaRef.current;
    if (!sg) return;
    sg.setSetting("labelRenderedSizeThreshold", 9 - 7 * cfg.labels);
    sg.setSetting("labelDensity", 0.4 + 0.6 * cfg.labels);
    markBig(marks.current.view === "local");
    sg.refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg, ready]);
  useEffect(() => { apply(); }, [period, kind, minConf, active, showIso, hits, mode, apply]);

  // 全体の地図: サーバーの応答が来て sigma の準備ができたら、保存した配置（無ければまとまりごとの塊）で描く
  const drawn = useRef(false);
  useEffect(() => {
    const o = overview.data;
    if (!ready || !o || drawn.current) return;
    drawn.current = true;
    rank.current = new Map([...o.communities].sort((a, b) => b.size - a.size).map((c, i) => [c.id, i]));
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
      for (const [id, p] of scatterOutside(iso.map((n) => n.id), reach)) all.set(id, p);
    } else {
      const lay = layoutOverview(ns);
      centers.current = lay.centers;
      for (const [id, p] of lay.pos) all.set(id, p);
      for (const [id, p] of scatterOutside(iso.map((n) => n.id), Math.max(...[...lay.centers.values()].map((c) => Math.hypot(c.x, c.y) + c.r), 200))) all.set(id, p);
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

  const applyCfg = (c: MapCfg) => {
    const cur = cfgRef.current;
    setCfg(c);
    if (c.center !== cur.center || c.repel !== cur.repel || c.linkDist !== cur.linkDist) scheduleRelayout();
  };
  const savePreset = () => {
    const name = presetName.trim();
    if (!name || BUILTIN.some(([n]) => n === name)) return;
    const next: [string, MapCfg][] = [...presets.filter(([n]) => n !== name), [name, cfg]].slice(-8) as [string, MapCfg][];
    setPresets(next);
    saveJson(LS_PRESETS, next);
    setPresetName("");
  };
  const toggle = (t: RelType) => setActive((s) => { const n = new Set(s); if (n.has(t)) n.delete(t); else n.add(t); return n; });
  const results = (found.data?.items ?? []).slice(0, 6);
  const o = overview.data;
  const used = useMemo(() => [...new Set([...nodes.current.values()].map((n) => n.type))], [counts.n, view]);

  return (
    <div className="mx-auto min-w-0 max-w-5xl space-y-3 p-2 md:p-6" data-testid="map-page">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">知識の地図</h1>
        <p className="text-xs leading-relaxed text-zinc-500">銀河のように、関係のある Things が広がります（配置は夜に計算して保存。大きな点は離れ、関係の薄い点は外側へ、点が 1 つだけつながる点はつながる点の周りに放射状に付きます）。色はまとまりごと（左上の「設定」で種類ごとにも）・点の大きさは重要度（検索・カードを開いた回数・最近の記事・関係の中心。上限あり）・光る点ほど重要です。点に触れる（押す）とつながる点と線が明るくなり、線の色は関係の型になります。語で探すと一致を強調するか絞り込み、まとまりや点を押すと中を開きます。</p>
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

      <div className="relative overflow-hidden rounded-2xl border border-zinc-200 dark:border-zinc-800" style={{ background: theme.current.bg }}>
        <div ref={box} data-testid="map-canvas" data-nodes={counts.n} data-edges={counts.e} data-view={view} data-big={BIG_LABELS} data-hits={hits.size} data-mode={mode} role="img" aria-label={`知識の地図（点 ${counts.n}・線 ${counts.e}）`} className="h-[58vh] min-h-80 w-full touch-none" />
        <div className="absolute left-2 top-2 z-10 max-w-[calc(100%-1rem)]">
          <button type="button" data-testid="map-settings-toggle" aria-expanded={panel} onClick={() => setPanel((v) => !v)}
            className="min-h-9 rounded-lg border border-zinc-300 bg-white/90 px-3 text-xs font-semibold text-zinc-700 backdrop-blur dark:border-zinc-700 dark:bg-zinc-900/90 dark:text-zinc-200">{panel ? "設定を閉じる" : "設定"}</button>
          {panel && (
            <div data-testid="map-settings" className="mt-1 max-h-[calc(58vh-3.5rem)] w-72 max-w-full space-y-3 overflow-y-auto rounded-xl border border-zinc-300 bg-white/95 p-3 text-xs text-zinc-800 backdrop-blur dark:border-zinc-700 dark:bg-zinc-900/95 dark:text-zinc-200">
              <fieldset className="space-y-1.5"><legend className="font-semibold">表示の型</legend>
                <div className="flex gap-1.5">
                  <select aria-label="表示の型" data-testid="map-preset" value="" onChange={(e) => { const hit = [...BUILTIN, ...presets].find(([n]) => n === e.target.value); if (hit) applyCfg(hit[1]); }} className={`${field} min-w-0 flex-1`}>
                    <option value="">型を選ぶ…</option>{[...BUILTIN, ...presets].map(([n]) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </div>
                <div className="flex gap-1.5">
                  <input aria-label="型の名前" data-testid="map-preset-name" value={presetName} maxLength={20} onChange={(e) => setPresetName(e.target.value)} placeholder="名前をつけて保存" className={`${field} min-w-0 flex-1`} />
                  <button type="button" data-testid="map-preset-save" disabled={!presetName.trim() || BUILTIN.some(([n]) => n === presetName.trim())} onClick={savePreset} className={ghost}>保存</button>
                </div>
                {presets.length > 0 && <div className="flex flex-wrap gap-1">{presets.map(([n]) => <button key={n} type="button" data-testid="map-preset-delete" onClick={() => { const next = presets.filter(([m]) => m !== n); setPresets(next); saveJson(LS_PRESETS, next); }} className="min-h-8 rounded-md border border-zinc-300 px-2 dark:border-zinc-700">{n} を消す</button>)}</div>}
                <p className="text-[11px] text-zinc-500">端末に保存します（ほかの端末へは行きません）。</p>
              </fieldset>
              <fieldset className="space-y-1.5"><legend className="font-semibold">表示</legend>
                <Slide label="点の大きさ" value={cfg.nodeScale} min={0.5} max={2.5} step={0.05} testid="map-set-size" onChange={(v) => setCfg((c) => ({ ...c, nodeScale: v }))} />
                <Slide label="線の透明度" value={cfg.edgeAlpha} min={0.03} max={0.6} step={0.01} testid="map-set-edge" onChange={(v) => setCfg((c) => ({ ...c, edgeAlpha: v }))} />
                <Slide label="名前の量" value={cfg.labels} min={0} max={1} step={0.05} testid="map-set-labels" onChange={(v) => setCfg((c) => ({ ...c, labels: v }))} />
                <label className="flex min-h-9 items-center gap-2"><input type="checkbox" data-testid="map-set-glow" checked={cfg.glow} onChange={(e) => setCfg((c) => ({ ...c, glow: e.target.checked }))} />重要な点を光らせる</label>
              </fieldset>
              <fieldset className="space-y-1.5"><legend className="font-semibold">色</legend>
                <div role="radiogroup" aria-label="色の分け方" className="flex gap-1.5">
                  {([["cluster", "まとまりごと"], ["kind", "種類ごと"]] as const).map(([id, label]) => (
                    <button key={id} type="button" role="radio" aria-checked={cfg.color === id} data-testid={`map-color-${id}`} onClick={() => setCfg((c) => ({ ...c, color: id }))}
                      className={`min-h-9 flex-1 rounded-lg border px-2 ${cfg.color === id ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{label}</button>
                  ))}
                </div>
              </fieldset>
              <fieldset className="space-y-1.5"><legend className="font-semibold">絞り込み・凡例</legend>
                <section className="space-y-2" aria-label="絞り込みと凡例" data-testid="map-filters">
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
          {used.map((t) => <span key={t} className="inline-flex items-center gap-1 text-[11px] text-zinc-600 dark:text-zinc-300"><span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: (dark ? NODE_COLOR_DARK : NODE_COLOR)[t] }} />{ENTITY_TYPE_LABEL[t] ?? t}</span>)}
          <label className="ml-auto inline-flex min-h-9 items-center gap-2 text-xs"><input type="checkbox" data-testid="map-iso" checked={showIso} onChange={(e) => setShowIso(e.target.checked)} />孤立した点も表示（{o?.isolated.length ?? 0}）</label>
        </div>
      </section>
              </fieldset>
              <fieldset className="space-y-1.5"><legend className="font-semibold">力（変えると、画面で短く並べ直します）</legend>
                <Slide label="中心へ引く力" value={cfg.center} min={0.05} max={2} step={0.05} testid="map-set-center" onChange={(v) => { setCfg((c) => ({ ...c, center: v })); scheduleRelayout(); }} />
                <Slide label="反発する力" value={cfg.repel} min={0.5} max={12} step={0.5} testid="map-set-repel" onChange={(v) => { setCfg((c) => ({ ...c, repel: v })); scheduleRelayout(); }} />
                <Slide label="リンクの距離" value={cfg.linkDist} min={0.5} max={3} step={0.1} testid="map-set-dist" onChange={(v) => { setCfg((c) => ({ ...c, linkDist: v })); scheduleRelayout(); }} />
                <button type="button" data-testid="map-set-reset" onClick={resetLayout} className={ghost}>夜の配置へ戻す</button>
              </fieldset>
            </div>
          )}
        </div>
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

function Slide({ label, value, min, max, step, onChange, testid }: { label: string; value: number; min: number; max: number; step: number; onChange: (v: number) => void; testid: string }) {
  return (
    <label className="flex items-center gap-2"><span className="w-20 shrink-0">{label}</span>
      <input type="range" min={min} max={max} step={step} value={value} data-testid={testid} aria-label={label} onChange={(e) => onChange(Number(e.target.value))} className="min-w-0 flex-1" />
      <span className="num w-10 shrink-0 text-right text-[11px] text-zinc-500">{value.toFixed(step < 0.1 ? 2 : 1)}</span></label>
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
