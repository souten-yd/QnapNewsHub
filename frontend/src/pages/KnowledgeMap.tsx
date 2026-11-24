import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { Core, ElementDefinition } from "cytoscape";
import { REL_COLOR, REL_LABEL, REL_TYPES, exportZip, neighbors, useSeeds, type GraphEdge, type GraphNode, type RelType } from "../api/kg";
import { ENTITY_TYPE_LABEL, useEntities, type EntityType } from "../api/news";
import { Card, field, ghost, primary } from "../features/news/settingsUi";

/** 知識の地図（/map）。物を点・関係を線（型で色）にして、選んだ点の周りから段階的に広げる。一度に全部は読まない（POST /graph/neighbors を段ごとに呼ぶ）。
 *  Cytoscape.js は地図を開いたときに遅延読み込み（版固定）。期間・種類・確かさ・関係の型で絞れる。推測・集計の線は破線（色だけに頼らない）。
 *  点を押すと下に出る札から、知識カードを開く・その点の周りを広げる。Obsidian の書き出しは範囲を選んで押したときだけ。 */
const PERIODS: [number, string][] = [[0, "全期間"], [30, "30 日"], [90, "90 日"], [365, "1 年"]];
const CONFS: [number, string][] = [[0, "すべて"], [0.3, "0.3 以上"], [0.5, "0.5 以上"], [0.7, "0.7 以上"]];
const NODE_COLOR: Record<EntityType, string> = {
  model: "#2563eb", software: "#0d9488", org: "#b45309", person: "#be185d", tech: "#7c3aed", product: "#0891b2", game: "#65a30d", policy: "#dc2626", place: "#52525b", other: "#71717a",
};
const STEP_LIMIT = 60;

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

export default function KnowledgeMapPage() {
  const dark = useDark();
  const [params] = useSearchParams();
  const box = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const nodes = useRef(new Map<number, GraphNode>());
  const edges = useRef(new Map<number, GraphEdge>());
  const [counts, setCounts] = useState({ n: 0, e: 0, hidden: 0 });
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [q, setQ] = useState("");
  const [period, setPeriod] = useState(0);
  const [kind, setKind] = useState("");
  const [minConf, setMinConf] = useState(0);
  const [active, setActive] = useState<Set<RelType>>(new Set(REL_TYPES));
  const found = useEntities({ q, type: "", days: 0, sort: "count" });
  const seeds = useSeeds();
  const filters = useRef({ period, kind, minConf, active });
  filters.current = { period, kind, minConf, active };

  const apply = useCallback(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const f = filters.current;
    const since = f.period ? Date.now() - f.period * 86_400_000 : 0;
    let hidden = 0;
    cy.batch(() => {
      cy.edges().forEach((e) => {
        const d = e.data();
        const ok = f.active.has(d.rel) && d.confidence >= f.minConf && (!since || !d.last_at || new Date(d.last_at).getTime() >= since);
        e.toggleClass("off", !ok);
        if (!ok) hidden += 1;
      });
      cy.nodes().forEach((n) => { n.toggleClass("off", !!f.kind && n.data("kind") !== f.kind && !n.data("seed")); });
    });
    setCounts({ n: cy.nodes().length, e: cy.edges().length, hidden });
  }, []);

  const style = useMemo(() => {
    const text = dark ? "#e4e4e7" : "#27272a";
    return [
      { selector: "node", style: { "background-color": "data(color)", label: "data(label)", color: text, "font-size": 11, "text-valign": "bottom", "text-margin-y": 3, "text-wrap": "ellipsis", "text-max-width": "90px",
                                    width: "mapData(size, 0, 60, 16, 38)", height: "mapData(size, 0, 60, 16, 38)", "border-width": 1, "border-color": dark ? "#18181b" : "#ffffff", "min-zoomed-font-size": 6 } },
      { selector: "node:selected", style: { "border-width": 3, "border-color": dark ? "#fbbf24" : "#111827" } },
      { selector: "edge", style: { "line-color": "data(color)", "target-arrow-color": "data(color)", width: "mapData(confidence, 0, 1, 1, 4)", "curve-style": "bezier", opacity: 0.85 } },
      { selector: "edge[directed = 1]", style: { "target-arrow-shape": "triangle" } },
      { selector: "edge[status = 'inferred'], edge[status = 'statistical']", style: { "line-style": "dashed" } },
      { selector: ".off", style: { display: "none" } },
    ] as never;
  }, [dark]);

  // Cytoscape は地図を開いたときだけ読む（遅延読み込み）
  useEffect(() => {
    let dead = false;
    let cy: Core | null = null;
    void import("cytoscape").then((mod) => {
      if (dead || !box.current) return;
      cy = mod.default({ container: box.current, style, wheelSensitivity: 0.3, minZoom: 0.15, maxZoom: 3, elements: [] });
      cyRef.current = cy;
      (box.current as unknown as { __cy: Core }).__cy = cy;
      cy.on("tap", "node", (ev) => setSelected(nodes.current.get(Number(ev.target.id())) ?? null));
      cy.on("tap", (ev) => { if (ev.target === cy) setSelected(null); });
      box.current.setAttribute("data-ready", "true");
      const focus = Number(params.get("focus"));
      if (Number.isFinite(focus) && focus > 0) void expand([focus]);
    });
    return () => { dead = true; cy?.destroy(); cyRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { cyRef.current?.style(style); }, [style]);
  useEffect(() => { apply(); }, [period, kind, minConf, active, apply]);

  const expand = useCallback(async (ids: number[]) => {
    setBusy(true);
    setMsg("");
    try {
      const f = filters.current;
      const res = await neighbors({ ids, types: f.active.size === REL_TYPES.length ? [] : [...f.active], depth: 1, limit: STEP_LIMIT, min_confidence: f.minConf, since: sinceIso(f.period) });
      const cy = cyRef.current;
      if (!cy) return;
      const add: ElementDefinition[] = [];
      const put = (n: GraphNode, seed: boolean) => {
        if (!nodes.current.has(n.id)) {
          nodes.current.set(n.id, n);
          add.push({ group: "nodes", data: { id: String(n.id), label: n.name, color: NODE_COLOR[n.type] ?? NODE_COLOR.other, size: n.article_count, kind: n.type, seed: seed ? 1 : 0 } });
        }
      };
      res.seeds.forEach((n) => put(n, true));
      res.levels.forEach((lv) => lv.nodes.forEach((n) => put(n, false)));
      res.levels.forEach((lv) => lv.edges.forEach((e) => {
        if (!edges.current.has(e.id) && nodes.current.has(e.source) && nodes.current.has(e.target)) {
          edges.current.set(e.id, e);
          add.push({ group: "edges", data: { id: `e${e.id}`, source: String(e.source), target: String(e.target), rel: e.type, color: REL_COLOR[e.type] ?? REL_COLOR.other, confidence: e.confidence,
                                             directed: e.directed ? 1 : 0, status: e.status, last_at: e.last_at } });
        }
      }));
      if (res.seeds.length === 0) setMsg("見つからない物か、読めない区分の物です。");
      cy.add(add);
      cy.layout({ name: "cose", animate: false, fit: cy.nodes().length <= add.length + 1, padding: 24, nodeRepulsion: () => 9000, idealEdgeLength: () => 70 } as never).run();
      apply();
      if (res.truncated) setMsg(`上限（${STEP_LIMIT} 本）で切りました。絞り込むか、点を選んで広げてください。`);
      else if (res.levels.every((l) => l.edges.length === 0) && res.seeds.length) setMsg("この条件で、つながる物はありません。");
    } catch {
      setMsg("地図を読み込めませんでした。");
    } finally {
      setBusy(false);
    }
  }, [apply]);

  const toggle = (t: RelType) => setActive((s) => { const n = new Set(s); if (n.has(t)) n.delete(t); else n.add(t); return n; });
  const results = (found.data?.items ?? []).slice(0, 6);

  return (
    <div className="mx-auto min-w-0 max-w-5xl space-y-3 p-2 md:p-6" data-testid="map-page">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">知識の地図</h1>
        <p className="text-xs leading-relaxed text-zinc-500">物を点、関係を線で見ます。始める物を選び、点を押して周りを広げます（一度に全部は読みません）。線の太さは確かさ、破線は「推測」「一緒に出る集計」です。</p>
      </header>

      <section className="space-y-2 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900" aria-label="始める物">
        <input type="search" aria-label="物を探して始める" data-testid="map-search" placeholder="物の名前で探す（例: Qwen）" value={q} onChange={(e) => setQ(e.target.value)} className={`${field} w-full`} />
        {q.trim() && (
          <ul className="flex flex-wrap gap-1.5" data-testid="map-results">
            {results.length === 0 && <li className="text-xs text-zinc-500">見つかりません。</li>}
            {results.map((r) => (
              <li key={r.id}><button type="button" data-testid="map-result" onClick={() => { void expand([r.id]); setQ(""); }} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{r.name}<span className="num text-zinc-500">{r.article_count}</span></button></li>
            ))}
          </ul>
        )}
        {!q.trim() && (seeds.data?.items.length ?? 0) > 0 && (
          <div className="flex flex-wrap items-center gap-1.5" data-testid="map-seeds"><span className="text-[11px] text-zinc-500">よく出る物:</span>
            {seeds.data!.items.slice(0, 8).map((s) => <button key={s.id} type="button" data-testid="map-seed" onClick={() => void expand([s.id])} className="min-h-9 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{s.name}</button>)}
          </div>
        )}
      </section>

      <section className="space-y-2 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900" aria-label="絞り込み" data-testid="map-filters">
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="text-[11px] text-zinc-500">期間（線の最後の記事）<select aria-label="期間" value={period} onChange={(e) => setPeriod(Number(e.target.value))} className={`${field} mt-0.5 w-full`}>{PERIODS.map(([n, l]) => <option key={n} value={n}>{l}</option>)}</select></label>
          <label className="text-[11px] text-zinc-500">物の種類<select aria-label="物の種類" value={kind} onChange={(e) => setKind(e.target.value)} className={`${field} mt-0.5 w-full`}><option value="">すべて</option>{(Object.keys(ENTITY_TYPE_LABEL) as EntityType[]).map((t) => <option key={t} value={t}>{ENTITY_TYPE_LABEL[t]}</option>)}</select></label>
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
      </section>

      <div className="relative rounded-2xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
        <div ref={box} data-testid="map-canvas" data-nodes={counts.n} data-edges={counts.e} role="img" aria-label={`知識の地図（点 ${counts.n}・線 ${counts.e}）`} className="h-[52vh] min-h-72 w-full touch-none" />
        {counts.n === 0 && <p className="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-zinc-500" data-testid="map-empty">上の「始める物」から選ぶと、ここに地図が出ます。</p>}
        {busy && <span role="status" className="absolute right-3 top-3 rounded-md bg-zinc-100 px-2 py-1 text-[11px] dark:bg-zinc-800">読み込み中…</span>}
      </div>
      <p className="num text-xs text-zinc-500" data-testid="map-count" role="status">点 {counts.n} · 線 {counts.e}{counts.hidden ? `（絞り込みで ${counts.hidden} 本を隠しています）` : ""}</p>
      {msg && <p role="status" className="text-xs text-amber-700 dark:text-amber-400" data-testid="map-msg">{msg}</p>}

      {selected && (
        <div className="space-y-2 rounded-2xl border border-accent-600 bg-white p-3 dark:bg-zinc-900" data-testid="map-selected">
          <p className="break-words text-sm font-semibold">{selected.name} <span className="ml-1 rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">{ENTITY_TYPE_LABEL[selected.type] ?? selected.type}</span>
            <span className="num ml-2 text-[11px] font-normal text-zinc-500">記事 {selected.article_count} 件</span></p>
          <div className="flex flex-wrap gap-2">
            <Link to={`/entities/${selected.id}`} data-testid="map-open-card" className={`${primary} inline-flex items-center`}>知識カードを開く</Link>
            <button type="button" data-testid="map-expand" disabled={busy} onClick={() => void expand([selected.id])} className={ghost}>この物の周りを広げる</button>
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
      setState({ busy: false, text: `${files} 件の物を書き出しました（物ごとの .md を zip にまとめています）。`, err: false });
    } catch {
      setState({ busy: false, text: "書き出せませんでした。", err: true });
    }
  };
  return (
    <Card id="map-export" title="Obsidian へ書き出す" hint="押したときだけ、範囲の物を 1 つずつの .md（front matter・[[関係する物]]・根拠の URL つき）にして zip で受け取ります。常には書き出しません。">
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
