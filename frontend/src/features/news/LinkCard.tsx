import { useEffect, useState } from "react";
import { useCancelPairing, useLink, useRevokePeer, useStartPairing, type PairingStart } from "../../api/link";
import { useToasts } from "../../stores";
import { Card, ghost, primary } from "./settingsUi";

const when = (iso: string) => (iso ? new Date(iso).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "まだ来ていません");
const KEY_LABEL: Record<string, string> = { openrouter: "OpenRouter の鍵" };

/** 残り時間（mm:ss）。0 になったら番号は無効。 */
function useRemaining(expiresAt: string | undefined): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!expiresAt) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [expiresAt]);
  return expiresAt ? Math.max(0, Math.floor((new Date(expiresAt).getTime() - now) / 1000)) : 0;
}

/** 設定 → ControlDeck とつなぐ。6 桁の番号（10 分）とニュースハブの公開鍵の指紋を出し、つないだ相手の一覧（最後に来た時刻・取り消し）・受け取った鍵の名前・仕事の数を見せる。
 *  番号と指紋は ControlDeck の画面と見比べる。鍵の値は画面に出さない。 */
export function LinkCard() {
  const toast = useToasts((s) => s.show);
  const [started, setStarted] = useState<PairingStart | null>(null);
  const [confirm, setConfirm] = useState<number | null>(null);
  const q = useLink(started !== null);
  const start = useStartPairing(), cancel = useCancelPairing(), revoke = useRevokePeer();
  const s = q.data;
  const left = useRemaining(started?.expires_at);
  const waiting = started !== null && left > 0 && s?.pairing != null;
  useEffect(() => { if (started && s && s.pairing === null && left > 0) setStarted(null); }, [started, s, left]);      // つながった（番号が使われた）
  const fail = (e: unknown) => toast(e instanceof Error ? e.message : "できませんでした", "error");
  const active = s?.peers.filter((p) => !p.revoked) ?? [];
  const mmss = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
  return (
    <Card id="link-controldeck" title="ControlDeck とつなぐ" hint="ControlDeck が仕事（埋め込み・要約・画像）を借りたり、画像を送ったりできるようにします。つないだ相手は読み取りと仕事の受け渡しだけで、ニュースハブの設定は変えられません。">
      {!s && <p className="text-xs text-zinc-500" role="status">読み込み中…</p>}
      {s && (
        <>
          <div className="rounded-xl border border-zinc-200 p-3 dark:border-zinc-700">
            <p className="text-xs text-zinc-500">ニュースハブの公開鍵の指紋（ControlDeck の画面に出る指紋と同じか、目で確かめてください）</p>
            <p data-testid="link-fingerprint" className="num mt-1 break-all font-mono text-base font-semibold tracking-wide">{s.fingerprint}</p>
          </div>
          {waiting && started ? (
            <div data-testid="link-pairing" className="rounded-xl border border-accent-500/50 bg-accent-500/5 p-3" role="status">
              <p className="text-xs text-zinc-600 dark:text-zinc-300">ControlDeck の「ニュースハブとつなぐ」に、この番号とニュースハブの URL を入れてください。</p>
              <p data-testid="link-code" className="num mt-2 text-center font-mono text-3xl font-bold tracking-[0.3em] md:text-4xl" aria-label={`つなぐ番号 ${started.code.split("").join(" ")}`}>{started.code}</p>
              <p data-testid="link-remaining" className="num mt-1 text-center text-xs text-zinc-500">あと {mmss}（1 回だけ使えます）</p>
              <button type="button" className={`${ghost} mt-2`} disabled={cancel.isPending} onClick={() => cancel.mutate(undefined, { onSuccess: () => setStarted(null), onError: fail })}>番号をやめる</button>
            </div>
          ) : (
            <div>
              {started && left === 0 && <p role="status" data-testid="link-expired" className="mb-2 text-xs font-medium text-amber-700 dark:text-amber-300">番号の期限が切れました。もう一度出してください。</p>}
              <button type="button" className={primary} disabled={start.isPending} onClick={() => start.mutate(undefined, { onSuccess: (r) => setStarted(r), onError: fail })}>{start.isPending ? "出しています…" : "つなぐ番号を出す"}</button>
            </div>
          )}

          <div>
            <h3 className="text-xs font-semibold text-zinc-500">つないだ ControlDeck</h3>
            {active.length === 0 ? <p data-testid="link-empty" className="mt-1 text-xs text-zinc-500">まだつないでいません。</p> : (
              <ul className="mt-1 space-y-1.5" data-testid="link-peers">
                {active.map((p) => (
                  <li key={p.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-zinc-200 px-3 py-2 text-xs dark:border-zinc-700">
                    <div className="min-w-0 flex-1">
                      <p className="break-words font-semibold">{p.name}</p>
                      <p className="num text-zinc-500">最後に来た時刻 {when(p.last_seen_at)} · 相手の鍵 <span className="font-mono">{p.fingerprint}</span></p>
                    </div>
                    <button type="button" className={ghost} onClick={() => setConfirm(p.id)}>取り消す</button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <dl className="text-xs" data-testid="link-secrets">
            <dt className="font-semibold text-zinc-500">受け取った鍵（値は表示しません）</dt>
            {s.secrets.length === 0 ? <dd className="mt-1 text-zinc-500">なし</dd> : s.secrets.map((k) => (
              <dd key={k.name} className="num mt-1 flex flex-wrap justify-between gap-x-3"><span className="font-medium">{KEY_LABEL[k.name] ?? k.name}{k.present ? "" : "（いまは入っていません）"}</span><span className="text-zinc-500">{when(k.received_at)}{k.peer ? ` · ${k.peer}` : ""}</span></dd>
            ))}
          </dl>
          <p className="num text-xs text-zinc-500" data-testid="link-jobs">仕事: 待ち {s.jobs.queued} · 借りている {s.jobs.leased} · 期限切れ {s.jobs.expired} · 終わらなかった {s.jobs.dead} · 24 時間で完了 {s.jobs.done_24h}</p>
        </>
      )}
      {confirm !== null && (
        <div role="dialog" aria-modal="true" aria-label="つないだ相手を取り消す" data-testid="link-confirm" className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4">
          <div className="w-full max-w-sm rounded-2xl border border-zinc-200 bg-white p-4 shadow-xl dark:border-zinc-700 dark:bg-zinc-900">
            <p className="text-sm font-semibold">この ControlDeck の接続を取り消しますか</p>
            <p className="mt-2 text-xs leading-relaxed text-zinc-500">取り消すと、その ControlDeck のトークンはすぐに使えなくなります。借りている仕事の期限が切れたら列に戻ります。もう一度つなぐには、新しい番号が要ります。</p>
            <div className="mt-3 flex justify-end gap-2">
              <button type="button" className={ghost} onClick={() => setConfirm(null)}>やめる</button>
              <button type="button" className={primary} disabled={revoke.isPending} onClick={() => revoke.mutate(confirm, { onSuccess: () => { setConfirm(null); toast("取り消しました", "success"); }, onError: fail })}>取り消す</button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
