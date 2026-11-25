import { Link, useParams } from "react-router-dom";
import { ENTITY_TYPE_LABEL } from "../api/news";
import { useMoc, useMocIndex } from "../api/things";
import { Card } from "../features/news/settingsUi";

/** 主題の入口（MOC。Map of Content）。/moc は入口の一覧（まとまりごと・チャンネルごと）、/moc/community/:id・/moc/channel/:id は 1 つの入口。
 *  主要な Things・最近の出来事・未解決の問いを並べる。まとまりは夜に関係から計算し、入口の中身は読むときに集める（自動生成）。 */
const day = (iso: string) => (iso ? new Date(iso).toLocaleDateString("ja-JP", { month: "numeric", day: "numeric" }) : "");

export function MocIndexPage() {
  const q = useMocIndex();
  return (
    <div className="mx-auto min-w-0 max-w-3xl space-y-3 p-3 md:p-6" data-testid="moc-index">
      <Link to="/entities" className="text-xs text-accent-700 underline dark:text-accent-300">← Things の一覧</Link>
      <h1 className="text-xl font-semibold tracking-tight">主題の入口</h1>
      <p className="text-xs leading-relaxed text-zinc-500">調べ始める入口です。まとまりは関係（一緒に出る・型つき）から夜に自動で作ります。チャンネルは設定の検索テーマです。</p>
      {q.isLoading && <p role="status" className="text-sm text-zinc-500">読み込み中…</p>}
      {q.isError && <p role="alert" className="text-sm">入口を読み込めませんでした。</p>}
      {q.data && (
        <>
          <Card id="moc-channels" title="チャンネルごと" hint="設定の検索テーマごとに、最近の記事に出た Things をまとめます。">
            <ul className="flex flex-wrap gap-1.5" data-testid="moc-channels">{q.data.channels.map((c) => (
              <li key={c.id}><Link to={`/moc/channel/${c.id}`} data-testid="moc-channel" className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700"><span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: c.color }} />{c.name}</Link></li>
            ))}</ul>
          </Card>
          <Card id="moc-communities" title="まとまりごと" hint="関係で強くつながる Things の集まりです。名前は中心の Things です。">
            {q.data.communities.length === 0 ? <p className="text-xs text-zinc-500" data-testid="moc-no-communities">まだまとまりがありません（夜に計算します）。</p> : (
              <ul className="flex flex-wrap gap-1.5" data-testid="moc-communities">{q.data.communities.map((c) => (
                <li key={c.id}><Link to={`/moc/community/${c.id}`} data-testid="moc-community" className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{c.name}<span className="num text-zinc-500">{c.size}</span></Link></li>
              ))}</ul>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

export function MocPage() {
  const { kind = "", id = "" } = useParams();
  const q = useMoc(kind, id);
  if (q.isLoading) return <div className="p-6 text-sm text-zinc-500" role="status">読み込み中…</div>;
  if (q.isError || !q.data) return <div className="p-6 text-sm" role="alert">その入口は見つかりません。<Link to="/moc" className="ml-2 text-accent-700 underline">入口の一覧へ</Link></div>;
  const m = q.data;
  return (
    <div className="mx-auto min-w-0 max-w-3xl space-y-4 p-3 md:p-6" data-testid="moc-page">
      <Link to="/moc" className="text-xs text-accent-700 underline dark:text-accent-300">← 主題の入口</Link>
      <header>
        <h1 className="break-words text-xl font-semibold tracking-tight" data-testid="moc-title">{m.title}</h1>
        <p className="num mt-1 text-xs text-zinc-500">{m.kind === "community" ? `Things ${m.size} 件のまとまり` : `最近 30 日の記事 ${m.size} 件`}</p>
        {m.kind === "community" && <p className="mt-2"><Link to={`/map?community=${m.id}`} data-testid="moc-open-map" className="inline-flex min-h-9 items-center rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">知識の地図で見る</Link></p>}
      </header>
      <Card id="moc-things" title="主要な Things" hint="記事の多い順です。1 行の定義は AI が夜に作ります。">
        <ul className="space-y-1.5" data-testid="moc-things">{m.things.map((t) => (
          <li key={t.id}><Link to={`/entities/${t.id}`} className="block rounded-xl border border-zinc-200 px-3 py-2 hover:border-accent-600 dark:border-zinc-800">
            <span className="flex items-center gap-2"><span className="min-w-0 flex-1 truncate text-sm font-semibold">{t.name}</span>
              <span className="rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">{ENTITY_TYPE_LABEL[t.type] ?? t.type}</span>
              <span className="num shrink-0 text-xs text-zinc-500">{t.recent_articles ?? t.article_count} 件</span></span>
            {t.definition && <span className="mt-0.5 block text-[11px] leading-snug text-zinc-500">{t.definition}</span>}
          </Link></li>
        ))}</ul>
      </Card>
      <Card id="moc-events" title="最近の出来事">
        {m.events.length === 0 ? <p className="text-xs text-zinc-500" data-testid="moc-no-events">最近の出来事はありません。</p> : (
          <ul className="space-y-1.5" data-testid="moc-events">{m.events.map((e) => (
            <li key={e.id}><Link to={`/events/${e.id}`} className="block rounded-xl border border-zinc-200 px-3 py-2 text-sm hover:border-accent-600 dark:border-zinc-800"><span className="num mr-2 text-[11px] text-zinc-500">{day(e.last_at)}</span>{e.title}<span className="num ml-2 text-[11px] text-zinc-500">記事 {e.n_articles}</span></Link></li>
          ))}</ul>
        )}
      </Card>
      <Card id="moc-questions" title="未解決の問い" hint="この入口の Things のカードにある問いです。">
        {m.questions.length === 0 ? <p className="text-xs text-zinc-500" data-testid="moc-no-questions">まだありません。</p> : (
          <ul className="space-y-1.5" data-testid="moc-questions">{m.questions.map((x, i) => (
            <li key={i} className="border-l-2 border-amber-500/70 pl-3 text-sm"><Link to={`/entities/${x.entity_id}`} className="mr-2 text-[11px] font-semibold text-accent-700 underline dark:text-accent-300">{x.entity}</Link>{x.question}</li>
          ))}</ul>
        )}
      </Card>
    </div>
  );
}
