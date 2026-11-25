import { useState } from "react";
import { Link } from "react-router-dom";
import { ENTITY_TYPE_LABEL, useEntities } from "../api/news";
import { COND_LABEL, useWatch, useWatchActions, useWatchChanges, type WatchChange } from "../api/watch";
import { Card, field, ghost, primary } from "../features/news/settingsUi";

/** ウォッチ（/watch）: 監視する Things を足し、「前回見てから何が変わったか」を機械的な要約で見る。LLM は使わない。
 *  「見た」を押すまで起点は進まない（要約を読むだけでは変わらない）。LLM には API（GET /watch/changes?format=text）で同じ要約が渡る。 */
const day = (iso: string) => iso.slice(0, 10);

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId: string }) {
  return <div data-testid={testId}><h4 className="mt-2 text-[11px] font-semibold text-zinc-500">{title}</h4><ul className="mt-1 space-y-1 text-xs leading-relaxed">{children}</ul></div>;
}

function Change({ c }: { c: WatchChange }) {
  return (
    <li className="min-w-0 rounded-xl border border-zinc-200 p-3 dark:border-zinc-800" data-testid="watch-change">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <Link to={`/entities/${c.thing.id}`} className="break-words text-sm font-semibold hover:underline">{c.thing.name}</Link>
        <span className="num text-[11px] text-zinc-500">{day(c.since)} 以降</span>
      </div>
      {c.quiet && <p className="mt-1.5 text-xs text-zinc-500" data-testid="watch-quiet">変化はありません。</p>}
      {c.change_points.length > 0 && (
        <Section title="変化点" testId="watch-points">{c.change_points.map((p, i) => <li key={i} className="break-words text-amber-800 dark:text-amber-300">{p.line}</li>)}</Section>
      )}
      {c.claims.length > 0 && (
        <Section title="新しい主張（AI の取り出し）" testId="watch-claims">
          {c.claims.map((x) => (
            <li key={x.id} className="break-words">
              {x.grounded && <span title="原文に照合できた" className="mr-1 text-emerald-700 dark:text-emerald-400">◎</span>}{x.text}
              {x.value && <span className="num ml-1 font-semibold">{x.value}</span>}
              {x.cond && <span className="ml-1 text-zinc-500">（{Object.entries(x.cond).map(([k, v]) => `${COND_LABEL[k] ?? k} ${v}`).join("・")}）</span>}
              {x.src && <span className="ml-1 text-[11px] text-zinc-400">{x.src}</span>}
            </li>
          ))}
        </Section>
      )}
      {c.metrics.length > 0 && (
        <Section title="数値の変化" testId="watch-metrics">
          {c.metrics.map((m, i) => (
            <li key={i} className="num break-words">{m.metric}{m.cond ? `（${m.cond}）` : ""}: {m.value}{m.unit}{m.previous !== undefined && <span className="ml-1 text-zinc-500">（前 {m.previous}{m.pct != null ? `・${m.pct > 0 ? "+" : ""}${m.pct}%` : ""}）</span>}</li>
          ))}
        </Section>
      )}
      {c.relations.length > 0 && <Section title="新しい関係" testId="watch-relations">{c.relations.map((r) => <li key={r.id} className="break-words">{r.subject} → {r.object}<span className="ml-1 text-zinc-500">（{r.type}）</span></li>)}</Section>}
      {c.versions.length > 0 && <Section title="版・リリース" testId="watch-versions">{c.versions.map((v) => <li key={v.doc} className="break-words">{v.title}{v.version && <span className="num ml-1 font-semibold">{v.version}</span>}<span className="ml-1 text-zinc-500">{day(v.at)}</span></li>)}</Section>}
      {c.events.length > 0 && <Section title="イベント" testId="watch-events">{c.events.map((e) => <li key={e.id}><Link to={`/events/${e.id}`} className="break-words hover:underline">{e.title}</Link><span className="ml-1 text-zinc-500">記事 {e.n_articles}</span></li>)}</Section>}
    </li>
  );
}

export default function WatchPage() {
  const watch = useWatch();
  const changes = useWatchChanges();
  const act = useWatchActions();
  const [q, setQ] = useState("");
  const found = useEntities({ q, type: "", days: 0, sort: "count" });
  const watched = new Set((watch.data?.items ?? []).map((w) => w.entity.id));
  return (
    <div className="mx-auto max-w-3xl space-y-3 p-3 md:p-6" data-testid="watch-page">
      <h1 className="text-xl font-semibold tracking-tight">ウォッチ</h1>
      <p className="text-xs leading-relaxed text-zinc-500">監視する Things を足すと、前に見てから新しくなった主張・数値・関係・版・イベント・変化点を集めます（AI の解説は付けません。主張は AI の取り出しです）。「見た」を押すと、そこから先の差になります。</p>
      <Card id="watch-add" title="監視に足す">
        <label className="block text-xs text-zinc-500" htmlFor="watch-q">Things の名前</label>
        <input id="watch-q" className={`${field} w-full`} value={q} onChange={(e) => setQ(e.target.value)} placeholder="例: llama.cpp" data-testid="watch-search" />
        {q.trim() && (
          <ul className="space-y-1" data-testid="watch-found">
            {(found.data?.items ?? []).slice(0, 6).map((e) => (
              <li key={e.id} className="flex min-w-0 items-center justify-between gap-2 text-sm">
                <span className="min-w-0 break-words">{e.name}<span className="ml-1 text-[11px] text-zinc-500">{ENTITY_TYPE_LABEL[e.type]}</span></span>
                <button type="button" className={ghost} disabled={watched.has(e.id) || act.add.isPending} onClick={() => act.add.mutate(e.id)} data-testid="watch-add-btn">{watched.has(e.id) ? "監視中" : "足す"}</button>
              </li>
            ))}
            {found.data && found.data.items.length === 0 && <li className="text-xs text-zinc-500">見つかりません。</li>}
          </ul>
        )}
      </Card>
      {watch.isLoading && <p role="status" className="text-sm text-zinc-500">読み込み中…</p>}
      {watch.isError && <p role="alert" className="text-sm text-red-600">ウォッチリストを読み込めませんでした。</p>}
      {watch.data && watch.data.items.length === 0 && <p className="rounded-xl border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500 dark:border-zinc-700" data-testid="watch-empty">監視している Things はまだありません。上で足してください。</p>}
      {watch.data && watch.data.items.length > 0 && (
        <Card id="watch-list" title={`監視中（${watch.data.items.length}）`}>
          <ul className="space-y-1" data-testid="watch-list">
            {watch.data.items.map((w) => (
              <li key={w.id} className="flex min-w-0 items-center justify-between gap-2 text-sm" data-testid="watch-row">
                <span className="min-w-0 break-words">{w.entity.name}<span className="num ml-1 text-[11px] text-zinc-500">前回 {day(w.seen_at)}</span></span>
                <button type="button" className={ghost} onClick={() => act.remove.mutate(w.entity.id)} aria-label={`${w.entity.name} を外す`}>外す</button>
              </li>
            ))}
          </ul>
        </Card>
      )}
      {changes.data && changes.data.items.length > 0 && (
        <section aria-labelledby="watch-changes-h" className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id="watch-changes-h" className="text-sm font-semibold text-zinc-500">前回から（変化のある物 <span data-testid="watch-changed">{changes.data.changed}</span> / {changes.data.items.length}）</h2>
            <button type="button" className={primary} onClick={() => act.seen.mutate(null)} disabled={act.seen.isPending} data-testid="watch-seen">見た（ここから先の差にする）</button>
          </div>
          <ul className="space-y-2">{changes.data.items.map((c) => <Change key={c.thing.id} c={c} />)}</ul>
        </section>
      )}
    </div>
  );
}
