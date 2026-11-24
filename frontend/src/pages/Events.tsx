import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { hoursSince, sinceLabel, useEvent, useEvents, type EventRow, type EventSort } from "../api/events";
import { ENTITY_TYPE_LABEL } from "../api/news";
import { Card } from "../features/news/settingsUi";

/** イベント（/events）: 同じ出来事を報じる記事のまとまり（SimHash・題の語・取り出した物の重なりで、言語をまたいで粗くまとめる）。
 *  イベントのページ（/events/:id）は関わる記事を時刻順に・出どころの種類ごとに。要約は AI（無料の LLM）で、記事が 3 件以上・出どころ 2 以上のときだけ。 */
const SORTS: [EventSort, string][] = [["recent", "動きの新しい順"], ["size", "記事の多い順"], ["first", "初報の新しい順"]];
const DAYS: [number, string][] = [[1, "1 日"], [2, "2 日"], [7, "7 日"], [30, "30 日"]];
const AI_SUMMARY = "AI の要約";
const time = (iso: string) => new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

function Seg<T extends string | number>({ value, onChange, items, label, testId }: { value: T; onChange: (v: T) => void; items: [T, string][]; label: string; testId: string }) {
  return (
    <div role="radiogroup" aria-label={label} data-testid={testId} className="flex flex-wrap gap-1.5">
      {items.map(([id, text]) => (
        <button key={String(id)} type="button" role="radio" aria-checked={value === id} onClick={() => onChange(id)}
          className={`min-h-9 rounded-lg border px-3 text-xs ${value === id ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{text}</button>
      ))}
    </div>
  );
}

export function PrimaryMark() {
  return <span data-testid="primary-mark" className="rounded-md bg-emerald-600/15 px-1.5 py-0.5 text-[11px] font-semibold text-emerald-800 dark:text-emerald-300">一次情報あり</span>;
}

function Row({ e }: { e: EventRow }) {
  return (
    <li className="min-w-0 rounded-xl border border-zinc-200 p-3 dark:border-zinc-800" data-testid="event-row">
      <Link to={`/events/${e.id}`} className="block min-w-0 hover:underline" data-testid="event-link"><h2 className="break-words text-sm font-semibold leading-snug">{e.title}</h2></Link>
      <p className="num mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-zinc-500">
        <span data-testid="event-count">記事 {e.n_articles} 件</span><span>出どころ {e.n_sources}</span>
        {e.has_primary && <PrimaryMark />}
        <span title={time(e.first_at)}>初報 {sinceLabel(hoursSince(e.first_at))}</span>
      </p>
      {e.summary && <p className="mt-1.5 break-words text-xs leading-relaxed text-zinc-700 dark:text-zinc-300" data-testid="event-summary"><span className="mr-1 font-semibold text-amber-700 dark:text-amber-400">{AI_SUMMARY}</span>{e.summary}</p>}
      {e.entities.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1" aria-label="物" data-testid="event-entities">
          {e.entities.map((t) => <li key={t.id}><Link to={`/entities/${t.id}`} title={ENTITY_TYPE_LABEL[t.type] ?? t.type} className="inline-flex min-h-7 items-center rounded-md bg-zinc-100 px-2 text-[11px] hover:bg-zinc-200 dark:bg-zinc-800 dark:hover:bg-zinc-700">{t.name}</Link></li>)}
        </ul>
      )}
    </li>
  );
}

export default function EventsPage() {
  const [sort, setSort] = useState<EventSort>("recent");
  const [days, setDays] = useState(2);
  const [primary, setPrimary] = useState(false);
  const q = useEvents({ sort, days, primary });
  return (
    <div className="mx-auto max-w-3xl space-y-3 p-3 md:p-6" data-testid="events-page">
      <h1 className="text-xl font-semibold tracking-tight">イベント</h1>
      <p className="text-xs leading-relaxed text-zinc-500">同じ出来事を報じる記事のまとまりです（言語をまたいで、題の近さ・取り出した物の重なりで粗くまとめます。別の出来事が混ざることも、同じ出来事が分かれることもあります）。</p>
      <div className="flex flex-col gap-2 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900">
        <Seg label="並び" value={sort} onChange={setSort} items={SORTS} testId="event-sort" />
        <Seg label="期間" value={days} onChange={setDays} items={DAYS} testId="event-days" />
        <label className="flex min-h-9 items-center gap-2 text-xs"><input type="checkbox" checked={primary} onChange={(e) => setPrimary(e.target.checked)} data-testid="event-primary" />一次情報（公式の発表）があるものだけ</label>
      </div>
      {q.isLoading && <p role="status" className="text-sm text-zinc-500">読み込み中…</p>}
      {q.isError && <p role="alert" className="text-sm text-red-600">イベントを読み込めませんでした。</p>}
      {q.data && q.data.items.length === 0 && <p className="rounded-xl border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500 dark:border-zinc-700" data-testid="events-empty">この条件のイベントはまだありません。</p>}
      {q.data && q.data.items.length > 0 && (
        <>
          <p className="num text-xs text-zinc-500" data-testid="events-total">{q.data.total} 件</p>
          <ul className="space-y-2">{q.data.items.map((e) => <Row key={e.id} e={e} />)}</ul>
        </>
      )}
    </div>
  );
}

export function EventPage() {
  const id = Number(useParams().id);
  const q = useEvent(id);
  const [origin, setOrigin] = useState("");
  if (q.isLoading) return <div className="p-6 text-sm text-zinc-500" role="status">読み込み中…</div>;
  if (q.isError || !q.data) return <div className="p-6 text-sm" role="alert">そのイベントは見つかりません。<Link to="/events" className="ml-2 text-accent-700 underline">イベントの一覧へ</Link></div>;
  const d = q.data;
  const shown = d.articles.filter((a) => !origin || a.origin === origin);
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-3 md:p-6" data-testid="event-page">
      <Link to="/events" className="text-xs text-accent-700 underline dark:text-accent-300">← イベントの一覧</Link>
      <header>
        <h1 className="break-words text-xl font-semibold leading-snug tracking-tight" data-testid="event-title">{d.title}</h1>
        <p className="num mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-zinc-500">
          <span>記事 {d.n_articles} 件</span><span>出どころ {d.n_sources}</span>{d.has_primary && <PrimaryMark />}
          <span data-testid="event-since">初報 {sinceLabel(d.hours_since_first)}（{time(d.first_at)}）</span>
        </p>
      </header>
      {d.summary && (
        <Card id="event-summary-card" title={AI_SUMMARY} hint="記事の題と概要だけを根拠に、AI（無料の LLM）が 1 回だけ書きました。間違いを含むことがあります。">
          <p className="break-words text-sm leading-relaxed" data-testid="event-summary">{d.summary}</p>
          {d.summary_model && <p className="mt-1 text-[11px] text-zinc-500">{d.summary_model}</p>}
        </Card>
      )}
      {d.entities.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="物" data-testid="event-entities">
          {d.entities.map((t) => <li key={t.id}><Link to={`/entities/${t.id}`} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{t.name}<span className="num text-zinc-500">{t.n}</span></Link></li>)}
        </ul>
      )}
      <Card id="event-articles" title="関わる記事" hint="公開の時刻順です。出どころの種類で絞れます。">
        <div role="radiogroup" aria-label="出どころの種類" data-testid="event-origins" className="mb-2 flex flex-wrap gap-1.5">
          <button type="button" role="radio" aria-checked={origin === ""} onClick={() => setOrigin("")} className={`min-h-9 rounded-lg border px-3 text-xs ${origin === "" ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>すべて {d.n_articles}</button>
          {d.origins.map((o) => (
            <button key={o.origin} type="button" role="radio" aria-checked={origin === o.origin} onClick={() => setOrigin(o.origin)}
              className={`min-h-9 rounded-lg border px-3 text-xs ${origin === o.origin ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{o.label} {o.n}</button>
          ))}
        </div>
        <ol className="space-y-2" data-testid="event-timeline">
          {shown.map((a) => (
            <li key={a.id} className="min-w-0 border-l-2 border-accent-600/50 pl-3" data-testid="event-article">
              <p className="num flex flex-wrap items-center gap-x-2 text-[11px] text-zinc-500">
                <span>{time(a.published_at)}</span><span>{a.source}</span>
                <span className={`rounded-md px-1.5 py-0.5 font-medium ${a.origin === "official" ? "bg-emerald-600/15 text-emerald-800 dark:text-emerald-300" : "bg-zinc-100 dark:bg-zinc-800"}`}>{a.origin_label}</span>
                {a.is_rep && <span className="font-semibold text-accent-700 dark:text-accent-300">代表</span>}
              </p>
              <p className="break-words text-sm font-medium">{a.url ? <a href={a.url} target="_blank" rel="noopener noreferrer" className="hover:underline">{a.title}</a> : a.title}</p>
              {a.summary && <p className="line-clamp-2 break-words text-xs text-zinc-500">{a.summary}</p>}
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}
