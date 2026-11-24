import { useDiagnostics, useFlushNow, useSelfCheck, type DiagPoint, type Diagnostics } from "../api/diag";
import { useToasts } from "../stores";
import { Card, ghost, primary } from "../features/news/settingsUi";

const bytes = (n: number) => (n >= 1 << 30 ? `${(n / (1 << 30)).toFixed(1)} GB` : n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1 << 10 ? `${(n / (1 << 10)).toFixed(0)} KB` : `${n} B`);
const clock = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const dur = (s: number) => (s >= 86400 ? `${Math.floor(s / 86400)} 日 ${Math.floor((s % 86400) / 3600)} 時間` : s >= 3600 ? `${Math.floor(s / 3600)} 時間 ${Math.floor((s % 3600) / 60)} 分` : `${Math.floor(s / 60)} 分`);
const QUOTA_LABEL: Record<string, string> = { prep: "下ごしらえと取り出し", gist: "要約", digest: "まとめ", card: "知識カード", trend: "傾向の解説", spare: "予備（借りた数）" };
const REASONS: Record<string, string> = { http: "HTTP エラー", timeout: "時間切れ", xml: "XML・形の異常", robots: "robots.txt", other: "その他" };

function Row({ k, v, id }: { k: string; v: React.ReactNode; id?: string }) {
  return <div className="flex flex-wrap justify-between gap-x-4 border-b border-zinc-100 py-1.5 text-xs last:border-0 dark:border-zinc-800" data-testid={id}><dt className="text-zinc-500">{k}</dt><dd className="num min-w-0 break-words text-right font-medium">{v}</dd></div>;
}

/** 直近 24 時間の書き込み量（5 分ごとの棒）。ここの数値はメモリだけ（ディスクへは書かない）。 */
function Bars({ points }: { points: DiagPoint[] }) {
  const max = Math.max(1, ...points.map((p) => p.write_bytes));
  return (
    <svg viewBox={`0 0 ${Math.max(points.length, 1) * 3} 40`} preserveAspectRatio="none" role="img" aria-label="直近 24 時間の書き込み量の推移" className="mt-2 h-16 w-full text-accent-600" data-testid="io-chart">
      {points.map((p, i) => <rect key={p.t} x={i * 3} width={2} y={40 - (p.write_bytes / max) * 38 - (p.write_bytes ? 1 : 0)} height={(p.write_bytes / max) * 38 + (p.write_bytes ? 1 : 0)} fill="currentColor"><title>{`${clock(p.t)} ${bytes(p.write_bytes)}`}</title></rect>)}
      <line x1="0" x2={Math.max(points.length, 1) * 3} y1="39.5" y2="39.5" stroke="currentColor" strokeOpacity="0.25" strokeWidth="1" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Problems({ d }: { d: Diagnostics }) {
  if (d.problems.length === 0) return <p role="status" data-testid="diag-ok" className="rounded-xl bg-emerald-500/10 px-3 py-2 text-xs font-medium text-emerald-800 dark:text-emerald-300">いま気づいている問題はありません</p>;
  return (
    <ul className="space-y-1.5" data-testid="diag-problems">
      {d.problems.map((p) => <li key={p.text} className={`rounded-xl border px-3 py-2 text-xs font-medium ${p.level === "error" ? "border-red-500/50 bg-red-500/10 text-red-800 dark:text-red-300" : "border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-300"}`}>{p.text}</li>)}
    </ul>
  );
}

function SelfCheckCard() {
  const run = useSelfCheck();
  const toast = useToasts((s) => s.show);
  return (
    <Card id="diag-self" title="自己点検" hint="データベースの整合・全文検索の索引・LLM の鍵・時計を確かめます（外へは通信しません）。">
      <button type="button" className={primary} disabled={run.isPending} onClick={() => run.mutate(undefined, { onError: (e) => toast(e instanceof Error ? e.message : "点検できませんでした", "error") })}>{run.isPending ? "点検中…" : "点検する"}</button>
      {run.data && (
        <ul className="space-y-1.5" data-testid="selfcheck" role="status">
          {run.data.checks.map((c) => (
            <li key={c.name} className={`rounded-xl border px-3 py-2 text-xs ${c.ok ? "border-zinc-200 dark:border-zinc-700" : "border-red-500/50 bg-red-500/10"}`}>
              <span className="font-semibold">{c.ok ? "✓ " : "✗ "}{c.name}</span><span className="text-zinc-500"> — {c.detail}</span>
              {!c.ok && c.fix && <p className="mt-0.5 font-medium">直し方: {c.fix}</p>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export default function DiagnosticsPage() {
  const q = useDiagnostics();
  const flush = useFlushNow();
  const toast = useToasts((s) => s.show);
  if (q.isLoading) return <div className="p-6 text-sm text-zinc-500" role="status">読み込み中…</div>;
  if (q.isError || !q.data) return <div className="p-6 text-sm" role="alert">診断を読み込めませんでした。{q.error instanceof Error ? q.error.message : ""}</div>;
  const d = q.data, s = d.storage, m = d.machine, l = d.llm;
  const stalled = d.fetch.sources.filter((x) => x.stalled);
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-3 md:p-6" data-testid="diag-page">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold tracking-tight">診断</h1>
        <a href="/api/v1/diagnostics/bundle" download className={`${ghost} ml-auto inline-flex items-center`} data-testid="bundle-link">診断の束を書き出す</a>
      </div>
      <Problems d={d} />

      <Card id="diag-fetch" title="取得" hint="取得先ごとの成否・理由・最終成功。止まった可能性のあるものを先に並べます。">
        <dl><Row k="有効な取得先" v={`${d.fetch.total} 件`} /><Row k="止まった可能性" v={`${d.fetch.stalled} 件`} id="fetch-stalled" />
          <Row k="304 の当たり率（条件付きの取得）" v={d.fetch.hit_rate === null ? "まだ計測なし" : `${d.fetch.hit_rate}%（${d.fetch.hits_304} / ${d.fetch.conditional} 回）`} id="fetch-hit-rate" />
          <Row k="失敗で間隔を延ばしている" v={`${d.fetch.backoff} 件`} /></dl>
        <ul className="space-y-1.5">
          {(stalled.length ? stalled : d.fetch.sources.slice(0, 5)).map((x) => (
            <li key={x.key} className={`rounded-xl border px-3 py-2 text-xs ${x.stalled ? "border-amber-500/50 bg-amber-500/10" : "border-zinc-200 dark:border-zinc-700"}`}>
              <p className="font-semibold">{x.name} <span className="font-normal text-zinc-500">{x.ok === false ? "失敗" : "成功"}{x.reason ? `（${REASONS[x.reason] ?? x.reason}）` : ""}</span></p>
              <p className="num text-zinc-500">最終成功 {clock(x.last_ok_at)} · 間隔 {x.interval_min} 分（{x.backoff ? "失敗で延長" : x.interval_auto ? "自動" : "固定"}） ·{x.hit_rate !== null ? ` 304 の当たり ${x.hit_rate}% ·` : ""} 7 日の新着 {x.new_7d} 件</p>
              {x.problems.map((p) => <p key={p} className="font-medium">{p}</p>)}
              {x.fix && <p className="text-zinc-600 dark:text-zinc-300">直し方: {x.fix}</p>}
              {x.error && <p className="break-words text-zinc-500">{x.error}</p>}
            </li>
          ))}
        </ul>
      </Card>

      <Card id="diag-llm" title="LLM" hint="今日の回数・モデルごとの成功率・429（混み合い）・まだ下ごしらえしていない記事。">
        <dl>
          <Row k="今日の回数" v={`${l.total} / ${l.daily_limit}（${Object.entries(l.calls).map(([k, n]) => `${k} ${n}`).join("・")}）`} id="llm-total" />
          <Row k="失敗" v={`${l.failed} 回${l.last_error ? ` · ${l.last_error}` : ""}`} />
          <Row k="429（混み合い）" v={`${l.rate_limited} 回`} />
          <Row k="未処理の記事" v={`${l.unprocessed} 件`} id="llm-unprocessed" />
          <Row k="鍵" v={l.key_set ? "入っています" : "未設定"} />
        </dl>
        <div data-testid="llm-quota" className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700">
          <p className="font-semibold">用途ごとの今日の使い方（使った / 割り当て）</p>
          <ul className="num mt-1 grid gap-x-4 sm:grid-cols-2">
            {Object.entries(l.quota).map(([k, n]) => (
              <li key={k} className="flex justify-between"><span className="text-zinc-500">{QUOTA_LABEL[k] ?? k}</span><span>{k === "spare" ? l.spare_used : (l.calls[k] ?? 0)} / {n}</span></li>
            ))}
          </ul>
          <p className="num mt-1 text-zinc-500">夜（{l.night_from} 時以降）の後回しに使える残り {l.leftover} 回 · 後回しで使った {l.calls.backfill ?? 0} 回 · 取り出し待ちの記事 {l.extract_waiting} 件{l.extract_enabled ? "" : "（取り出しは切）"}</p>
        </div>
        <dl>
          <Row k="いま使うモデル" v={<span className="font-mono">{l.chain.current || l.prep_model}</span>} id="llm-current-model" />
          <Row k="モデルの並び" v={<span className="font-mono">{l.chain.chain.join(" → ") || l.prep_model}</span>} />
          {l.chain.return_in_seconds !== null && <Row k="1 番目へ戻すまで" v={`${Math.ceil(l.chain.return_in_seconds / 60)} 分`} />}
          <Row k="ゲートウェイ" v={!l.gateway.enabled ? "使わない" : l.chain.gateway_down_in_seconds !== null ? `つながらないため、あと約 ${Math.floor(l.chain.gateway_down_in_seconds / 60) + 1} 分は外しています` : `使う（今日 ${l.gateway.calls} 回・1 回 ${l.gateway.batch} 件）`} id="llm-gateway" />
          <Row k="取り出した物・fact・記事" v={`${l.knowledge.entities} 物・${l.knowledge.claims} fact・${l.knowledge.extracted_articles} 記事`} id="llm-knowledge" />
        </dl>
        {l.chain.events.length > 0 && <ul className="num text-xs text-zinc-500" data-testid="llm-chain-events">{l.chain.events.map((e) => <li key={e.at + e.kind}>{clock(e.at)} {e.kind === "switch" ? "切り替え" : e.kind === "down" ? "ゲートウェイを外す" : "戻し"} → <span className="font-mono">{e.model}</span> {e.reason}</li>)}</ul>}
        {l.models.length > 0 && <ul className="space-y-1">{l.models.map((x) => <li key={x.model} className="num text-xs"><span className="rounded bg-zinc-100 px-1 dark:bg-zinc-800">{x.provider === "gateway" ? "ゲートウェイ" : "OpenRouter"}</span> <span className="font-mono">{x.model.replace(/^gateway:/, "")}</span>{x.paid ? " （有料）" : ""} — 成功率 {x.success_pct ?? "—"}%（{x.ok}/{x.ok + x.fail}）· 日本語 {x.ja_pct ?? "—"}% · 429 {x.rate_limited} 回 · 平均 {x.avg_ms} ms · 最後に成功 {clock(x.last_ok_at)}</li>)}</ul>}
      </Card>

      <Card id="diag-storage" title="保存と I/O" hint="書き込みは 30 分ごとのまとめ書きが基本です。数値はメモリにあり、ディスクへは 1 日 1 回だけ要約を書きます。">
        <dl>
          <Row k="プロセスの書き込み（起動後の合計）" v={bytes(s.io.write_bytes)} id="io-write" />
          <Row k="プロセスの読み込み（起動後の合計）" v={bytes(s.io.read_bytes)} />
          <Row k="最終保存" v={clock(s.last_flush_at)} id="last-flush" />
          <Row k="次の保存の予定" v={s.rest.active && s.rest.mode !== "flush_at_limit" ? "お休みが明けたとき" : clock(s.next_flush_at)} />
          <Row k="未保存" v={`記事 ${s.unsaved.items} 件・更新 ${s.unsaved.updates + s.unsaved.patches} 件・取得の履歴 ${s.unsaved.fetchlog} 件`} id="unsaved" />
          <Row k="DB / WAL" v={`${bytes(s.files.db)} / ${bytes(s.files.wal)}`} id="db-size" />
          <Row k="ディスクの空き" v={`${bytes(s.disk.free)}（全体 ${bytes(s.disk.total)}）`} />
          <Row k="お休みモード" v={s.rest.enabled ? (s.rest.active ? `お休み中（${s.rest.ends_at?.slice(11, 16) ?? ""} まで）` : `入（${s.rest.start}〜${s.rest.end}）`) : "切"} id="rest-state" />
        </dl>
        <Bars points={s.series} />
        <p className="text-[11px] text-zinc-500">直近 24 時間の書き込み量（5 分ごと。{s.series.length} 点）</p>
        {s.rest_writes.length > 0 && (
          <div data-testid="rest-writes" className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700">
            <p className="font-semibold">お休み中の書き込み（処理名）</p>
            <ul>{s.rest_writes.map((w, i) => <li key={i} className="num">{clock(w.at)} {w.name} <span className="text-zinc-500">{w.user ? "あなたの操作" : "アプリが原因"}</span></li>)}</ul>
          </div>
        )}
        <button type="button" className={ghost} disabled={flush.isPending} onClick={() => flush.mutate(undefined, { onSuccess: () => toast("保存しました", "success"), onError: (e) => toast(e instanceof Error ? e.message : "保存できませんでした", "error") })}>いますぐ保存する</button>
      </Card>

      <Card id="diag-machine" title="機械">
        <dl>
          <Row k="CPU（直近）" v={`${m.cpu_pct.toFixed(1)} %`} id="cpu" />
          <Row k="負荷（1・5・15 分）" v={m.load.join(" / ") || "—"} />
          <Row k="常駐メモリ（RSS）" v={bytes(m.rss)} id="rss" />
          <Row k="稼働時間" v={dur(m.uptime_seconds)} />
          <Row k="版" v={`${m.version}（Python ${m.python}）`} />
        </dl>
      </Card>

      <Card id="diag-update" title="更新" hint="新しい版の確認（GitHub の Release）と、前回の更新の結果です。">
        <dl>
          <Row k="今の版" v={`${d.update.version}（${d.update.install === "linux" ? "Linux・手動で更新" : "QPKG"}）`} id="update-version" />
          <Row k="新しい版" v={d.update.latest || "なし"} id="update-latest-diag" />
          <Row k="最後に確かめた" v={clock(d.update.checked_at)} />
          <Row k="確認の失敗" v={d.update.error || "なし"} />
          <Row k="ETag の当たり（GitHub の制限の節約）" v={`${d.update.etag_hits} / ${d.update.checks} 回`} />
          <Row k="前回の更新" v={d.update.last ? `${d.update.last.ok ? "成功" : "失敗"}（${d.update.last.from} → ${d.update.last.to}）` : "なし"} id="update-last-diag" />
        </dl>
      </Card>

      <SelfCheckCard />
    </div>
  );
}
