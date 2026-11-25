import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ENTITY_TYPE_LABEL, relativeTime, useEntities, useEntity, type EntityRow, type EntityType } from "../api/news";
import { MERGE_REASON, useMergeAction, useMergeCandidates } from "../api/things";
import KnowledgeCard from "../features/knowledge/KnowledgeCard";
import { Card, field } from "../features/news/settingsUi";

/** Things（/entities）: 要素の取り出し（AI。無料 LLM）で記事から取れた、モデル・ソフト・組織などの一覧。名前の検索・種類・期間で絞る。
 *  Thingsのページ（/entities/:id）は記事の年表・facts・一緒に出るThings。facts は「AI の取り出し」の印つき（確かめた事実ではない）。 */
const AI_MARK = "AI の取り出し";
const DAY_CHOICES: [number, string][] = [[0, "全期間"], [1, "1 日"], [7, "7 日"], [30, "30 日"]];
const FACT_KIND: Record<string, string> = { release: "公開", benchmark: "性能", price: "価格", date: "日付", claim: "主張", other: "その他" };

const day = (iso: string) => (iso ? new Date(iso).toLocaleDateString("ja-JP", { year: "numeric", month: "numeric", day: "numeric" }) : "—");

function TypeBadge({ type }: { type: EntityType }) {
  return <span className="rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">{ENTITY_TYPE_LABEL[type] ?? type}</span>;
}

function Row({ e, days }: { e: EntityRow; days: number }) {
  return (
    <li>
      <Link to={`/entities/${e.id}`} data-testid="entity-row" className="flex min-h-12 items-center gap-2 rounded-xl border border-zinc-200 px-3 py-2 hover:border-accent-600 dark:border-zinc-800">
        <span className="min-w-0 flex-1 truncate text-sm font-semibold">{e.name}</span>
        <TypeBadge type={e.type} />
        <span className="num shrink-0 text-xs text-zinc-500" title="記事の数">{days ? `${e.period_count} / ` : ""}{e.article_count} 件</span>
        <span className="num hidden shrink-0 text-xs text-zinc-400 sm:inline">{relativeTime(e.last_seen)}</span>
      </Link>
    </li>
  );
}

/** 名寄せの候補（管理者だけ。管理者でなければ 403 で何も出さない）。確かなものは夜に自動で寄せてあり、ここには迷うものと、寄せた記録（元に戻せる）が出る。 */
function MergePanel() {
  const q = useMergeCandidates();
  const act = useMergeAction();
  if (!q.data || (q.data.items.length === 0 && q.data.merges.length === 0)) return null;
  const recent = q.data.merges.filter((m) => !m.undone).slice(0, 5);
  return (
    <Card id="merge-candidates" title={`名寄せの候補（${q.data.total} 件）`} hint="同じ物かもしれない別の表記です。確定すると、記事・主張・関係・別名を 1 つにまとめます（記録が残り、あとで元に戻せます）。確かな表記の違い（Inc.・版の .0 など）は夜に自動で寄せています。">
      <ul className="space-y-1.5" data-testid="merge-list">
        {q.data.items.map((c) => (
          <li key={c.id} data-testid="merge-row" className="flex flex-wrap items-center gap-2 rounded-xl border border-zinc-200 px-3 py-2 text-xs dark:border-zinc-800">
            <span className="min-w-0 flex-1"><span className="font-semibold">{c.keep.name}</span> ← {c.drop.name}<span className="ml-2 text-zinc-500">{MERGE_REASON[c.reason] ?? c.reason}・記事 {c.keep.article_count}／{c.drop.article_count}</span></span>
            <button type="button" data-testid="merge-confirm" disabled={act.isPending} onClick={() => act.mutate({ path: `/merge-candidates/${c.id}/confirm` })} className="min-h-9 rounded-lg border border-accent-600 px-3 font-semibold">寄せる</button>
            <button type="button" data-testid="merge-dismiss" disabled={act.isPending} onClick={() => act.mutate({ path: `/merge-candidates/${c.id}/dismiss` })} className="min-h-9 rounded-lg border border-zinc-300 px-3 dark:border-zinc-700">別の物</button>
          </li>
        ))}
      </ul>
      {recent.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-zinc-500" data-testid="merge-history">
          {recent.map((m) => (
            <li key={m.id} className="flex items-center gap-2"><span className="min-w-0 flex-1 truncate">{m.loser.name} → {m.winner.name}（{m.mode === "auto" ? "自動" : "確定"}）</span>
              {m.can_undo && <button type="button" data-testid="merge-undo" disabled={act.isPending} onClick={() => act.mutate({ path: `/merges/${m.id}/undo` })} className="min-h-9 rounded-lg border border-zinc-300 px-3 dark:border-zinc-700">元に戻す</button>}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export default function EntitiesPage() {
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [days, setDays] = useState(0);
  const [sort, setSort] = useState<"count" | "recent">("count");
  const list = useEntities({ q, type, days, sort });
  return (
    <div className="mx-auto max-w-3xl space-y-3 p-3 md:p-6" data-testid="entities-page">
      <h1 className="text-xl font-semibold tracking-tight">Things</h1>
      <p className="text-xs leading-relaxed text-zinc-500">記事から AI（無料の LLM）が取り出した、モデル・ソフト・組織などの一覧です。{AI_MARK}なので、間違いを含みます。</p>
      <p className="flex flex-wrap gap-2 text-xs">
        <Link to="/moc" data-testid="things-moc-link" className="inline-flex min-h-9 items-center rounded-lg border border-zinc-300 px-3 hover:border-accent-600 dark:border-zinc-700">主題の入口（まとまり・チャンネル）</Link>
        <Link to="/map" data-testid="things-map-link" className="inline-flex min-h-9 items-center rounded-lg border border-zinc-300 px-3 hover:border-accent-600 dark:border-zinc-700">知識の地図（全体）</Link>
      </p>
      <MergePanel />
      <div className="grid gap-2 sm:grid-cols-4">
        <input type="search" aria-label="Thingsを探す" placeholder="名前で探す" value={q} onChange={(e) => setQ(e.target.value)} className={`${field} sm:col-span-2`} />
        <select aria-label="種類" value={type} onChange={(e) => setType(e.target.value)} className={field}>
          <option value="">すべての種類</option>
          {(Object.keys(ENTITY_TYPE_LABEL) as EntityType[]).map((t) => <option key={t} value={t}>{ENTITY_TYPE_LABEL[t]}</option>)}
        </select>
        <select aria-label="期間" value={days} onChange={(e) => setDays(Number(e.target.value))} className={field}>
          {DAY_CHOICES.map(([n, label]) => <option key={n} value={n}>{label}</option>)}
        </select>
      </div>
      <div role="radiogroup" aria-label="並び" className="flex gap-1.5">
        {([["count", "記事の多い順"], ["recent", "最近出た順"]] as const).map(([id, label]) => (
          <button key={id} type="button" role="radio" aria-checked={sort === id} onClick={() => setSort(id)}
            className={`min-h-9 rounded-lg border px-3 text-xs ${sort === id ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{label}</button>
        ))}
      </div>
      {list.isLoading && <p role="status" className="text-sm text-zinc-500">読み込み中…</p>}
      {list.isError && <p role="alert" className="text-sm">Thingsを読み込めませんでした。</p>}
      {list.data && list.data.items.length === 0 && (
        <p className="rounded-xl border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500 dark:border-zinc-700" data-testid="entities-empty">
          まだThingsがありません。OpenRouter の鍵を入れると、新しい記事から順に取り出します。
        </p>
      )}
      {list.data && list.data.items.length > 0 && (
        <>
          <p className="num text-xs text-zinc-500" data-testid="entities-total">{list.data.total} 件{days ? `（左の数は直近 ${days} 日の記事）` : ""}</p>
          <ul className="space-y-1.5">{list.data.items.map((e) => <Row key={e.id} e={e} days={days} />)}</ul>
        </>
      )}
    </div>
  );
}

export function EntityPage() {
  const id = Number(useParams().id);
  const q = useEntity(id);
  if (q.isLoading) return <div className="p-6 text-sm text-zinc-500" role="status">読み込み中…</div>;
  if (q.isError || !q.data) return <div className="p-6 text-sm" role="alert">そのThingsは見つかりません。<Link to="/entities" className="ml-2 text-accent-700 underline">Thingsの一覧へ</Link></div>;
  const d = q.data;
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-3 md:p-6" data-testid="entity-page">
      <Link to="/entities" className="text-xs text-accent-700 underline dark:text-accent-300">← Thingsの一覧</Link>
      <header>
        <h1 className="break-words text-xl font-semibold tracking-tight" data-testid="entity-name">{d.name}</h1>
        <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
          <TypeBadge type={d.type} /><span className="num">記事 {d.article_count} 件 · 初出 {day(d.first_seen)} · 最後 {day(d.last_seen)}</span>
        </p>
        {d.aliases.length > 0 && <p className="mt-1 text-xs text-zinc-500">別の表記: {d.aliases.join("、")}</p>}
      </header>

      <KnowledgeCard id={id} />

      <Card id="entity-facts" title={`facts（${AI_MARK}）`} hint="記事の題・抜粋に書かれていることだけを AI が取り出したものです。確かめた事実ではないので、元の記事で確かめてください。">
        {d.facts.length === 0 ? <p className="text-xs text-zinc-500" data-testid="entity-no-facts">取り出した fact はありません。</p> : (
          <ul className="space-y-2" data-testid="entity-facts">
            {d.facts.map((f) => (
              <li key={f.id} className="rounded-xl border border-zinc-200 p-3 text-sm dark:border-zinc-700">
                <p className="flex flex-wrap items-center gap-2">
                  <span className="rounded-md bg-accent-600/10 px-1.5 py-0.5 text-[11px] font-semibold text-accent-700 dark:text-accent-300">{FACT_KIND[f.kind] ?? f.kind}</span>
                  {f.value && <span className="num rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] dark:bg-zinc-800">{f.value}</span>}
                  <span className="min-w-0 break-words">{f.text}</span>
                </p>
                <p className="mt-1 text-[11px] text-zinc-500">
                  {f.ai_extracted && <span data-testid="ai-mark" className="mr-1 font-semibold text-amber-700 dark:text-amber-400">{AI_MARK}</span>}
                  {f.model} · {day(f.published_at)} · {f.article_title}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card id="entity-timeline" title="記事の年表" hint="このThingsが出た記事を、新しい順に並べます。">
        <ol className="space-y-2" data-testid="entity-timeline">
          {d.timeline.map((a) => (
            <li key={a.id} className="border-l-2 border-accent-600/50 pl-3">
              <p className="num text-[11px] text-zinc-500">{day(a.published_at)} · {a.source}</p>
              <p className="break-words text-sm font-medium">{a.url ? <a href={a.url} target="_blank" rel="noopener noreferrer" className="hover:underline">{a.title}</a> : a.title}</p>
              {a.summary && <p className="line-clamp-2 text-xs text-zinc-500">{a.summary}</p>}
            </li>
          ))}
        </ol>
      </Card>

      {d.related.length > 0 && (
        <Card id="entity-related" title="一緒に出るThings" hint="同じ記事に出た回数の多い順です。">
          <ul className="flex flex-wrap gap-1.5" data-testid="entity-related">
            {d.related.map((r) => (
              <li key={r.id}><Link to={`/entities/${r.id}`} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">{r.name}<span className="num text-zinc-500">{r.count}</span></Link></li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
