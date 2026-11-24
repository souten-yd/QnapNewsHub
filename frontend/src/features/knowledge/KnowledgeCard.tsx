import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { REL_LABEL, STATUS_LABEL, useCard, useSaveNote, type CardClaim, type CardRelation, type HistoryEntry, type RelType } from "../../api/kg";
import { Card, field } from "../news/settingsUi";

/** 知識カード（物のページの上半分）。概要（AI。夜にまとめて作る）・要素・関係・変化と矛盾・主張と根拠・利用者のメモ。
 *  AI の部分と利用者のメモは別に保存する。メモは打つとすぐ保存し、再分析・概要の作り直しでは上書きされない。 */
const AI = "AI の取り出し";
const day = (iso: string) => (iso ? new Date(iso).toLocaleDateString("ja-JP", { year: "numeric", month: "numeric", day: "numeric" }) : "—");
const MARK: Record<HistoryEntry["mark"], [string, string]> = {
  first: ["最初", "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"], same: ["同じ", "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"],
  change: ["変化", "bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200"], contradiction: ["矛盾", "bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200"],
};
const KIND: Record<string, string> = { release: "公開・版", benchmark: "性能", price: "価格", date: "日付", claim: "主張", other: "その他" };

function Badge({ children, tone = "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`rounded-md px-1.5 py-0.5 text-[11px] font-medium ${tone}`}>{children}</span>;
}

function Relations({ rows }: { rows: CardRelation[] }) {
  return (
    <ul className="space-y-1.5" data-testid="card-relations">
      {rows.map((r) => (
        <li key={r.id} className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800" data-status={r.status}>
          <span aria-hidden className="text-zinc-400">{r.direction === "out" ? "→" : r.direction === "in" ? "←" : "↔"}</span>
          <Badge>{REL_LABEL[r.type as RelType] ?? r.type}</Badge>
          <Link to={`/entities/${r.other.id}`} className="min-w-0 break-words font-medium hover:underline">{r.other.name}</Link>
          <span className="num ml-auto text-[11px] text-zinc-500">確かさ {r.confidence} · 根拠 {r.evidence_count}</span>
          <Badge tone={r.status === "inferred" ? "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200" : undefined}>{STATUS_LABEL[r.status]}</Badge>
        </li>
      ))}
    </ul>
  );
}

function Claims({ rows }: { rows: CardClaim[] }) {
  return (
    <ul className="space-y-2" data-testid="card-claims">
      {rows.map((c) => (
        <li key={c.id} className="rounded-xl border border-zinc-200 p-3 text-sm dark:border-zinc-800">
          <p className="flex flex-wrap items-center gap-2"><Badge>{KIND[c.kind] ?? c.kind}</Badge>{c.value && <span className="num rounded-md bg-zinc-100 px-1.5 py-0.5 text-[11px] dark:bg-zinc-800">{c.value}</span>}<span className="min-w-0 break-words">{c.text}</span></p>
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
        placeholder="この物についての自分のメモ（AI の部分とは別に保存します）" className={`${field} h-auto w-full py-2 leading-relaxed`} />
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
      <Card id="card-overview" title="概要" hint="夜にまとめて AI が作ります。変化のあった物だけ作り直します。確かめた事実ではありません。">
        {c.overview ? (
          <p className="text-sm leading-relaxed" data-testid="card-overview">{c.overview.text}
            <span className="ml-2 text-[11px] text-zinc-500"><span className="font-semibold text-amber-700 dark:text-amber-400">AI の要約</span> · {c.overview.model} · {day(c.overview.at)}</span></p>
        ) : <p className="text-xs text-zinc-500" data-testid="card-no-overview">まだ概要はありません（記事・主張が増えると、夜に作ります）。</p>}
        {c.elements.topics.length > 0 && (
          <p className="flex flex-wrap gap-1.5" data-testid="card-topics" aria-label="話題の語">{c.elements.topics.map((t) => <Badge key={t}>{t}</Badge>)}</p>
        )}
        <p><Link to={`/map?focus=${c.id}`} data-testid="card-open-map" className="inline-flex min-h-9 items-center rounded-lg border border-zinc-300 px-3 text-xs hover:border-accent-600 dark:border-zinc-700">知識の地図で見る</Link></p>
      </Card>

      <Card id="card-relations" title="関係" hint="型・向き・確かさ・根拠の数つき。「推測」は原文に根拠が見つからなかった関係で、確定ではありません。">
        {c.relations.length === 0 ? <p className="text-xs text-zinc-500" data-testid="card-no-relations">関係はまだありません。</p> : <Relations rows={c.relations} />}
      </Card>

      <Card id="card-history" title={`変化と矛盾（変化 ${hist.changes} · 矛盾 ${hist.contradictions}）`} hint="同じ種類の主張の値を時系列に並べます。値が変われば「変化」、同じ時期に別の記事が別の値を述べていれば「矛盾」。元の主張は上書きしません。">
        {hist.series.length === 0 ? <p className="text-xs text-zinc-500" data-testid="card-no-history">比べられる値の入った主張はまだありません。</p> : (
          <div className="space-y-3" data-testid="card-history">
            {hist.series.map((g) => (
              <div key={g.kind}>
                <h3 className="mb-1 text-xs font-semibold text-zinc-500">{KIND[g.kind] ?? g.kind}</h3>
                <ol className="space-y-1">
                  {g.entries.map((e) => (
                    <li key={e.claim_id} data-mark={e.mark} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-l-2 border-zinc-300 pl-3 text-sm dark:border-zinc-700">
                      <span className="num text-[11px] text-zinc-500">{day(e.t)}</span><span className="num font-medium">{e.value}</span>
                      <Badge tone={MARK[e.mark][1]}>{MARK[e.mark][0]}</Badge>{e.from && <span className="text-[11px] text-zinc-500">前は {e.from}</span>}
                      {e.url && <a href={e.url} target="_blank" rel="noopener noreferrer" className="min-w-0 max-w-full truncate text-[11px] text-accent-700 underline dark:text-accent-300">{e.title}</a>}
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card id="card-claims" title="主張と根拠" hint="根拠は複数持てます。文書・位置（ページ・文字の範囲）・短い引用を確かめられます。">
        {c.claims.length === 0 ? <p className="text-xs text-zinc-500">主張はまだありません。</p> : <Claims rows={c.claims} />}
      </Card>

      <Card id="card-note" title="自分のメモ" hint="打つとすぐ保存します。AI の再分析・概要の作り直しでは上書きしません。">
        <Note id={c.id} initial={c.note.body} />
      </Card>
    </div>
  );
}
