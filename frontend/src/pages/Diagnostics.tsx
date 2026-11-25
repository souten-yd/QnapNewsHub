import { useDiagnostics, useFlushNow, useRebuildSearch, useSelfCheck, type DiagPoint, type Diagnostics } from "../api/diag";
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
  const rebuild = useRebuildSearch();
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
          <Row k="取り出したThings・fact・記事" v={`${l.knowledge.entities} Things・${l.knowledge.claims} fact・${l.knowledge.extracted_articles} 記事`} id="llm-knowledge" />
          {l.knowledge.communities !== undefined && <Row k="Things の脳（定義つきカード・まとまり・つなぐ候補）" v={`${l.knowledge.cards ?? 0} カード・${l.knowledge.communities} まとまり・${l.knowledge.mention_candidates ?? 0} 候補・名寄せ ${l.knowledge.merged ?? 0} 件（確認待ち ${l.knowledge.merge_candidates ?? 0}）`} id="llm-things" />}
          {l.cards && <Row k="知識カードの仕事（夜）" id="llm-cards" v={[
            l.cards.blocked ? `止まっています（${l.cards.blocked}）` : l.cards.window_open ? "いま走れます（次の取得の回）" : `次は ${l.cards.next_run_at.slice(0, 16).replace("T", " ")} 以降の取得の回`,
            l.cards.last ? `最後 ${l.cards.last.at.slice(0, 16).replace("T", " ")}: 作成 ${l.cards.last.done}・検査落ち ${l.cards.last.failed}・呼び出し ${l.cards.last.calls}${l.cards.last.error ? `・失敗「${l.cards.last.error}」（数えずにやり直し）` : ""}` : "まだ走っていません",
            `概要の無い Things ${l.cards.pending} 件・作成済み ${l.cards.made}・同じ材料で ${l.cards.max_tries} 回落ちて諦めた ${l.cards.gave_up}`].join(" / ")} />}
          {l.events && <Row k="イベント・要約・今日の傾向" v={`${l.events.events} イベント・要約済み ${l.events.summarized}・待ち ${l.events.summary_waiting}・諦め ${l.events.summary_gave_up}・傾向の解説 ${l.events.trend_note_day ? `${l.events.trend_note_day}（${(l.events.trend_note_slot ?? 0) === 0 ? "朝" : "夕"}）` : "まだ"}`} id="llm-events" />}
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

      <Card id="diag-search" title="検索と本文・圧縮" hint="日本語の部分一致（trigram）と英語の索引、本文の取り出し（Trafilatura・Zstandard）、古い記事の圧縮の状態です。">
        <dl>
          <Row k="検索の索引" v={!d.search.index.available ? "使えません（LIKE で検索）" : d.search.index.building ? `作り直し中（${d.search.index.cursor} / ${d.search.index.hi}）` : `日本語 ${d.search.index.docs?.tri ?? 0} 件・英語 ${d.search.index.docs?.word ?? 0} 件（記事 ${d.search.index.articles} 件）`} id="search-index" />
          <Row k="本文の取り出し（今日）" v={d.search.body.enabled ? `${d.search.body.today} / ${d.search.body.per_day} 件（1 分 ${d.search.body.per_minute} 件まで）` : "切"} id="body-today" />
          <Row k="本文の成否（合計）" v={`取れた ${d.search.body.ok} 件・取れない ${d.search.body.failed} 件`} id="body-result" />
          {Object.keys(d.search.body.reasons).length > 0 && <Row k="取れない理由" v={Object.entries(d.search.body.reasons).map(([r, n]) => `${d.search.body.reason_labels[r] ?? r} ${n}`).join("・")} id="body-reasons" />}
          <Row k="本文の圧縮の効果" v={d.search.body.raw_bytes ? `${bytes(d.search.body.raw_bytes)} → ${bytes(d.search.body.packed_bytes)}（${Math.round(d.search.body.packed_bytes * 100 / d.search.body.raw_bytes)}%）` : "まだありません"} id="body-pack" />
          <Row k="古い記事の抜粋の圧縮" v={d.search.compress_years === 0 ? "しない設定" : `${d.search.compress_years} 年より古い ${d.search.body.cold.items} 件${d.search.body.cold.raw_bytes ? `（${bytes(d.search.body.cold.raw_bytes)} → ${bytes(d.search.body.cold.packed_bytes)}）` : ""}`} id="cold-pack" />
        </dl>
        <button type="button" className={ghost} disabled={rebuild.isPending || d.search.index.building} data-testid="rebuild-search"
          onClick={() => rebuild.mutate(undefined, { onSuccess: () => toast("索引の作り直しを予約しました（1 分ごとに 2 万件ずつ進みます）", "success"), onError: (e) => toast(e instanceof Error ? e.message : "予約できませんでした", "error") })}>検索の索引を作り直す</button>
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

      {d.link && (
        <Card id="diag-link" title="つないだ ControlDeck" hint="つないだ相手が最後に来た時刻と、仕事の列の状態です。期限切れの仕事は、次に借りるときに列へ戻ります。">
          <dl data-testid="diag-link">
            <Row k="つないでいる相手" v={d.link.peers.length ? d.link.peers.map((p) => p.name).join("・") : "なし"} />
            <Row k="最後に来た時刻" v={clock(d.link.last_seen_at)} id="link-last-seen" />
            <Row k="借りている仕事" v={`${d.link.jobs.leased} 件`} id="link-leased" />
            <Row k="期限切れの仕事" v={`${d.link.jobs.expired} 件`} id="link-expired" />
            <Row k="待ち・終わらなかった・24 時間の完了" v={`${d.link.jobs.queued} · ${d.link.jobs.dead} · ${d.link.jobs.done_24h}`} />
            <Row k="受け取った鍵" v={d.link.secrets.length ? d.link.secrets.map((k) => `${k.name}（${clock(k.received_at)}）`).join("・") : "なし"} />
            <Row k="受け取った画像・調査の記録" v={`${d.link.artifacts} 枚 · ${d.link.research} 件`} />
          </dl>
        </Card>
      )}

      {d.translate && (
        <Card id="diag-translate" title="翻訳" hint="軽い翻訳（QuickMT）の件数・速さ・キャッシュの当たり・検査で落ちた率です。落ちた文は原文のまま残しています。">
          <dl data-testid="diag-translate">
            <Row k="エンジン・モデル" v={`${d.translate.engine}・${d.translate.installed ? (d.translate.loaded ? "メモリに読み込み済み" : "入っている") : "未取得"}（スレッド ${d.translate.threads}・バッチ ${d.translate.batch}・beam ${d.translate.beam}${d.translate.cpu_isa ? `・${d.translate.cpu_isa}` : ""}）`} />
            <Row k="保存した訳（題・本文・今日）" v={`${d.translate.counts.titles} · ${d.translate.counts.bodies} · ${d.translate.counts.today} 件`} id="tr-counts" />
            <Row k="LLM の状態（ok 以外は題と本文だけ訳す）" v={d.translate.llm_state} />
            <Row k="動いてからの文・速さ" v={`${d.translate.run.units} 文・${d.translate.run.chars_per_sec} 文字/秒（読み込み ${d.translate.run.load_ms} ms）`} />
            <Row k="キャッシュの当たり" v={d.translate.run.cache_hit_pct == null ? "—" : `${d.translate.run.cache_hit_pct} %（覚え ${d.translate.counts.cache} 文）`} id="tr-cache" />
            <Row k="検査で落ちた率（動いてから・保存した全体）" v={`${d.translate.run.fail_pct ?? "—"} % · ${d.translate.counts.fail_pct ?? "—"} %`} id="tr-fail" />
            <Row k="落ちた理由" v={Object.entries(d.translate.run.reasons).map(([k, n]) => `${k} ${n}`).join("・") || "なし"} />
            {d.translate.run.load_error && <Row k="読み込みの失敗" v={d.translate.run.load_error} />}
          </dl>
        </Card>
      )}

      <SelfCheckCard />
    </div>
  );
}
