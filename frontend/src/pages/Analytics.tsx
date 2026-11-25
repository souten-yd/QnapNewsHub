import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useAnalyticsFilters, useCompare, useHotTopics, useOverview, useSeries, useSourcesAnalysis, useTrendStats, type By, type Filters, type HotTopic, type Range, type Series } from "../api/analytics";
import { Sparkline, Skeleton } from "../components/ui";
import { Chart } from "../features/analytics/Chart";
import { chartColors, colorOf, useIsDark } from "../features/analytics/theme";
import type { EChartsOption } from "../features/analytics/echarts";

const RANGES: [Range, string][] = [["24h", "24 時間"], ["7d", "7 日"], ["30d", "30 日"], ["90d", "90 日"]];
const BYS: [By, string][] = [["none", "分けない"], ["kind", "分類"], ["channel", "チャンネル"], ["source", "取得先"], ["lang", "言語"], ["origin", "出どころ"]];
const num = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("ja-JP"));
const pct = (n: number | null | undefined) => (n == null ? "—" : `${n.toFixed(n % 1 ? 1 : 0)}%`);

function Seg<T extends string>({ value, onChange, items, label, testId }: { value: T; onChange: (v: T) => void; items: [T, string][]; label: string; testId?: string }) {
  return (
    <div role="radiogroup" aria-label={label} data-testid={testId} className="flex flex-wrap gap-1">
      {items.map(([id, text]) => (
        <button key={id} type="button" role="radio" aria-checked={value === id} onClick={() => onChange(id)}
          className={`min-h-10 rounded-lg border px-3 text-xs ${value === id ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{text}</button>
      ))}
    </div>
  );
}

function Section({ id, title, hint, children }: { id: string; title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section id={id} data-testid={id} className="min-w-0 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900 md:p-4">
      <h2 className="text-base font-bold">{title}</h2>
      {hint && <p className="mb-2 mt-0.5 text-xs text-zinc-500">{hint}</p>}
      <div className="mt-2 min-w-0">{children}</div>
    </section>
  );
}

function Stat({ label, value, sub, id }: { label: string; value: string; sub?: string; id: string }) {
  return (
    <div data-testid={id} className="min-w-0 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="num mt-1 text-2xl font-bold leading-none">{value}</div>
      {sub && <div className="mt-1.5 text-[11px] leading-snug text-zinc-500">{sub}</div>}
    </div>
  );
}

function Cards({ f }: { f: Filters }) {
  const o = useOverview(f);
  if (o.isLoading) return <div className="grid grid-cols-2 gap-2 md:grid-cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24" />)}</div>;
  if (o.isError || !o.data) return <p role="alert" className="text-sm text-red-600">数字を読み込めませんでした。</p>;
  const d = o.data;
  const ratio = d.ratio_24h;
  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-5" data-testid="cards">
      <Stat id="card-today" label="今日の記事" value={`${num(d.today)} 件`} sub={d.unsaved > 0 ? `ほか未保存 ${d.unsaved} 件（数えていません）` : `直近 24 時間 ${num(d.last24h)} 件`} />
      <Stat id="card-indep" label="独立した記事" value={`${num(d.today_indep)} 件`} sub="近い重複を除いた今日の数" />
      <Stat id="card-events" label="独立したイベント" value={d.independent_events_24h == null ? "—" : `${num(d.independent_events_24h)} 件`}
        sub={d.independent_events_24h == null ? "" : `直近 24 時間。まとまり ${num(d.events_24h)} 件 + 単独の記事 ${num(d.singles_24h)} 件${d.independent_events_prev_daily != null ? `（前の 7 日の 1 日平均 ${d.independent_events_prev_daily} 件）` : ""}`} />
      <Stat id="card-sources" label="取得先" value={`${num(d.sources_today)} 件`} sub="今日、記事を出した取得先の数" />
      <Stat id="card-ratio" label="前の 7 日との比" value={ratio == null ? "—" : `×${ratio.toFixed(2)}`}
        sub={d.prev7_daily_avg == null ? "比べる履歴がまだありません" : `直近 24 時間 ÷ 前の 7 日の 1 日平均（${d.prev7_daily_avg} 件）`} />
    </div>
  );
}

const hourLabel = (t: string) => (t.endsWith(" 00") ? t.slice(5, 10) : `${t.slice(11)}時`);
const dayLabel = (t: string) => t.slice(5);

function trendOption(s: Series, dark: boolean): EChartsOption {
  const c = chartColors(dark);
  const hour = s.grain === "hour";
  return {
    grid: { left: 8, right: 12, top: 52, bottom: 62, containLabel: true },
    legend: { type: "scroll", top: 0, textStyle: { color: c.text, fontSize: 12 }, pageTextStyle: { color: c.text }, pageIconColor: c.text, pageIconInactiveColor: c.axis },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => `${v} 件` },
    xAxis: { type: "category", data: s.buckets, axisLabel: { color: c.text, fontSize: 12, hideOverlap: true, formatter: (v: string) => (hour ? hourLabel(v) : dayLabel(v)) }, axisLine: { lineStyle: { color: c.axis } } },
    yAxis: { type: "value", name: "件", nameTextStyle: { color: c.text, fontSize: 12 }, axisLabel: { color: c.text, fontSize: 12 }, splitLine: { lineStyle: { color: c.grid } } },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 22, bottom: 6, textStyle: { color: c.text, fontSize: 11 }, borderColor: c.axis }],
    series: s.series.map((it) => ({ name: it.label, type: "bar", stack: "total", data: it.counts, itemStyle: { color: colorOf(s.by, it.key, it.color) }, emphasis: { focus: "series" } })),
  };
}

function Trend({ f, range }: { f: Filters; range: Range }) {
  const [by, setBy] = useState<By>("kind");
  const [grain, setGrain] = useState<"" | "hour" | "day">("");
  const [metric, setMetric] = useState<"all" | "indep">("all");
  const dark = useIsDark();
  const eff = grain || (range === "24h" || range === "7d" ? "hour" : "day");
  const s = useSeries({ range, grain: eff, by, metric, ...f });
  const option = useMemo(() => (s.data ? trendOption(s.data, dark) : null), [s.data, dark]);
  return (
    <Section id="sec-trend" title="新着の傾向" hint="記事の数を、時間ごと・日ごとに積み上げます。凡例を押すと絞り込み、下の帯で範囲を選べます。">
      <div className="mb-2 flex flex-col gap-2">
        <Seg label="分け方" value={by} onChange={setBy} items={BYS} testId="trend-by" />
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          <Seg label="粒度" value={eff as "hour" | "day"} onChange={setGrain} items={[["hour", "時間ごと"], ["day", "日ごと"]]} testId="trend-grain" />
          <Seg label="数え方" value={metric} onChange={setMetric} items={[["all", "すべて"], ["indep", "重複を除く"]]} testId="trend-metric" />
        </div>
      </div>
      {s.isLoading ? <Skeleton className="h-72" /> : s.isError || !option ? <p role="alert" className="text-sm text-red-600">グラフを読み込めませんでした。</p>
        : s.data!.total === 0 ? <p className="py-8 text-center text-sm text-zinc-500" data-testid="trend-empty">この期間の記事はありません。</p>
        : <Chart option={option} height={340} testId="trend-chart" label={`記事の数の推移（${s.data!.grain === "hour" ? "時間" : "日"}ごと・${BYS.find((b) => b[0] === by)?.[1]}別の積み上げ。合計 ${s.data!.total} 件）`} />}
    </Section>
  );
}

function TopicRow({ t, rank, showScore = true }: { t: HotTopic; rank: number; showScore?: boolean }) {
  return (
    <li data-testid="hot-row" className="flex min-w-0 items-center gap-2 border-b border-zinc-100 py-2 last:border-0 dark:border-zinc-800">
      <span className="num w-5 shrink-0 text-right text-xs text-zinc-500">{rank}</span>
      <Link to={t.kind === "entity" && t.entity_id ? `/entities/${t.entity_id}` : `/?q=${encodeURIComponent(t.label)}`} data-testid="hot-link" className="flex min-h-11 min-w-0 flex-1 items-center gap-2" aria-label={t.kind === "entity" ? `${t.label}（Things）のページを見る` : `${t.label} の記事を見る`}>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-1.5"><span className="truncate text-sm font-semibold">{t.label}</span>{t.kind === "entity" && <span data-testid="hot-entity-mark" className="shrink-0 rounded-md bg-accent-600/10 px-1 py-0.5 text-[10px] font-medium text-accent-700 dark:text-accent-300">Things</span>}</span>
          <span className="num block break-words text-[11px] leading-snug text-zinc-500">{showScore && t.score != null ? `点数 ${t.score.toFixed(1)} · ` : ""}{t.growth_pct != null && showScore ? `平均より +${num(t.growth_pct)}% · ` : ""}{t.count} 件 · 出どころ {t.sources}</span>
        </span>
        <span className="w-16 shrink-0 text-accent-600 sm:w-24" aria-hidden><Sparkline values={t.spark} max={Math.max(1, ...t.spark)} fill heightClass="h-6" /></span>
      </Link>
    </li>
  );
}

function Hot({ f }: { f: Filters }) {
  const h = useHotTopics(f);
  return (
    <Section id="sec-hot" title="ホットトピック" hint="直近 24 時間に、いつもより増えた語。常に多い語（AI・ゲームなど）は平均が高いので上に来ません。押すとその語の記事を探します。">
      {h.isLoading ? <Skeleton className="h-48" /> : h.isError || !h.data ? <p role="alert" className="text-sm text-red-600">読み込めませんでした。</p> : (
        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          <div className="min-w-0">
            <h3 className="text-sm font-semibold">急に増えた語 上位 10</h3>
            {!h.data.enough_history ? <p className="py-3 text-xs text-zinc-500" data-testid="hot-nohistory">比べる履歴がまだ足りません（取得を始めて {h.data.baseline_days} 日分）。3 日分たまると点数が出ます。</p>
              : h.data.top.length === 0 ? <p className="py-3 text-xs text-zinc-500" data-testid="hot-empty">いまは目立って増えた語がありません。</p>
              : <ol data-testid="hot-list">{h.data.top.map((t, i) => <TopicRow key={t.term} t={t} rank={i + 1} />)}</ol>}
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold">新しく出てきた語</h3>
            <p className="text-[11px] text-zinc-500">これまで（過去 28 日）ほとんど出ていなかった語。</p>
            {h.data.new.length === 0 ? <p className="py-3 text-xs text-zinc-500" data-testid="new-empty">ありません。</p>
              : <ol data-testid="new-list">{h.data.new.map((t, i) => <TopicRow key={t.term} t={t} rank={i + 1} showScore={false} />)}</ol>}
          </div>
          {h.data.fields.length > 0 && (
            <div className="min-w-0 lg:col-span-2" data-testid="hot-fields">
              <h3 className="text-sm font-semibold">分野（チャンネル）ごと</h3>
              <div className="mt-1 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {h.data.fields.map((fl) => (
                  <div key={fl.channel} className="min-w-0 rounded-xl border border-zinc-200 p-2 dark:border-zinc-800">
                    <div className="flex items-center gap-1.5 text-xs font-semibold"><span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: fl.color }} /><span className="truncate">{fl.name}</span></div>
                    <ol>{fl.top.map((t, i) => <TopicRow key={t.term} t={t} rank={i + 1} />)}</ol>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

const diffLabel = (cur: number | null | undefined, prev: number | null | undefined, unit = "") => (cur == null || prev == null ? "" : `${cur - prev >= 0 ? "+" : ""}${(Math.round((cur - prev) * 10) / 10).toLocaleString("ja-JP")}${unit}`);

function Arrivals() {
  const t = useTrendStats();
  return (
    <Section id="sec-arrivals" title="新着の傾向" hint="直近 24 時間を、その前の 7 日（1 日あたり）と比べます。数字は集計の表から（LLM なし）。解説だけが AI です。">
      {t.isLoading ? <Skeleton className="h-64" /> : t.isError || !t.data ? <p role="alert" className="text-sm text-red-600">読み込めませんでした。</p> : (() => {
        const d = t.data;
        const rows: [string, string, string, string][] = [
          ["記事", `${num(d.cur.n)} 件`, d.prev.n_per_day == null ? "—" : `${num(d.prev.n_per_day)} 件/日`, d.ratio == null ? "" : `×${d.ratio.toFixed(2)}`],
          ["重複の率", pct(d.cur.dup_pct), pct(d.prev.dup_pct), diffLabel(d.cur.dup_pct, d.prev.dup_pct, " pt")],
          ["画像のある率", pct(d.cur.img_pct), pct(d.prev.img_pct), diffLabel(d.cur.img_pct, d.prev.img_pct, " pt")],
          ["発表から取得までの遅れ（平均）", d.cur.lag_min == null ? "—" : `${d.cur.lag_min} 分`, d.prev.lag_min == null ? "—" : `${d.prev.lag_min} 分`, diffLabel(d.cur.lag_min, d.prev.lag_min, " 分")],
          ["1 時間以内に取れた率", pct(d.cur.fast_pct), pct(d.prev.fast_pct), diffLabel(d.cur.fast_pct, d.prev.fast_pct, " pt")],
          ["一次情報の率", pct(d.cur.primary_pct), pct(d.prev.primary_pct), diffLabel(d.cur.primary_pct, d.prev.primary_pct, " pt")],
          ["独立したイベント", `${num(d.events.independent_events_24h)} 件`, d.events.independent_events_prev_daily == null ? "—" : `${d.events.independent_events_prev_daily} 件/日`, ""],
        ];
        return (
          <div className="grid min-w-0 gap-4 lg:grid-cols-2">
            <div className="min-w-0 lg:col-span-2" data-testid="trend-note">
              <h3 className="text-sm font-semibold">今日の傾向 <span className="ml-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-800 dark:text-amber-300">AI の解説</span></h3>
              {d.note ? (
                <>
                  <ul className="mt-1 space-y-2" data-testid="trend-note-lines">
                    {d.note.lines.map((ln, i) => (
                      <li key={i} className="min-w-0 break-words text-sm leading-relaxed" data-testid="trend-note-line">{ln.text}
                        <span className="ml-1.5 inline-flex flex-wrap gap-1 align-middle">
                          {ln.refs.map((r) => r.to ? <Link key={r.key} to={r.to} title={r.label} className="rounded-md border border-zinc-300 px-1.5 py-0.5 text-[10px] text-accent-700 dark:border-zinc-700 dark:text-accent-300">{r.kind === "event" ? "イベント" : "Things"} {r.key}</Link>
                            : <span key={r.key} title={r.label} className="rounded-md bg-zinc-100 px-1.5 py-0.5 text-[10px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">数字 {r.key}</span>)}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <p className="mt-1 text-[11px] text-zinc-500">{d.note.day} の{d.note.slot === 0 ? "朝" : "夕方"} · {d.note.model}。数字は集計にあるものだけを使い、間違いを含むことがあります。</p>
                </>
              ) : <p className="py-2 text-xs text-zinc-500" data-testid="trend-note-empty">まだ解説はありません（1 日 1〜2 回、無料の LLM が書きます。記事が直近 24 時間に 20 件以上あるときだけ）。</p>}
            </div>
            <div className="min-w-0" data-testid="arrivals-table-wrap">
              <div className="max-w-full overflow-x-auto">
                <table className="w-full min-w-[20rem] text-xs" data-testid="arrivals-table">
                  <thead><tr className="border-b border-zinc-200 text-left text-zinc-500 dark:border-zinc-800"><th className="py-1.5 pr-2 font-medium">項目</th><th className="px-2 text-right font-medium">直近 24 時間</th><th className="px-2 text-right font-medium">前の 7 日</th><th className="pl-2 text-right font-medium">差</th></tr></thead>
                  <tbody>{rows.map(([k, a, b, c]) => <tr key={k} className="num border-b border-zinc-100 last:border-0 dark:border-zinc-800"><td className="py-1.5 pr-2 font-medium">{k}</td><td className="px-2 text-right">{a}</td><td className="px-2 text-right">{b}</td><td className="pl-2 text-right">{c}</td></tr>)}</tbody>
                </table>
              </div>
              {!d.enough_history && <p className="mt-1 text-[11px] text-zinc-500">比べる履歴がまだ足りません（{d.prev.days} 日分）。</p>}
            </div>
            <div className="min-w-0 space-y-3">
              <div>
                <h3 className="text-sm font-semibold">新しく出たThings</h3>
                {d.new_entities.length === 0 ? <p className="py-1 text-xs text-zinc-500" data-testid="arrivals-new-empty">ありません。</p> : (
                  <ul className="mt-1 flex flex-wrap gap-1.5" data-testid="arrivals-new">{d.new_entities.map((e) => <li key={e.id}><Link to={`/entities/${e.id}`} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{e.name}<span className="num text-zinc-500">{e.count}</span></Link></li>)}</ul>
                )}
              </div>
              <div>
                <h3 className="text-sm font-semibold">急に増えたThings</h3>
                {d.surging_entities.length === 0 ? <p className="py-1 text-xs text-zinc-500">ありません。</p> : (
                  <ul className="mt-1 flex flex-wrap gap-1.5" data-testid="arrivals-surging">{d.surging_entities.map((e) => <li key={e.entity_id}><Link to={`/entities/${e.entity_id}`} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{e.label}<span className="num text-zinc-500">{e.count} 件</span></Link></li>)}</ul>
                )}
              </div>
              {d.channels.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold">分野ごとの勢い</h3>
                  <ul className="mt-1 text-xs" data-testid="arrivals-channels">{d.channels.map((c) => (
                    <li key={c.channel} className="num flex justify-between gap-2 border-b border-zinc-100 py-1 dark:border-zinc-800"><span className="flex min-w-0 items-center gap-1.5"><span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: c.color }} /><span className="truncate">{c.name}</span></span><span>{c.cur} 件{c.ratio != null ? `（×${c.ratio.toFixed(2)}）` : ""}</span></li>
                  ))}</ul>
                </div>
              )}
            </div>
          </div>
        );
      })()}
    </Section>
  );
}

function heatOption(days: string[], rows: { name: string; daily: number[] }[], dark: boolean): EChartsOption {
  const c = chartColors(dark);
  const data: [number, number, number][] = [];
  rows.forEach((r, y) => r.daily.forEach((v, x) => data.push([x, y, v])));
  const max = Math.max(1, ...data.map((d) => d[2]));
  const short = (s: string) => (s.length > 14 ? `${s.slice(0, 13)}…` : s);
  return {
    grid: { left: 8, right: 12, top: 8, bottom: 48, containLabel: true },
    tooltip: { position: "top", formatter: (p: unknown) => { const d = (p as { data: [number, number, number] }).data; return `${rows[d[1]].name}<br/>${days[d[0]]}: ${d[2]} 件`; } },
    xAxis: { type: "category", data: days, splitArea: { show: true }, axisLabel: { color: c.text, fontSize: 12, hideOverlap: true, formatter: dayLabel } },
    yAxis: { type: "category", data: rows.map((r) => short(r.name)), inverse: true, axisLabel: { color: c.text, fontSize: 12 } },
    visualMap: { min: 0, max, calculable: false, orient: "horizontal", left: "center", bottom: 0, itemWidth: 12, itemHeight: 80, text: ["多", "少"], textStyle: { color: c.text, fontSize: 12 }, inRange: { color: dark ? ["#1e293b", "#3b82f6", "#bfdbfe"] : ["#eff6ff", "#60a5fa", "#1d4ed8"] } },
    series: [{ type: "heatmap", data, label: { show: false }, emphasis: { itemStyle: { borderColor: c.text, borderWidth: 1 } } }],
  };
}

function SourcesPanel({ f, range }: { f: Filters; range: Range }) {
  const s = useSourcesAnalysis({ range, ...f });
  const dark = useIsDark();
  const top = (s.data?.sources ?? []).slice(0, 20);
  const option = useMemo(() => (s.data && top.length ? heatOption(s.data.days, top, dark) : null), [s.data, dark]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Section id="sec-sources" title="取得先の分析" hint="量・取得の成功率・304（変更なし）の割合・画像のある割合・近い重複の割合。成功率と 304 は絞り込みの影響を受けません。">
      {s.isLoading ? <Skeleton className="h-48" /> : s.isError || !s.data ? <p role="alert" className="text-sm text-red-600">読み込めませんでした。</p> : top.length === 0 ? <p className="py-4 text-center text-sm text-zinc-500">この期間の取得先のデータはありません。</p> : (
        <>
          <div className="max-w-full overflow-x-auto" data-testid="sources-scroll">
            <table className="w-full min-w-[34rem] text-xs" data-testid="sources-table">
              <thead><tr className="border-b border-zinc-200 text-left text-zinc-500 dark:border-zinc-800">
                <th className="py-1.5 pr-2 font-medium">取得先</th><th className="px-2 text-right font-medium">記事（件）</th><th className="px-2 text-right font-medium">成功率</th>
                <th className="px-2 text-right font-medium">304</th><th className="px-2 text-right font-medium">画像あり</th><th className="pl-2 text-right font-medium">重複</th></tr></thead>
              <tbody>
                {s.data.sources.slice(0, 30).map((r) => (
                  <tr key={r.key} className="num border-b border-zinc-100 last:border-0 dark:border-zinc-800">
                    <td className="max-w-[12rem] truncate py-1.5 pr-2 font-medium" title={r.key}>{r.name}</td><td className="px-2 text-right">{num(r.articles)}</td><td className="px-2 text-right">{pct(r.success_pct)}</td>
                    <td className="px-2 text-right">{pct(r.not_modified_pct)}</td><td className="px-2 text-right">{pct(r.image_pct)}</td><td className="pl-2 text-right">{pct(r.dup_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {option && (
            <div className="mt-4">
              <h3 className="text-sm font-semibold">取得先 × 日（記事の件数・上位 {top.length}）</h3>
              <Chart option={option} height={Math.max(180, top.length * 26 + 70)} testId="heatmap" label={`取得先ごと・日ごとの記事の数のヒートマップ（上位 ${top.length} の取得先）`} />
            </div>
          )}
        </>
      )}
    </Section>
  );
}

function ComparePanel({ f }: { f: Filters }) {
  const [by, setBy] = useState("kind");
  const c = useCompare({ by, ...f });
  const dark = useIsDark();
  const option = useMemo<EChartsOption | null>(() => {
    if (!c.data || c.data.rows.length === 0) return null;
    const col = chartColors(dark);
    const rows = c.data.rows.slice(0, 12);
    return {
      grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      legend: { top: 0, textStyle: { color: col.text, fontSize: 12 } },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => `${v} 件` },
      xAxis: { type: "value", name: "件", axisLabel: { color: col.text, fontSize: 12 }, nameTextStyle: { color: col.text, fontSize: 12 }, splitLine: { lineStyle: { color: col.grid } } },
      yAxis: { type: "category", inverse: true, data: rows.map((r) => r.label), axisLabel: { color: col.text, fontSize: 12, width: 90, overflow: "truncate" } },
      series: [{ name: "今週", type: "bar", data: rows.map((r) => r.this), itemStyle: { color: "#2563eb" } }, { name: "先週", type: "bar", data: rows.map((r) => r.prev), itemStyle: { color: "#94a3b8" } }],
    };
  }, [c.data, dark]);
  return (
    <Section id="sec-compare" title="期間の比較" hint="今週（直近 7 日）と先週（その前の 7 日）の記事の数。">
      <Seg label="比べる分け方" value={by} onChange={setBy} items={[["kind", "分類"], ["channel", "チャンネル"], ["lang", "言語"], ["origin", "出どころ"]]} testId="compare-by" />
      {c.isLoading ? <Skeleton className="mt-2 h-48" /> : c.isError || !c.data ? <p role="alert" className="mt-2 text-sm text-red-600">読み込めませんでした。</p> : !option ? <p className="py-4 text-center text-sm text-zinc-500">比べるデータがまだありません。</p> : (
        <>
          <p className="num mt-2 text-xs text-zinc-500" data-testid="compare-total">今週 {num(c.data.total_this)} 件 / 先週 {num(c.data.total_prev)} 件（{c.data.this_week[0]}〜{c.data.this_week[1]}）</p>
          <Chart option={option} height={Math.max(160, Math.min(12, c.data.rows.length) * 44 + 60)} testId="compare-chart" label="今週と先週の記事の数の比較" />
          <ul className="mt-2 grid gap-1 text-xs sm:grid-cols-2" data-testid="compare-list">
            {c.data.rows.slice(0, 12).map((r) => (
              <li key={r.key} className="num flex justify-between gap-2 border-b border-zinc-100 py-1 dark:border-zinc-800"><span className="min-w-0 truncate">{r.label}</span>
                <span className={r.delta > 0 ? "text-emerald-700 dark:text-emerald-400" : r.delta < 0 ? "text-red-700 dark:text-red-400" : ""}>{r.delta > 0 ? "+" : ""}{r.delta} 件{r.pct != null ? `（${r.pct > 0 ? "+" : ""}${r.pct}%）` : "（新規）"}</span></li>
            ))}
          </ul>
        </>
      )}
    </Section>
  );
}

export default function AnalyticsPage() {
  const [range, setRange] = useState<Range>("7d");
  const [kind, setKind] = useState("");
  const [lang, setLang] = useState("");
  const flt = useAnalyticsFilters();
  const { hash } = useLocation();
  useEffect(() => {      // ホームの「ホットトピック」から来たときは、その節へ
    if (!hash) return;
    const t = setTimeout(() => document.getElementById(hash.slice(1))?.scrollIntoView({ block: "start" }), 400);
    return () => clearTimeout(t);
  }, [hash]);
  const f: Filters = { kind, lang };
  return (
    <div className="mx-auto min-w-0 max-w-6xl space-y-3 p-2 md:p-6" data-testid="analytics-page">
      <header className="min-w-0">
        <h1 className="text-xl font-bold">分析</h1>
        <p className="text-xs text-zinc-500">1 日に集まる記事を、数字とグラフで見ます（LLM は使いません。保存した記事の集計だけ）。</p>
      </header>
      <div className="flex flex-col gap-2 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900" data-testid="filters">
        <Seg label="期間" value={range} onChange={setRange} items={RANGES} testId="filter-range" />
        <Seg label="分類" value={kind} onChange={setKind} items={[["", "すべての分類"], ...((flt.data?.kinds ?? []).map((k) => [k.id, k.label] as [string, string]))]} testId="filter-kind" />
        <Seg label="言語" value={lang} onChange={setLang} items={[["", "すべての言語"], ["ja", "日本語"], ["en", "英語"]]} testId="filter-lang" />
      </div>
      <Cards f={f} />
      <Arrivals />
      <Trend f={f} range={range} />
      <Hot f={f} />
      <SourcesPanel f={f} range={range} />
      <ComparePanel f={f} />
      <div className="grid gap-2 md:grid-cols-2">
        <Link to="/map" data-testid="map-link" className="block min-w-0 rounded-2xl border border-zinc-200 p-3 hover:border-accent-600 dark:border-zinc-800">
          <h2 className="text-sm font-bold">知識の地図 →</h2>
          <p className="mt-1 text-xs text-zinc-500">Things（モデル・会社・技術）のつながりを地図で見る。点を選ぶとカード、周りから段階的に広げる</p>
        </Link>
        <Link to="/entities" data-testid="entities-link" className="block min-w-0 rounded-2xl border border-zinc-200 p-3 hover:border-accent-600 dark:border-zinc-800">
          <h2 className="text-sm font-bold">Things →</h2>
          <p className="mt-1 text-xs text-zinc-500">記事から AI が取り出したThings（モデル・会社・技術）の一覧と、Thingsごとの年表・facts を見る</p>
        </Link>
      </div>
    </div>
  );
}
