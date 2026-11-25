import { useState } from "react";
import { useConnectorMutations, useConnectors, type Connector, type SettingValue } from "../../api/connectors";
import { useToasts } from "../../stores";

const btn = "min-h-9 shrink-0 rounded-lg border border-zinc-300 px-3 text-xs hover:border-zinc-400 disabled:opacity-40 dark:border-zinc-700";
const input = "h-9 min-w-0 rounded-lg border border-zinc-300 bg-white px-2 text-xs dark:border-zinc-700 dark:bg-zinc-900";
const STATUS: Record<string, { label: string; cls: string }> = {
  confirmed: { label: "確認済み", cls: "border-emerald-500/60 text-emerald-800 dark:text-emerald-300" },
  partial: { label: "一部確認", cls: "border-amber-500/60 text-amber-800 dark:text-amber-300" },
  per_site: { label: "サイトごと", cls: "border-sky-500/60 text-sky-800 dark:text-sky-300" },
  unconfirmed: { label: "未確認", cls: "border-red-500/60 text-red-800 dark:text-red-300" },
};
const REDIST: Record<string, string> = { allowed: "可", conditional: "条件つき", forbidden: "不可", unknown: "未確認" };
const SETTING_LABEL: Record<string, string> = {
  topics: "トピック", window_days: "直近の日数", min_stars: "最低のスター数", per_topic: "トピックごとの件数", watch_stars: "監視のリポジトリのスター数も取る",
  authors: "作者", tags: "タグ", pipelines: "pipeline", watch_models: "監視のモデル", limit: "件数", relations: "派生の種類", filter: "フィルタ", search: "検索語", days: "直近の日数",
  per_page: "1 回の件数", batch: "1 回に解決する DOI", depth: "引用をたどる段（1〜2）", seeds: "1 回にたどる論文", per_paper: "1 論文あたりの件数", second_level: "2 段目にたどる数",
  per_query: "1 回の問いの件数", series: "系列（DB:系列コード）", last_points: "取り込む点の数", tables: "統計表 ID", terms: "検索語", house: "院（空なら全部）", per_term: "1 語の件数",
  laws: "法令（法令 ID か法令番号）", max_revisions: "取り込む版の数", sec_codes: "証券コード（空なら上場会社すべて）", chains: "チェーン", protocols: "プロトコル（slug）", stablecoins_top: "ステーブルコインの上位",
  rwa_top: "RWA の上位", coins: "銘柄の id", vs: "通貨", queries: "検索語", timespan: "期間（例 1d）", max_records: "1 問いの件数", packages: "パッケージ", per_package: "1 パッケージの件数", sites: "サイト（名前 | URL を 1 行ずつ）", max_urls: "1 サイトの URL の数", pages: "ページ（名前 | URL を 1 行ずつ）",
};
const when = (iso: string) => (iso ? new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const pct = (a: number, b: number) => (b > 0 ? Math.min(100, Math.round((a * 100) / b)) : 100);

function Bar({ label, used, max, unit, testId }: { label: string; used: number; max: number; unit: string; testId: string }) {
  const p = pct(used, max);
  return (
    <div data-testid={testId}>
      <div className="flex justify-between gap-2 text-[11px]"><span className="text-zinc-500">{label}</span><span className="num">{used.toLocaleString()} / {max.toLocaleString()} {unit}</span></div>
      <div role="progressbar" aria-label={label} aria-valuenow={p} aria-valuemin={0} aria-valuemax={100} className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-700">
        <div className={`h-full ${p >= 100 ? "bg-red-500" : p >= 80 ? "bg-amber-500" : "bg-accent-600"}`} style={{ width: `${p}%` }} /></div>
    </div>
  );
}

const isSites = (v: SettingValue): v is { name: string; url: string }[] => Array.isArray(v) && v.length > 0 && typeof v[0] === "object";
const toText = (v: SettingValue): string => (isSites(v) ? v.map((r) => `${r.name} | ${r.url}`).join("\n") : Array.isArray(v) ? (v as string[]).join(", ") : String(v));
function fromText(old: SettingValue, key: string, text: string): SettingValue {
  if (typeof old === "boolean") return text === "true";
  if (typeof old === "number") return Number(text);
  if (Array.isArray(old)) {
    if (isSites(old) || key === "sites" || key === "pages") {
      return text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [name, url] = l.includes("|") ? l.split("|").map((x) => x.trim()) : ["", l];
        return { name, url };
      });
    }
    return text.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
  }
  return text;
}

function ConnectorCard({ c }: { c: Connector }) {
  const toast = useToasts((s) => s.show);
  const m = useConnectorMutations();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState(String(c.daily_items));
  const [mb, setMb] = useState(String(c.daily_mb));
  const [every, setEvery] = useState(String(c.interval_minutes));
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [key, setKey] = useState("");
  const [ack, setAck] = useState(c.terms_ack);
  const err = (e: unknown) => toast(e instanceof Error ? e.message : "できませんでした", "error");
  const st = STATUS[c.terms.status] ?? STATUS.unconfirmed;
  const busy = m.patch.isPending;
  const keyMissing = c.key_required && !c.has_key;
  const saveBudget = () => m.patch.mutate({ id: c.id, body: { daily_items: Number(items), daily_mb: Number(mb), interval_minutes: Number(every) } }, { onSuccess: () => toast("保存しました", "success"), onError: err });
  const saveSettings = () => {
    const settings: Record<string, SettingValue> = {};
    for (const [k, v] of Object.entries(c.settings)) if (k in draft) settings[k] = fromText(v, k, draft[k]);
    m.patch.mutate({ id: c.id, body: { settings } }, { onSuccess: () => { setDraft({}); toast("設定を保存しました", "success"); }, onError: err });
  };
  const toggle = (on: boolean) => m.patch.mutate({ id: c.id, body: { enabled: on, ...(on && c.needs_ack ? { terms_ack: ack } : {}) } }, { onError: err });
  return (
    <li className="rounded-xl border border-zinc-200 p-3 dark:border-zinc-700" data-testid="connector-row" data-id={c.id} data-enabled={c.enabled}>
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-[10rem] flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <button type="button" className="text-left text-sm font-medium break-words" aria-expanded={open} onClick={() => setOpen(!open)} data-testid="connector-open">{c.label}</button>
            <span className="rounded bg-zinc-100 px-1.5 text-[10px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">{c.kind_label}</span>
            {c.adapted && <span className="rounded border border-zinc-300 px-1.5 text-[10px] text-zinc-600 dark:border-zinc-600 dark:text-zinc-300">既存の取得</span>}
            <span className={`rounded border px-1.5 text-[10px] ${st.cls}`} data-testid="connector-terms-status">利用条件: {st.label}</span>
            {c.key_required && <span className={`rounded border px-1.5 text-[10px] ${keyMissing ? "border-amber-500/60 text-amber-800 dark:text-amber-300" : "border-emerald-500/60 text-emerald-800 dark:text-emerald-300"}`} data-testid="connector-key-badge">{keyMissing ? "鍵が要る" : "鍵あり"}</span>}
          </div>
          <p className="mt-0.5 text-[11px] leading-relaxed text-zinc-500">{c.desc}</p>
        </div>
        <label className="flex min-h-9 items-center gap-1 text-[11px]">
          <input type="checkbox" role="switch" checked={c.enabled} disabled={busy || (!c.enabled && (keyMissing || (c.needs_ack && !ack)))} aria-label={`${c.label} を入れる`} data-testid="connector-enabled" onChange={(e) => toggle(e.target.checked)} />{c.enabled ? "入" : "切"}</label>
      </div>
      {keyMissing && <p role="note" data-testid="connector-key-needed" className="mt-2 rounded-lg border border-amber-500/50 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-900 dark:text-amber-300">鍵が要ります。詳細を開いて鍵を入れると、入にできます。</p>}
      {c.needs_ack && !c.enabled && (
        <label className="mt-2 flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-500/5 p-2 text-[11px]" data-testid="connector-ack">
          <input type="checkbox" className="mt-0.5" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span>利用条件が未確認だと分かったうえで入れます（公式の資料で確かめられていません）。</span></label>
      )}
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <Bar label="今日の件数" used={c.usage.items} max={c.daily_items} unit="件" testId="connector-usage-items" />
        <Bar label="今日の通信量" used={c.usage.mb} max={c.daily_mb} unit="MB" testId="connector-usage-mb" />
      </div>
      {c.budget_stop && c.enabled && <p role="alert" data-testid="connector-budget-stop" className="mt-2 rounded-lg border border-amber-500/50 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-900 dark:text-amber-300">止めています: {c.budget_stop.reason}（{when(c.budget_stop.at)}）</p>}
      <p className="mt-1 break-words text-[11px] text-zinc-500" data-testid="connector-last">
        {c.last ? `最後: ${when(c.last.at)} · ${c.last.error ? `失敗（${c.last.error}）` : c.last.skipped ? `見送り（${c.last.skipped}）` : `${c.last.count} 件`}` : c.adapted ? "実績は取得先の一覧で見られます" : "まだ取っていません"}</p>
      {open && (
        <div className="mt-3 space-y-3 border-t border-zinc-200 pt-3 dark:border-zinc-700" data-testid="connector-detail">
          <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2" data-testid="connector-terms">
            <div><dt className="text-zinc-400">ライセンス</dt><dd className="break-words">{c.terms.license}</dd></div>
            <div><dt className="text-zinc-400">保存</dt><dd className="break-words">{c.terms.storage}</dd></div>
            <div><dt className="text-zinc-400">再配布</dt><dd>{REDIST[c.terms.redistribution] ?? c.terms.redistribution}</dd></div>
            <div><dt className="text-zinc-400">レート</dt><dd className="break-words">{c.terms.rate}</dd></div>
            <div className="sm:col-span-2"><dt className="text-zinc-400">注意</dt><dd className="break-words">{c.terms.notes}</dd></div>
            <div><dt className="text-zinc-400">公式の利用規約</dt><dd><a href={c.terms.tos_url} target="_blank" rel="noopener noreferrer" className="break-all underline">{c.terms.tos_url}</a></dd></div>
            <div><dt className="text-zinc-400">確かめた日</dt><dd className="num">{c.terms.checked_at}</dd></div>
          </dl>
          <div className="flex flex-wrap items-end gap-2 text-[11px]">
            <label className="flex flex-col gap-0.5">取得の間隔（分）<input className={`${input} w-24`} inputMode="numeric" value={every} onChange={(e) => setEvery(e.target.value)} data-testid="connector-interval" /></label>
            <label className="flex flex-col gap-0.5">1 日の件数<input className={`${input} w-24`} inputMode="numeric" value={items} onChange={(e) => setItems(e.target.value)} data-testid="connector-daily-items" /></label>
            <label className="flex flex-col gap-0.5">1 日の通信量（MB）<input className={`${input} w-24`} inputMode="numeric" value={mb} onChange={(e) => setMb(e.target.value)} data-testid="connector-daily-mb" /></label>
            <button type="button" className={btn} disabled={busy} onClick={saveBudget} data-testid="connector-save-budget">予算を保存</button>
          </div>
          {Object.keys(c.settings).length > 0 && (
            <div className="space-y-2" data-testid="connector-settings">
              {Object.entries(c.settings).map(([k, v]) => (
                <label key={k} className="flex flex-col gap-0.5 text-[11px]">{SETTING_LABEL[k] ?? k}
                  {typeof v === "boolean"
                    ? <input type="checkbox" className="h-5 w-5" checked={(draft[k] ?? String(v)) === "true"} onChange={(e) => setDraft({ ...draft, [k]: String(e.target.checked) })} />
                    : k === "sites" || k === "pages"
                      ? <textarea className={`${input} h-20 py-1`} value={draft[k] ?? toText(v)} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
                      : <input className={`${input} w-full`} value={draft[k] ?? toText(v)} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />}
                </label>
              ))}
              <button type="button" className={btn} disabled={busy || Object.keys(draft).length === 0} onClick={saveSettings} data-testid="connector-save-settings">設定を保存</button>
            </div>
          )}
          {c.has_key_option && (
            <div className="flex flex-wrap items-center gap-2 text-[11px]" data-testid="connector-key">
              <span className="text-zinc-500">{c.key_required ? "鍵（必須）" : "鍵（任意）"}: {c.has_key ? "入っています" : c.key_required ? "なし（鍵が入るまで動きません）" : "なし（低いレートで取ります）"}</span>
              <input type="password" autoComplete="off" aria-label={`${c.label} の鍵`} className={`${input} w-48`} value={key} onChange={(e) => setKey(e.target.value)} placeholder="鍵を入れる" />
              <button type="button" className={btn} disabled={!key || m.setKey.isPending} onClick={() => m.setKey.mutate({ id: c.id, value: key }, { onSuccess: () => { setKey(""); toast("暗号化して保存しました", "success"); }, onError: err })}>保存</button>
              {c.has_key && <button type="button" className={btn} onClick={() => m.delKey.mutate(c.id, { onError: err })}>消す</button>}
            </div>
          )}
          {!c.adapted && c.scheduled && (
            <button type="button" className={btn} disabled={!c.enabled || m.run.isPending} data-testid="connector-run"
              onClick={() => m.run.mutate(c.id, { onSuccess: (r) => toast(r.ran ? `${r.count} 件を取りました（保存は次のまとめ書き）` : r.reason, r.ran && !r.error ? "success" : "error"), onError: err })}>{m.run.isPending ? "取っています…" : "いま取る"}</button>
          )}
        </div>
      )}
    </li>
  );
}

/** 取得先の管理 → connector。収集の種類ごとの入・切・予算・今日の使用・利用条件・鍵、保存の段（L0〜L4）の数、昇格の規則。 */
export function ConnectorsPanel() {
  const q = useConnectors();
  const m = useConnectorMutations();
  const toast = useToasts((s) => s.show);
  if (q.isError) return <p role="alert" className="text-xs">connector を読み込めませんでした。</p>;
  if (!q.data) return <p className="text-xs text-zinc-500">読み込んでいます…</p>;
  const d = q.data;
  const total = (tier: string) => Object.values(d.tiers).reduce((n, row) => n + (row[tier] ?? 0), 0);
  const rules: [string, string][] = [["channel", "チャンネルに合う"], ["hot", "ホットトピックを含む"], ["watch", "監視のThingsを含む"], ["surge", "スターの急増"], ["derivative", "監視のモデルの派生版"]];
  return (
    <section aria-label="connector" className="space-y-3" data-testid="connectors-panel">
      <p className="text-xs leading-relaxed text-zinc-500">
        収集の種類ごとに、取得の間隔・1 日の件数・1 日の通信量（予算）・鍵・利用条件を持ちます。予算を超えると止まり、診断に出ます。新しい connector は切で始まり、利用条件が未確認のものは確認の明示が要ります。{d.rest && " いまはお休みモードです。"}
      </p>
      <div className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700" data-testid="connector-tiers">
        <p className="font-semibold">保存の段</p>
        <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px]">{Object.entries(d.tier_labels).map(([t, l]) => <li key={t}><span className="text-zinc-500">{l}</span> <span className="num font-medium" data-testid={`connector-tier-${t}`}>{total(t).toLocaleString()}</span></li>)}</ul>
        <p className="mt-2 font-semibold">昇格の規則（L0・L1 → L2。理由と時刻を残します）</p>
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">{rules.map(([k, l]) => (
          <label key={k} className="flex min-h-8 items-center gap-1 text-[11px]"><input type="checkbox" checked={Boolean(d.promotion[k])} data-testid={`connector-rule-${k}`}
            onChange={(e) => m.promotion.mutate({ [k]: e.target.checked }, { onError: (er) => toast(er instanceof Error ? er.message : "できませんでした", "error") })} />{l}</label>))}</div>
      </div>
      <ul className="space-y-2" data-testid="connector-list">
        {d.connectors.map((c) => <ConnectorCard key={c.id} c={c} />)}
      </ul>
    </section>
  );
}
