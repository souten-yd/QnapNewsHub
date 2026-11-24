import { useState } from "react";
import { Toggle } from "../models/formControls";
import { useDeleteGatewayKey, useGatewayKey, useSaveGatewayKey, useTestGateway, type GatewaySettings, type GatewayTest, type ModelChainStatus } from "../../api/news";
import { useDiagnostics } from "../../api/diag";
import { useToasts } from "../../stores";
import { Card, field, ghost, primary } from "./settingsUi";

const label = "block text-xs text-zinc-500";
const clock = (iso: string) => (iso ? new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");

/** ControlDeck のゲートウェイ（OpenAI 互換のローカル LLM）。行き先の順番・鍵・接続の確認・行き先ごとの成績。
 *  鍵は OpenRouter の鍵と同じく暗号化して保存し、入れたら入力欄を空にする（本文は返らない）。 */
export function GatewayCard({ value, onChange, chain, disabled, onSave, canSave }: {
  value: GatewaySettings; onChange: (g: GatewaySettings) => void; chain?: ModelChainStatus; disabled: boolean; onSave: () => void; canSave: boolean;
}) {
  const toast = useToasts((s) => s.show);
  const keyStatus = useGatewayKey();
  const saveKey = useSaveGatewayKey();
  const removeKey = useDeleteGatewayKey();
  const test = useTestGateway();
  const diag = useDiagnostics();
  const [keyValue, setKeyValue] = useState("");
  const [result, setResult] = useState<GatewayTest | null>(null);
  const set = (patch: Partial<GatewaySettings>) => onChange({ ...value, ...patch });
  const fail = (e: unknown) => toast(e instanceof Error ? e.message : "できませんでした", "error");
  const num = (v: string, low: number, high: number, fallback: number) => Math.max(low, Math.min(high, Number(v) || fallback));
  const keySet = keyStatus.data?.set === true;
  const rows = (diag.data?.llm.models ?? []);
  return (
    <Card id="news-gateway" title="ControlDeck のゲートウェイ" hint="ControlDeck のローカル LLM（OpenAI 互換）を、OpenRouter より先に使えます。ControlDeck の PC の電源が切れていてつながらないときは、自動で OpenRouter の無料モデルへ切り替わります。1 日の上限・用途ごとの割り当ては OpenRouter にだけ掛かります。">
      <Toggle label="ゲートウェイを使う" value={value.enabled} disabled={disabled} onChange={(on) => set({ enabled: on })} />
      <label className={label}>基準の URL（例 http://（ControlDeck の PC）:8765/api/v1/llm/v1）
        <input aria-label="ゲートウェイの URL" data-testid="gateway-url" value={value.base_url} disabled={disabled} spellCheck={false} autoComplete="off" inputMode="url"
          onChange={(e) => set({ base_url: e.target.value })} className={`${field} mt-1 w-full font-mono text-xs`} placeholder="http://…:8765/api/v1/llm/v1" /></label>
      <label className={label}>モデル（接続を試すと一覧から選べます。auto はゲートウェイに任せる）
        <input aria-label="ゲートウェイのモデル" data-testid="gateway-model" list="gateway-models" value={value.model} disabled={disabled} spellCheck={false} autoComplete="off"
          onChange={(e) => set({ model: e.target.value })} className={`${field} mt-1 w-full font-mono text-xs`} />
        <datalist id="gateway-models">{(result?.models ?? []).map((m) => <option key={m} value={m} />)}</datalist></label>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" disabled={test.isPending || !value.base_url.trim()} data-testid="gateway-test"
          onClick={() => test.mutate(value.base_url.trim(), { onSuccess: setResult, onError: fail })} className={ghost}>{test.isPending ? "試しています…" : "接続を試す"}</button>
        {result && (
          <p role="status" data-testid="gateway-result" className={`num text-xs font-medium ${result.ok ? "text-emerald-700 dark:text-emerald-400" : "text-red-700 dark:text-red-400"}`}>
            {result.ok ? `つながりました（${result.ms} ms・モデル ${result.models.length} 件）` : `つながりません: ${result.error}`}
          </p>
        )}
      </div>
      <p className="text-xs" data-testid="gateway-key-status" role="status">
        {keyStatus.isLoading ? "確認中…" : keySet ? <span className="font-semibold text-emerald-700 dark:text-emerald-400">鍵は入っています（本文は表示しません）</span> : <span className="text-zinc-500">鍵は未設定です（ゲートウェイが鍵を求めないなら不要です）</span>}
      </p>
      <form onSubmit={(e) => { e.preventDefault(); if (keyValue.trim()) saveKey.mutate(keyValue.trim(), { onSuccess: () => { setKeyValue(""); toast("ゲートウェイの鍵を保存しました", "success"); }, onError: fail }); }} className="flex flex-wrap items-end gap-2">
        <label className={`${label} min-w-0 flex-1`}>{keySet ? "鍵を入れ直す" : "鍵を入れる"}
          <input type="password" name="gateway-key" aria-label="ゲートウェイの鍵" autoComplete="new-password" spellCheck={false} value={keyValue} onChange={(e) => setKeyValue(e.target.value)} className={`${field} mt-1 w-full font-mono text-xs`} /></label>
        <button type="submit" disabled={!keyValue.trim() || saveKey.isPending} className={primary}>登録する</button>
        {keySet && <button type="button" disabled={removeKey.isPending} onClick={() => removeKey.mutate(undefined, { onSuccess: () => toast("鍵を消しました", "success"), onError: fail })} className={`${ghost} text-red-600`}>登録を消す</button>}
      </form>
      <Toggle label="OpenRouter より先に使う" hint="切にすると OpenRouter が先で、ゲートウェイは最後の予備になります" value={value.first} disabled={disabled} onChange={(on) => set({ first: on })} />
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={label}>1 回に送る記事の件数（1〜30。ControlDeck を邪魔しない件数）
          <input type="number" min={1} max={30} aria-label="ゲートウェイの 1 回の件数" value={value.batch} disabled={disabled} onChange={(e) => set({ batch: num(e.target.value, 1, 30, 10) })} className={`${field} mt-1 w-full`} /></label>
        <label className={label}>時間切れ（秒。30〜1800。初回はモデルの読み込みで遅い）
          <input type="number" min={30} max={1800} aria-label="ゲートウェイの時間切れ" value={value.timeout_seconds} disabled={disabled} onChange={(e) => set({ timeout_seconds: num(e.target.value, 30, 1800, 300) })} className={`${field} mt-1 w-full`} /></label>
        <label className={label}>1 分の呼び出しの上限（回。同時は 1 本）
          <input type="number" min={1} max={60} aria-label="ゲートウェイの 1 分の上限" value={value.minute_limit} disabled={disabled} onChange={(e) => set({ minute_limit: num(e.target.value, 1, 60, 6) })} className={`${field} mt-1 w-full`} /></label>
        <label className={label}>つながらないとき、飛ばす時間（分）
          <input type="number" min={1} max={120} aria-label="ゲートウェイを飛ばす時間" value={value.cooldown_minutes} disabled={disabled} onChange={(e) => set({ cooldown_minutes: num(e.target.value, 1, 120, 10) })} className={`${field} mt-1 w-full`} /></label>
      </div>
      <div className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700" data-testid="provider-order">
        <p className="font-semibold">いまの行き先の順番</p>
        <ol className="mt-1 space-y-0.5">
          {(chain?.chain ?? []).map((m, i) => (
            <li key={m} className="num"><span className="text-zinc-500">{i + 1}. </span>{m.startsWith("gateway:") ? "ControlDeck のゲートウェイ" : "OpenRouter"} <span className="font-mono">{m.replace(/^gateway:/, "")}</span></li>
          ))}
        </ol>
        {chain?.gateway_down_in_seconds != null && <p className="mt-1 font-medium text-amber-800 dark:text-amber-300" role="status">ゲートウェイにつながらないため、あと約 {Math.floor(chain.gateway_down_in_seconds / 60) + 1} 分は順番から外しています。</p>}
        {rows.length > 0 && (
          <ul className="mt-2 space-y-0.5 border-t border-zinc-200 pt-2 dark:border-zinc-700" data-testid="provider-stats">
            {rows.map((r) => <li key={r.model} className="num">{r.provider === "gateway" ? "ゲートウェイ" : "OpenRouter"} <span className="font-mono">{r.model.replace(/^gateway:/, "")}</span> — 成功率 {r.success_pct ?? "—"}% · 平均 {r.avg_ms} ms · 最後に成功 {clock(r.last_ok_at)}</li>)}
          </ul>
        )}
      </div>
      <button type="button" onClick={onSave} disabled={!canSave} className={primary}>保存</button>
    </Card>
  );
}
