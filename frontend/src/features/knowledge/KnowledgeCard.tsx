import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { REL_LABEL, STATUS_LABEL, useCard, useSaveNote, type CardClaim, type CardRelation, type HistoryEntry, type KnowledgeCardData, type RelType, type RelationGroup, type ThingState } from "../../api/kg";
import { COND_LABEL } from "../../api/watch";
import { useDecideMention } from "../../api/things";
import { ENTITY_TYPE_LABEL } from "../../api/news";
import { Card, field } from "../news/settingsUi";

/** 知識カード（Things のページの上半分）。**形は固定**（context と同じ順）: 先頭の帯（1 行の定義・種類・今の状態・確かさ・別名）→ 概要（AI。夜にまとめて作る）→ 関係（型ごと。畳める）→
 *  主張と根拠（原文に照合できた根拠が先）→ 主な数値（条件つき）→ 未解決の問い・反対の見方 → 変化と矛盾・つなぐ候補 → 出典の数と新しさ・最後に確かめた日 → 利用者のメモ。
 *  AI の部分と利用者のメモは別に保存する。メモは打つとすぐ保存し、再分析・概要の作り直しでは上書きされない。 */
const AI = "AI の取り出し";
const day = (iso: string) => (iso ? new Date(iso).toLocaleDateString("ja-JP", { year: "numeric", month: "numeric", day: "numeric" }) : "—");
const MARK: Record<HistoryEntry["mark"], [string, string]> = {
  first: ["最初", "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"], same: ["同じ", "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"],
  change: ["変化", "bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200"], contradiction: ["矛盾", "bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200"],
  cond_diff: ["条件違い", "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200"],
};
const KIND: Record<string, string> = { release: "公開・版", benchmark: "性能", price: "価格", date: "日付", claim: "主張", other: "その他" };

function Badge({ children, tone = "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`rounded-md px-1.5 py-0.5 text-[11px] font-medium ${tone}`}>{children}</span>;
}

const STATE_TONE: Record<ThingState, string> = {
  official: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200", experimental: "bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200",
  deprecated: "bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200", unknown: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300",
};

/** カードの先頭の帯。確かさは根拠の文書の数だけで決まる（AI の言い分ではない）。 */
function BriefBand({ c }: { c: KnowledgeCardData }) {
  const b = c.brief;
  return (
    <section className="space-y-2 rounded-2xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900" data-testid="card-brief" aria-label="帯">
      <p className="text-sm leading-relaxed" data-testid="brief-definition">
        {b.definition ? <>{b.definition} <span className="text-[11px] font-semibold text-amber-700 dark:text-amber-400">AI</span></> : <span className="text-zinc-500">1 行の定義はまだありません（夜に作ります）。</span>}
      </p>
      <p className="flex flex-wrap items-center gap-1.5">
        <Badge>{ENTITY_TYPE_LABEL[b.type] ?? b.type}</Badge>
        <span data-testid="brief-state" className={`rounded-md px-1.5 py-0.5 text-[11px] font-medium ${STATE_TONE[b.state.key]}`}>{b.state.label}</span>
        <span data-testid="brief-confidence" title="根拠の文書（記事）の数から決めた値です" className="num rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] font-medium dark:bg-zinc-800">確かさ {b.confidence}（{b.confidence_label}）</span>
        {b.aliases.map((a) => <span key={a} className="rounded-md border border-zinc-200 px-1.5 py-0.5 text-[11px] text-zinc-600 dark:border-zinc-700 dark:text-zinc-300">{a}</span>)}
      </p>
    </section>
  );
}

/** カードの AI の部分の末尾: 出典の数と新しさ・最後に確かめた日（固定の形の最後の節）。 */
function SourceFooter({ c }: { c: KnowledgeCardData }) {
  const b = c.brief;
  return <p className="num px-1 text-[11px] text-zinc-500" data-testid="brief-evidence">根拠 {b.evidence_count} 件{b.latest_evidence_at && ` · 最新 ${day(b.latest_evidence_at)}`} · 最後に確かめた日 {b.checked_at ? day(b.checked_at) : "まだ"}</p>;
}

function RelationItem({ r }: { r: CardRelation & { other: { id: number; name: string; type: string; definition?: string } } }) {
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800" data-status={r.status}>
      <span aria-hidden className="text-zinc-400">{r.direction === "out" ? "→" : r.direction === "in" ? "←" : "↔"}</span>
      <Badge>{REL_LABEL[r.type as RelType] ?? r.type}</Badge>
      <Link to={`/entities/${r.other.id}`} className="min-w-0 break-words font-medium hover:underline">{r.other.name}</Link>
      <span className="num ml-auto text-[11px] text-zinc-500">確かさ {r.confidence} · 根拠 {r.evidence_count}</span>
      <Badge tone={r.status === "inferred" ? "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200" : undefined}>{STATUS_LABEL[r.status]}</Badge>
      {r.other.definition && <span className="w-full text-[11px] leading-snug text-zinc-500" data-testid="relation-definition">{r.other.definition}</span>}
    </li>
  );
}

/** 関係を型ごとの節に分ける（ExcaliBrain のように。上位・下位・仲間・使う・使われる・置き換え・矛盾）。節は畳める。 */
function Groups({ groups }: { groups: RelationGroup[] }) {
  return (
    <div className="space-y-2" data-testid="card-relations">
      {groups.map((g) => (
        <details key={g.key} open={g.key !== "other" && g.key !== "peers" ? true : g.count <= 6} data-testid={`group-${g.key}`} className="rounded-xl">
          <summary className="flex min-h-9 cursor-pointer select-none items-center gap-2 text-xs font-semibold text-zinc-600 dark:text-zinc-300">{g.label}<span className="num rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] font-medium dark:bg-zinc-800">{g.count}</span></summary>
          <ul className="mt-1 space-y-1.5">{g.items.map((r) => <RelationItem key={r.id} r={r} />)}</ul>
          {g.more > 0 && <p className="mt-1 text-[11px] text-zinc-500">ほか {g.more} 件（確かさの高い順に上位だけを出しています）</p>}
        </details>
      ))}
    </div>
  );
}

function Questions({ c }: { c: KnowledgeCardData }) {
  return (
    <div className="space-y-3" data-testid="card-questions">
      {c.questions.length > 0 && (
        <div><h3 className="mb-1 text-xs font-semibold text-zinc-500">未解決の問い</h3>
          <ul className="space-y-1">{c.questions.map((q) => <li key={q} className="border-l-2 border-amber-500/70 pl-3 text-sm" data-testid="card-question">{q}</li>)}</ul></div>
      )}
      {c.opposing.length > 0 && (
        <div><h3 className="mb-1 text-xs font-semibold text-zinc-500">反対の見方</h3>
          <ul className="space-y-1">{c.opposing.map((q) => <li key={q} className="border-l-2 border-red-500/70 pl-3 text-sm" data-testid="card-opposing">{q}</li>)}</ul></div>
      )}
    </div>
  );
}

function Metrics({ rows }: { rows: KnowledgeCardData["metrics"] }) {
  return (
    <div className="overflow-x-auto" data-testid="card-metrics">
      <table className="w-full text-left text-sm">
        <thead><tr className="text-[11px] text-zinc-500"><th className="py-1 pr-3 font-medium">項目（条件）</th><th className="py-1 pr-3 font-medium">値</th><th className="py-1 font-medium">出どころ</th></tr></thead>
        <tbody>{rows.map((m, i) => (
          <tr key={i} className="border-t border-zinc-200 align-top dark:border-zinc-800">
            <td className="py-1.5 pr-3 break-words">{m.label}</td><td className="num py-1.5 pr-3 font-medium">{m.value}{m.unit}</td>
            <td className="py-1.5 text-[11px] text-zinc-500">{m.url ? <a href={m.url} target="_blank" rel="noopener noreferrer" className="text-accent-700 underline dark:text-accent-300">記事</a> : m.source === "metric" ? `Metric ${day(m.at)}` : "—"}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Mentions({ c }: { c: KnowledgeCardData }) {
  const decide = useDecideMention(c.id);
  return (
    <div className="space-y-1.5">
      <ul className="space-y-1.5" data-testid="card-mentions">
        {c.mentions.map((m) => (
          <li key={m.article_id} className="flex flex-wrap items-center gap-2 rounded-xl border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800">
            <span className="min-w-0 flex-1 break-words">{m.url ? <a href={m.url} target="_blank" rel="noopener noreferrer" className="hover:underline">{m.title}</a> : m.title}<span className="num ml-2 text-[11px] text-zinc-500">{day(m.published_at)} · 「{m.matched}」</span></span>
            <button type="button" data-testid="mention-confirm" disabled={decide.isPending} onClick={() => decide.mutate({ articleId: m.article_id, accept: true })} className="min-h-9 rounded-lg border border-accent-600 px-3 text-xs font-semibold">つなぐ</button>
            <button type="button" data-testid="mention-dismiss" disabled={decide.isPending} onClick={() => decide.mutate({ articleId: m.article_id, accept: false })} className="min-h-9 rounded-lg border border-zinc-300 px-3 text-xs dark:border-zinc-700">違う</button>
          </li>
        ))}
      </ul>
      {decide.isError && <p role="alert" className="text-xs text-red-700 dark:text-red-400">決められませんでした（管理者でログインしているか確かめてください）。</p>}
    </div>
  );
}

function Claims({ rows }: { rows: CardClaim[] }) {
  return (
    <ul className="space-y-2" data-testid="card-claims">
      {rows.map((c) => (
        <li key={c.id} className="rounded-xl border border-zinc-200 p-3 text-sm dark:border-zinc-800">
          <p className="flex flex-wrap items-center gap-2"><Badge>{KIND[c.kind] ?? c.kind}</Badge>{c.evidences.some((e) => e.grounded) && <span title="根拠の引用が原文に見つかった主張です" data-testid="claim-grounded" className="rounded-md bg-emerald-100 px-1.5 py-0.5 text-[11px] font-medium text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200">原文一致</span>}{c.value && <span className="num rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] dark:bg-zinc-800">{c.value}</span>}<span className="min-w-0 break-words">{c.text}</span></p>
          <details className="mt-1.5 text-[11px] text-zinc-500">
            <summary className="cursor-pointer select-none">根拠 {c.evidences.length} 件{c.ai_extracted && <span className="ml-2 font-semibold text-amber-700 dark:text-amber-400">{AI}</span>}</summary>
            <ul className="mt-1 space-y-1">
              {c.evidences.map((e, i) => (
                <li key={i} className="break-words">
                  {e.document.url ? <a href={e.document.url} target="_blank" rel="noopener noreferrer" className="text-accent-700 underline dark:text-accent-300">{e.document.title || e.document.url}</a> : e.document.title}
                  {e.page ? ` · p.${e.page}` : ""}{e.start != null ? ` · 文字 ${e.start}〜${e.end ?? ""}` : ""}{e.quote && <q className="ml-1 text-zinc-600 dark:text-zinc-300">{e.quote}</q>}
                </li>
              ))}
            </ul>
          </details>
        </li>
      ))}
    </ul>
  );
}

function Note({ id, initial }: { id: number; initial: string }) {
  const save = useSaveNote(id);
  const [text, setText] = useState(initial);
  const [state, setState] = useState<"" | "saving" | "saved" | "error">("");
  const last = useRef(initial);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => { setText(initial); last.current = initial; }, [id, initial]);
  const flush = (value: string) => {
    clearTimeout(timer.current);
    if (value === last.current) return;
    setState("saving");
    save.mutate(value, { onSuccess: () => { last.current = value; setState("saved"); }, onError: () => setState("error") });
  };
  const change = (value: string) => {
    setText(value);
    setState("");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => flush(value), 600);
  };
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <div>
      <textarea aria-label="自分のメモ" data-testid="card-note" rows={5} maxLength={20000} value={text} onChange={(e) => change(e.target.value)} onBlur={() => flush(text)}
        placeholder="このThingsについての自分のメモ（AI の部分とは別に保存します）" className={`${field} h-auto w-full py-2 leading-relaxed`} />
      <p role="status" data-testid="note-state" className="mt-1 min-h-4 text-[11px] text-zinc-500">
        {state === "saving" && "保存中…"}{state === "saved" && "保存しました"}{state === "error" && <span role="alert">保存できませんでした。もう一度試してください。</span>}
      </p>
    </div>
  );
}

export default function KnowledgeCard({ id }: { id: number }) {
  const q = useCard(id);
  if (q.isLoading) return <p role="status" className="text-xs text-zinc-500">知識カードを読み込み中…</p>;
  if (q.isError || !q.data) return null;
  const c = q.data;
  const hist = c.history;
  return (
    <div className="space-y-4" data-testid="knowledge-card">
      <BriefBand c={c} />
      <Card id="card-overview" title="概要" hint="夜にまとめて AI が作ります。変化のあったThingsだけ作り直します。確かめた事実ではありません。">
        {c.overview ? (
          <p className="text-sm leading-relaxed" data-testid="card-overview">{c.overview.text}
            <span className="ml-2 text-[11px] text-zinc-500"><span className="font-semibold text-amber-700 dark:text-amber-400">AI の要約</span> · {c.overview.model} · {day(c.overview.at)}</span></p>
        ) : <p className="text-xs text-zinc-500" data-testid="card-no-overview">まだ概要はありません（記事・主張が増えると、夜に作ります）。</p>}
        {c.elements.topics.length > 0 && (
          <p className="flex flex-wrap gap-1.5" data-testid="card-topics" aria-label="話題の語">{c.elements.topics.map((t) => <Badge key={t}>{t}</Badge>)}</p>
        )}
        <p><Link to={`/map?focus=${c.id}`} data-testid="card-open-map" className="inline-flex min-h-9 items-center rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">知識の地図で見る</Link></p>
      </Card>

      <Card id="card-groups" title="関係（型ごと）" hint="上位・下位・仲間・使う・使われる・置き換え・矛盾に分けています。「推測」は原文に根拠が見つからなかった関係で、確定ではありません。">
        {c.relations.length === 0 ? <p className="text-xs text-zinc-500" data-testid="card-no-relations">関係はまだありません。</p> : <Groups groups={c.groups} />}
      </Card>

      <Card id="card-claims" title="主張と根拠" hint="根拠は複数持てます。文書・位置（ページ・文字の範囲）・短い引用を確かめられます。">
        {c.claims.length === 0 ? <p className="text-xs text-zinc-500">主張はまだありません。</p> : <Claims rows={c.claims} />}
      </Card>

      {c.metrics.length > 0 && (
        <Card id="card-metrics" title="主な数値（条件つき）" hint="値を持つ主張（条件は主張の文）と、Metric の最新の値です。">
          <Metrics rows={c.metrics} />
        </Card>
      )}

      <Card id="card-open" title="未解決の問い・反対の見方" hint="AI が材料から作ります。材料に無い事実は書かせず、根拠が無いことは問いとして出します。">
        {c.questions.length + c.opposing.length === 0 ? <p className="text-xs text-zinc-500" data-testid="card-no-questions">まだありません（夜に作ります）。</p> : <Questions c={c} />}
      </Card>

      {c.mentions.length > 0 && (
        <Card id="card-mentions" title="つなぐ候補（リンクされていない言及）" hint="名前・別名が題や抜粋に出ているのに、この Things とつながっていない記事です。夜にまとめて探します。「つなぐ」で確定します。">
          <Mentions c={c} />
        </Card>
      )}

      <Card id="card-history" title={`変化と矛盾（変化 ${hist.changes} · 矛盾 ${hist.contradictions}${hist.cond_diffs ? ` · 条件違い ${hist.cond_diffs}` : ""}）`} hint="同じ指標（測ったもの）の値を時系列に並べます。単位をそろえ、数％の違いは同じ値です。値が変われば「変化」、同じ時期に別の記事が別の値を述べていれば「矛盾」。量子化・機材などの条件が違うものは矛盾にせず「条件違い」。元の主張は上書きしません。">
        {hist.series.length === 0 ? <p className="text-xs text-zinc-500" data-testid="card-no-history">比べられる値の入った主張はまだありません。</p> : (
          <div className="space-y-3" data-testid="card-history">
            {hist.series.map((g) => (
              <div key={`${g.kind}/${g.dim ?? ""}/${g.metric ?? ""}`} data-testid="history-group">
                <h3 className="mb-1 text-xs font-semibold text-zinc-500" data-testid="history-metric">{g.metric || KIND[g.kind] || g.kind}</h3>
                <ol className="space-y-1">
                  {g.entries.map((e) => (
                    <li key={e.claim_id} data-mark={e.mark} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-l-2 border-zinc-300 pl-3 text-sm dark:border-zinc-700">
                      <span className="num text-[11px] text-zinc-500">{day(e.t)}</span><span className="num font-medium">{e.value}</span>
                      <Badge tone={MARK[e.mark][1]}>{MARK[e.mark][0]}</Badge>{e.from && <span className="text-[11px] text-zinc-500">前は {e.from}</span>}{e.cond && Object.keys(e.cond).length > 0 && <span data-testid="history-cond" className="text-[11px] text-zinc-500">（{Object.entries(e.cond).map(([k, v]) => `${COND_LABEL[k] ?? k} ${v}`).join("・")}）</span>}
                      {e.url && <a href={e.url} target="_blank" rel="noopener noreferrer" className="min-w-0 max-w-full truncate text-[11px] text-accent-700 underline dark:text-accent-300">{e.title}</a>}
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        )}
      </Card>

      <SourceFooter c={c} />

      <Card id="card-note" title="自分のメモ" hint="打つとすぐ保存します。AI の再分析・概要の作り直しでは上書きしません。">
        <Note id={c.id} initial={c.note.body} />
      </Card>
    </div>
  );
}
