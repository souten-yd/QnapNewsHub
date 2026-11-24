import { useEffect, useState } from "react";
import { Toggle } from "../models/formControls";
import { useDeleteOpenRouterKey, useNewsChannels, useNewsSettings, useOpenRouterKey, useRunDigest, useSaveChannels, useSaveNewsSettings, useSaveOpenRouterKey, type NewsRuntimeSettings } from "../../api/news";
import { useToasts } from "../../stores";
import { Card, field, ghost, primary } from "./settingsUi";

const label = "block text-xs text-zinc-500";
const PURPOSE_LABEL: Record<string, string> = { prep: "下ごしらえ", digest: "まとめ", gist: "一言要約" };

/** OpenRouter のモデル名の入力（無料のモデルは :free で終わる）。一覧は取らない（ニュースハブは外へ余計に通信しない）。 */
function ModelInput({ name, value, disabled, onChange }: { name: string; value: string; disabled: boolean; onChange: (v: string) => void }) {
  return <input aria-label={name} value={value} disabled={disabled} spellCheck={false} autoComplete="off" onChange={(e) => onChange(e.target.value)} className={`${field} mt-1 w-full font-mono text-xs`} placeholder="例: provider/model:free" />;
}

/** OpenRouter の鍵。入れた値は画面に残さず（送ったら入力欄を空にする）、サーバーも本文を返さない。入っているかだけを見せる。 */
function KeyCard() {
  const status = useOpenRouterKey();
  const save = useSaveOpenRouterKey();
  const remove = useDeleteOpenRouterKey();
  const toast = useToasts((s) => s.show);
  const [value, setValue] = useState("");
  const set = status.data?.set === true;
  const fail = (e: unknown) => toast(e instanceof Error ? e.message : "保存できませんでした", "error");
  const commit = () => save.mutate(value.trim(), { onSuccess: () => { setValue(""); toast("鍵を保存しました", "success"); }, onError: fail });
  return (
    <Card id="news-key" title="OpenRouter の鍵" hint="下ごしらえとまとめに使う無料の LLM（OpenRouter）の鍵です。暗号化してこの機器にだけ保存し、画面・ログには出しません。鍵が無くても、取得と閲覧は動きます。">
      <p className="text-xs" data-testid="key-status" role="status">
        {status.isLoading ? "確認中…" : set ? <span className="font-semibold text-emerald-700 dark:text-emerald-400">鍵は入っています（本文は表示しません）</span> : <span className="font-semibold text-amber-700 dark:text-amber-400">鍵は未設定です（下ごしらえとまとめは止まっています）</span>}
      </p>
      <form onSubmit={(e) => { e.preventDefault(); if (value.trim()) commit(); }} className="flex flex-wrap items-end gap-2">
        <label className={`${label} min-w-0 flex-1`}>{set ? "鍵を入れ直す" : "鍵を入れる"}
          <input type="password" name="openrouter-key" aria-label="OpenRouter の鍵" autoComplete="new-password" spellCheck={false} value={value} onChange={(e) => setValue(e.target.value)} className={`${field} mt-1 w-full font-mono text-xs`} /></label>
        <button type="submit" disabled={!value.trim() || save.isPending} className={primary}>{save.isPending ? "保存中…" : "鍵を保存"}</button>
        {set && <button type="button" disabled={remove.isPending} onClick={() => remove.mutate(undefined, { onSuccess: () => toast("鍵を消しました", "success"), onError: fail })} className={`${ghost} text-red-600`}>鍵を消す</button>}
      </form>
    </Card>
  );
}

export function NewsRuntimeCards() {
  const { data } = useNewsSettings();
  const channels = useNewsChannels();
  const save = useSaveNewsSettings();
  const saveChannels = useSaveChannels();
  const runDigest = useRunDigest();
  const toast = useToasts((s) => s.show);
  const canEdit = true;      // ニュースハブの利用者はすべて管理者
  const [draft, setDraft] = useState<NewsRuntimeSettings | null>(null);
  const [interval, setInterval] = useState("");
  useEffect(() => { if (data) setDraft(data.settings); }, [data]);
  useEffect(() => { if (channels.data) setInterval(String(channels.data.interval_minutes)); }, [channels.data]);
  if (!data || !draft || !channels.data) return null;
  const weekdays = data.weekdays;
  const set = (patch: Partial<NewsRuntimeSettings>) => setDraft({ ...draft, ...patch });
  const dirty = JSON.stringify(draft) !== JSON.stringify(data.settings) || interval !== String(channels.data.interval_minutes);
  const fail = (e: unknown) => toast(e instanceof Error ? e.message : "保存できませんでした", "error");
  const commit = () => {
    save.mutate(draft, { onSuccess: () => {
      if (interval !== String(channels.data!.interval_minutes))
        saveChannels.mutate({ channels: channels.data!.channels, archive_keep: channels.data!.archive_keep, interval_minutes: Number(interval) }, { onSuccess: () => toast("保存しました", "success"), onError: fail });
      else toast("保存しました", "success");
    }, onError: fail });
  };
  const make = (period: "daily" | "weekly") => runDigest.mutate(period, {
    onSuccess: (r) => toast(r.error && !r.body ? `まとめを作りませんでした: ${r.error}` : r.fallback ? "LLM が使えなかったので、見出しの一覧のまとめを作りました" : "まとめを作りました", r.error && !r.body ? "error" : "success"),
    onError: fail,
  });
  const saveButton = <button type="button" onClick={commit} disabled={!dirty || save.isPending || saveChannels.isPending} className={primary}>保存</button>;
  const dis = !canEdit;
  const stats = data.llm_stats;
  const num = (v: string, low: number, high: number, fallback: number) => Math.max(low, Math.min(high, Number(v) || fallback));
  return (
    <>
      <Card id="news-update" title="更新のタイミング" hint={`ニュースのページの取得・ホームのフィード・日刊と週刊のまとめを、別々に決めます。時刻はサーバーの地域の時刻です。$`}>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className={label}>ニュースのページの取得の間隔（分。15〜360）
            <input type="number" aria-label="取得の間隔" min={15} max={360} value={interval} disabled={dis} onChange={(e) => setInterval(e.target.value)} className={`${field} mt-1 w-full`} /></label>
          <label className={label}>ホームのフィードの更新の間隔（分。画面の読み直し。1〜240）
            <input type="number" aria-label="ホームのフィードの更新の間隔" min={1} max={240} value={draft.feed_refresh_minutes} disabled={dis}
              onChange={(e) => set({ feed_refresh_minutes: Number(e.target.value) })} className={`${field} mt-1 w-full`} /></label>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-2">
            <Toggle label="日刊のまとめ（今朝のまとめ）を作る" value={draft.digest_daily.enabled} disabled={dis} onChange={(on) => set({ digest_daily: { ...draft.digest_daily, enabled: on } })} />
            <label className={label}>日刊の時刻
              <input type="time" aria-label="日刊の時刻" value={draft.digest_daily.time} disabled={dis || !draft.digest_daily.enabled} onChange={(e) => set({ digest_daily: { ...draft.digest_daily, time: e.target.value } })} className={`${field} mt-1 w-full`} /></label>
          </div>
          <div className="space-y-2">
            <Toggle label="週刊のまとめ（今週のまとめ）を作る" value={draft.digest_weekly.enabled} disabled={dis} onChange={(on) => set({ digest_weekly: { ...draft.digest_weekly, enabled: on } })} />
            <div className="grid grid-cols-2 gap-2">
              <label className={label}>週刊の曜日
                <select aria-label="週刊の曜日" value={draft.digest_weekly.weekday} disabled={dis || !draft.digest_weekly.enabled} onChange={(e) => set({ digest_weekly: { ...draft.digest_weekly, weekday: Number(e.target.value) } })} className={`${field} mt-1 w-full`}>
                  {weekdays.map((w, i) => <option key={w} value={i}>{w}曜日</option>)}</select></label>
              <label className={label}>週刊の時刻
                <input type="time" aria-label="週刊の時刻" value={draft.digest_weekly.time} disabled={dis || !draft.digest_weekly.enabled} onChange={(e) => set({ digest_weekly: { ...draft.digest_weekly, time: e.target.value } })} className={`${field} mt-1 w-full`} /></label>
            </div>
          </div>
        </div>
        <div className="space-y-2">
          <Toggle label="取得しない時間帯を決める" hint="この間は記事を取りに行きません（まとめの時刻には影響しません）" value={draft.quiet.enabled} disabled={dis} onChange={(on) => set({ quiet: { ...draft.quiet, enabled: on } })} />
          {draft.quiet.enabled && (
            <div className="grid grid-cols-2 gap-2">
              <label className={label}>開始<input type="time" aria-label="取得しない時間帯の開始" value={draft.quiet.start} disabled={dis} onChange={(e) => set({ quiet: { ...draft.quiet, start: e.target.value } })} className={`${field} mt-1 w-full`} /></label>
              <label className={label}>終わり<input type="time" aria-label="取得しない時間帯の終わり" value={draft.quiet.end} disabled={dis} onChange={(e) => set({ quiet: { ...draft.quiet, end: e.target.value } })} className={`${field} mt-1 w-full`} /></label>
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {saveButton}
          {<>
            <button type="button" onClick={() => make("daily")} disabled={runDigest.isPending} className={ghost}>今すぐ日刊のまとめを作る</button>
            <button type="button" onClick={() => make("weekly")} disabled={runDigest.isPending} className={ghost}>今すぐ週刊のまとめを作る</button>
          </>}
          {runDigest.isPending && <span role="status" className="text-xs text-zinc-500">作っています…</span>}
        </div>
      </Card>

      <KeyCard />

      <Card id="news-llm" title="使う LLM" hint="用途ごとに OpenRouter のモデルを選べます。ニュースは公開情報なので、無料のモデル（名前が :free で終わる）をおすすめします。LLM に送るのは記事の題・抜粋・出どころだけです。">
        <label className={label}>日刊・週刊のまとめのモデル
          <ModelInput name="まとめのモデル" value={draft.llm.digest_model} disabled={dis} onChange={(v) => set({ llm: { ...draft.llm, digest_model: v } })} /></label>
        <label className={label}>まとめの重点（必ず含めること。日本語で、1 行に 1 つ。1000 文字まで）
          <textarea aria-label="まとめの重点" data-testid="digest-focus" rows={4} maxLength={1000} value={draft.llm.digest_focus ?? ""} disabled={dis}
            placeholder={"例:\nAI 技術の最新動向\nAI モデルの最新動向"}
            onChange={(e) => set({ llm: { ...draft.llm, digest_focus: e.target.value } })} className={`${field} mt-1 h-auto w-full py-2`} /></label>
        <p className="text-[11px] text-zinc-500" data-testid="digest-focus-note">
          ここに書いた話題を、まとめの先頭に必ず入れます（その日の記事に無ければ「今日は該当なし」と書きます）。<b>この文も OpenRouter へ送られます</b>（まとめを作るときだけ）。未公開の情報は書かないでください。
          {(draft.llm.digest_focus ?? "").length > 0 && <span className="tabular-nums"> {draft.llm.digest_focus.length} / 1000 文字</span>}
        </p>
        <div className="space-y-2">
          <Toggle label="記事の下ごしらえ（日本語の概要・話題の語）" hint={`新しく取った記事を、${data.prep_batch} 件ずつ 1 回の呼び出しで、日本語の 1〜2 文の概要（英語なら訳）と話題の語にします（題は訳しません）。終わるまでは元の題と抜粋を出します。`} value={draft.llm.prep_enabled} disabled={dis} onChange={(on) => set({ llm: { ...draft.llm, prep_enabled: on } })} />
          {draft.llm.prep_enabled && <>
            <label className={label}>下ごしらえのモデル
              <ModelInput name="下ごしらえのモデル" value={draft.llm.prep_model} disabled={dis} onChange={(v) => set({ llm: { ...draft.llm, prep_model: v } })} /></label>
            <label className={label}>1 回の取得で下ごしらえする上限（件。{data.prep_batch} 件で 1 回の呼び出し）
              <input type="number" min={1} max={200} aria-label="下ごしらえの上限" data-testid="prep-max" value={draft.llm.prep_max_per_run} disabled={dis}
                onChange={(e) => set({ llm: { ...draft.llm, prep_max_per_run: num(e.target.value, 1, 200, 1) } })} className={`${field} mt-1 w-28`} /></label>
          </>}
        </div>
        <div className="space-y-2">
          <Toggle label="記事の一言要約と話題の語付け" hint="新しく取った記事に付けます（既定は切）" value={draft.llm.gist_enabled} disabled={dis} onChange={(on) => set({ llm: { ...draft.llm, gist_enabled: on } })} />
          {draft.llm.gist_enabled && <label className={label}>一言要約のモデル
            <ModelInput name="一言要約のモデル" value={draft.llm.gist_model} disabled={dis} onChange={(v) => set({ llm: { ...draft.llm, gist_model: v } })} /></label>}
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className={label}>1 日の呼び出しの上限（回。1〜5000）
            <input type="number" min={1} max={5000} aria-label="1 日の上限" data-testid="daily-limit" value={draft.llm.daily_limit} disabled={dis}
              onChange={(e) => set({ llm: { ...draft.llm, daily_limit: num(e.target.value, 1, 5000, 300) } })} className={`${field} mt-1 w-full`} /></label>
          <label className={label}>1 分の呼び出しの上限（回。1〜100）
            <input type="number" min={1} max={100} aria-label="1 分の上限" value={draft.llm.minute_limit} disabled={dis}
              onChange={(e) => set({ llm: { ...draft.llm, minute_limit: num(e.target.value, 1, 100, 10) } })} className={`${field} mt-1 w-full`} /></label>
        </div>
        <div className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700" data-testid="llm-stats" role="status">
          <p className="num"><span className="font-semibold">今日の呼び出し {stats.total} / {stats.daily_limit} 回</span>
            <span className="text-zinc-500">（{Object.entries(stats.calls).map(([k, n]) => `${PURPOSE_LABEL[k] ?? k} ${n}`).join("・")}）</span></p>
          <p className="mt-1 text-zinc-500">失敗 {stats.failed} 回{stats.last_error ? ` · 直近の理由: ${stats.last_error}` : ""}。上限に近づくと止めて、次の日に回します。</p>
        </div>
        <p className="text-[11px] text-zinc-500">LLM が使えないときは、まとめを作りません（鍵が未設定のときも同じです）。</p>
        {saveButton}
      </Card>
    </>
  );
}
