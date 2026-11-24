import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { SafeMarkdown } from "../../components/SafeMarkdown";
import { Toggle } from "../models/formControls";
import { BUSY, useCheckUpdate, useInstallUpdate, useRollbackUpdate, useSaveUpdateSettings, useUpdate } from "../../api/update";
import { useToasts } from "../../stores";
import { Card, ghost, primary } from "./settingsUi";

const PHASES: Record<string, string> = { downloading: "取得しています", verifying: "署名と sha256 を確かめています", flushing: "メモリの記事を書き切っています", installing: "インストールしています。サーバーが入れ替わります" };
const when = (iso: string) => (iso ? new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const mb = (n: number) => (n ? `${(n / 1048576).toFixed(0)} MB` : "");

/** 設定 → 更新。QPKG では「更新する」で、署名を確かめた新しい版を入れる。Linux では新しい版があることと手順だけを出す。 */
export function UpdateCard() {
  const toast = useToasts((s) => s.show);
  const [waiting, setWaiting] = useState<string | null>(null);      // 入れたあと、サーバーが新しい版で起きるのを待っている（目標の版）
  const [confirm, setConfirm] = useState<"update" | "rollback" | null>(null);
  const q = useUpdate(waiting !== null);
  const s = q.data;
  const busy = Boolean(s && BUSY.includes(s.job.phase)) || waiting !== null;
  const { hash } = useLocation();
  useEffect(() => { if (hash === "#app-update" && s) document.getElementById("app-update")?.scrollIntoView({ block: "start" }); }, [hash, Boolean(s)]);  // eslint-disable-line react-hooks/exhaustive-deps
  const check = useCheckUpdate(), save = useSaveUpdateSettings(), install = useInstallUpdate(), rollback = useRollbackUpdate();
  useEffect(() => {
    if (waiting !== null && s?.version === waiting) { setWaiting(null); window.location.reload(); }      // 画面も新しい版にする
    if (waiting !== null && s?.job.phase === "error") setWaiting(null);
  }, [waiting, s?.version, s?.job.phase]);
  if (!s) return q.isError ? <Card id="app-update" title="アプリの更新"><p role="alert" className="text-xs">更新の状態を読み込めませんでした。</p></Card> : null;
  const err = (e: unknown) => toast(e instanceof Error ? e.message : "できませんでした", "error");
  const target = confirm === "rollback" ? s.rollback : s.latest?.version;
  const run = () => {
    if (!target) return;
    const mut = confirm === "rollback" ? rollback : install;
    mut.mutate(target, { onSuccess: () => { setConfirm(null); setWaiting(target); }, onError: (e) => { setConfirm(null); err(e); } });
  };
  const job = s.job;
  return (
    <Card id="app-update" title="アプリの更新" hint="新しい版を GitHub の Release で確かめます（1 日 1 回と、ここの「今すぐ確認」）。入れる前に、署名・sha256・大きさ・名前・版を確かめ、合わないものは入れません。">
      <dl className="text-xs" data-testid="update-state">
        <div className="flex flex-wrap justify-between gap-x-4 border-b border-zinc-100 py-1.5 dark:border-zinc-800"><dt className="text-zinc-500">今の版</dt><dd className="num font-medium" data-testid="update-current">{s.version}</dd></div>
        <div className="flex flex-wrap justify-between gap-x-4 border-b border-zinc-100 py-1.5 dark:border-zinc-800"><dt className="text-zinc-500">新しい版</dt>
          <dd className="num font-medium" data-testid="update-latest">{s.latest ? `${s.latest.version}${s.latest.prerelease ? "（試験版）" : ""}${s.latest.size ? `・${mb(s.latest.size)}` : ""}` : s.checked_at ? "ありません（最新です）" : "まだ確かめていません"}</dd></div>
        <div className="flex flex-wrap justify-between gap-x-4 py-1.5"><dt className="text-zinc-500">最後に確かめた</dt><dd className="num">{when(s.checked_at)}</dd></div>
      </dl>
      {s.error && <p role="alert" data-testid="update-error" className="rounded-xl border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-900 dark:text-amber-300">{s.error}</p>}

      {s.latest && (
        <div className="rounded-xl border border-zinc-200 p-3 dark:border-zinc-700">
          <p className="mb-1 text-xs font-semibold">Release のノート（{s.latest.tag}）</p>
          <div className="max-h-60 overflow-y-auto">{s.latest.notes ? <SafeMarkdown text={s.latest.notes} /> : <p className="text-xs text-zinc-500">ノートはありません</p>}</div>
          {s.latest.url && <a href={s.latest.url} target="_blank" rel="noopener noreferrer" className="mt-2 inline-block text-xs underline">GitHub で見る</a>}
        </div>
      )}
      {!s.can_install && s.update_available && (
        <div data-testid="update-manual" className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700">
          <p className="font-semibold">この環境（Linux）では自動では入れません。次の手順で更新してください。</p>
          <ol className="mt-1 list-decimal space-y-0.5 pl-5">{s.manual_steps.map((m) => <li key={m}><code className="font-mono">{m}</code></li>)}</ol>
        </div>
      )}
      <Toggle label="試験版（prerelease）も対象にする" hint="既定は含めません。試験版は動作の確かめが足りないことがあります" value={s.include_prerelease} disabled={save.isPending || busy} onChange={(v) => save.mutate(v, { onError: err })} />
      {s.rest.active && <p data-testid="update-rest" className="text-xs text-amber-800 dark:text-amber-300">お休みモードの間は更新しません{s.rest.ends_at ? `（${s.rest.ends_at.slice(11, 16)} に明けます）` : ""}。</p>}

      <div className="flex flex-wrap gap-2">
        <button type="button" className={ghost} disabled={check.isPending || busy} onClick={() => check.mutate(undefined, { onSuccess: (r) => toast(r.error ? "確かめられませんでした" : r.update_available ? `新しい版 ${r.latest?.version} があります` : "最新です", r.error ? "error" : "success"), onError: err })}>{check.isPending ? "確かめています…" : "今すぐ確認"}</button>
        {s.can_install && s.latest && <button type="button" className={primary} disabled={busy || s.rest.active} onClick={() => setConfirm("update")}>更新する</button>}
        {s.can_install && s.rollback && <button type="button" className={ghost} disabled={busy || s.rest.active} onClick={() => setConfirm("rollback")}>前の版（{s.rollback}）に戻す</button>}
      </div>

      {busy && (
        <div role="status" data-testid="update-progress" className="rounded-xl border border-accent-600/40 bg-accent-600/5 p-3 text-xs">
          <p className="font-medium">{job.phase === "idle" || waiting !== null && !BUSY.includes(job.phase) ? "サーバーが新しい版で起きるのを待っています…" : PHASES[job.phase] ?? job.message}</p>
          {job.phase === "downloading" && <div role="progressbar" aria-valuenow={job.percent} aria-valuemin={0} aria-valuemax={100} className="mt-2 h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-700"><div className="h-full bg-accent-600" style={{ width: `${job.percent}%` }} /></div>}
          <p className="mt-1 text-zinc-500">この画面を開いたままで大丈夫です。数十秒で入れ替わり、終わると自動で読み込み直します。</p>
        </div>
      )}
      {job.phase === "error" && <p role="alert" data-testid="update-job-error" className="rounded-xl border border-red-500/50 bg-red-500/10 px-3 py-2 text-xs font-medium text-red-800 dark:text-red-300">{job.message}（入れていません）</p>}
      {s.last && (
        <div data-testid="update-last" className={`rounded-xl border px-3 py-2 text-xs ${s.last.ok ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-900 dark:text-emerald-300" : "border-red-500/50 bg-red-500/10 text-red-800 dark:text-red-300"}`}>
          <p className="font-semibold">{s.last.ok ? "✓ " : "✗ "}{s.last.message}</p>
          <p className="num opacity-80">{when(s.last.at)}</p>
          {!s.last.ok && s.last.log && <pre data-testid="update-log" className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px]">{s.last.log}</pre>}
        </div>
      )}

      {confirm && target && (
        <div className="fixed inset-0 z-[60] grid place-items-center bg-black/50 p-4" onClick={() => setConfirm(null)}>
          <div role="dialog" aria-modal="true" aria-labelledby="update-confirm-title" data-testid="update-confirm" onClick={(e) => e.stopPropagation()} className="w-full max-w-sm space-y-3 rounded-2xl bg-white p-4 shadow-xl dark:bg-zinc-900">
            <h3 id="update-confirm-title" className="text-sm font-semibold">{confirm === "rollback" ? `前の版 ${target} に戻しますか` : `${target} に更新しますか`}</h3>
            <ul className="list-disc space-y-1 pl-5 text-xs leading-relaxed">
              <li>署名を確かめてから入れます。合わなければ入れません。</li>
              <li>メモリにある記事は先に保存します。数十秒、ニュースハブにつながらなくなります。</li>
              <li>記事・設定などのデータは残ります。{confirm === "update" ? "うまくいかないときは「前の版に戻す」で戻せます（取得した版が残っているとき）。" : ""}</li>
            </ul>
            <div className="flex justify-end gap-2">
              <button type="button" className={ghost} onClick={() => setConfirm(null)}>やめる</button>
              <button type="button" className={primary} autoFocus disabled={install.isPending || rollback.isPending} onClick={run}>{confirm === "rollback" ? "戻す" : "更新する"}</button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
