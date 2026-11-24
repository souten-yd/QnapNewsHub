import { useEffect, useState } from "react";
import { Toggle } from "../models/formControls";
import { useDeleteOpenRouterKey, useNewsChannels, useNewsSettings, useOpenRouterKey, useRunDigest, useSaveChannels, useSaveNewsSettings, useSaveOpenRouterKey, type NewsRuntimeSettings } from "../../api/news";
import { useToasts } from "../../stores";
import { Card, field, ghost, primary } from "./settingsUi";
import { GatewayCard } from "./GatewayCard";

const label = "block text-xs text-zinc-500";
const REST_MODES: Record<string, string> = { keep_memory: "集め続けてメモリに保持", stop_all: "集める・分析・保存をすべて休む", flush_at_limit: "集め続けて上限のときだけ一括で保存" };
const PURPOSE_LABEL: Record<string, string> = { prep: "下ごしらえ", digest: "まとめ", gist: "一言要約", card: "知識カード", trend: "傾向の解説", backfill: "夜の後回し" };
const QUOTA_ORDER = ["prep", "gist", "digest", "card", "trend", "spare"];
const isPaid = (name: string) => name.trim() !== "" && !name.trim().endsWith(":free") && !name.startsWith("gateway:");

/** 有料のモデルを選んだときの注意（無料は :free で終わる）。 */
function PaidNote({ names }: { names: string[] }) {
  const paid = names.filter(isPaid);
  if (paid.length === 0) return null;
  return <p role="alert" data-testid="paid-warning" className="rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-900 dark:text-amber-300">有料のモデル（{paid.join("、")}）が入っています。使うたびに OpenRouter の残高が減ります。無料のモデルは名前が :free で終わります。</p>;
}

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

      <Card id="news-rest" title="お休みモード" hint="決めた時間帯は、ディスクへの書き込みを止めて機器を休ませます。既定は切です。">
        <Toggle label="お休みモードを使う" value={draft.rest.enabled} disabled={dis} onChange={(on) => set({ rest: { ...draft.rest, enabled: on } })} />
        {data.rest_state?.active && <p role="status" data-testid="rest-active" className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-800 dark:text-amber-300">いまお休み中です{data.rest_state.ends_at ? `（${data.rest_state.ends_at.slice(11, 16)} まで）` : ""}</p>}
        {draft.rest.enabled && <>
          <div className="grid grid-cols-2 gap-2">
            <label className={label}>開始<input type="time" aria-label="お休みの開始" value={draft.rest.start} disabled={dis} onChange={(e) => set({ rest: { ...draft.rest, start: e.target.value } })} className={`${field} mt-1 w-full`} /></label>
            <label className={label}>終わり<input type="time" aria-label="お休みの終わり" value={draft.rest.end} disabled={dis} onChange={(e) => set({ rest: { ...draft.rest, end: e.target.value } })} className={`${field} mt-1 w-full`} /></label>
          </div>
          <fieldset className="space-y-1.5">
            <legend className="text-xs font-medium text-zinc-500">お休み中の動き</legend>
            {(Object.entries(data.rest_modes ?? REST_MODES) as [NewsRuntimeSettings["rest"]["mode"], string][]).map(([id, text]) => (
              <label key={id} className="flex min-h-11 items-center gap-3 rounded-xl border border-zinc-200 px-3 py-2 text-xs dark:border-zinc-700">
                <input type="radio" name="rest-mode" checked={draft.rest.mode === id} disabled={dis} onChange={() => set({ rest: { ...draft.rest, mode: id } })} className="h-4 w-4 shrink-0" />
                <span>{text}</span>
              </label>
            ))}
          </fieldset>
          <p className="rounded-lg border border-amber-500/40 px-3 py-2 text-xs leading-relaxed" data-testid="rest-warning">
            <b>停電などで電源が切れると、メモリにだけある分は消えます。</b>「メモリに保持」「上限のときだけ保存」では、お休み中に集めた記事が保存されるのはお休みが明けたとき（または上限に達したとき）です。消えても、取得元に残っている記事は次の取得で取り直せます。お休み中も、設定の保存やログインなど、あなたの操作は書き込みます。
          </p>
        </>}
        <div className="grid gap-3 sm:grid-cols-2">
          <label className={label}>まとめ書きの間隔（分。1〜1440。ふだんの保存）
            <input type="number" min={1} max={1440} aria-label="まとめ書きの間隔" value={draft.storage.flush_minutes} disabled={dis}
              onChange={(e) => set({ storage: { ...draft.storage, flush_minutes: num(e.target.value, 1, 1440, 30) } })} className={`${field} mt-1 w-full`} /></label>
          <label className={label}>まとめ書きの件数の上限（件）
            <input type="number" min={10} max={100000} aria-label="まとめ書きの件数の上限" value={draft.storage.max_items} disabled={dis}
              onChange={(e) => set({ storage: { ...draft.storage, max_items: num(e.target.value, 10, 100000, 1000) } })} className={`${field} mt-1 w-full`} /></label>
        </div>
        <Toggle label="ログをファイルにも書く（再起動で効く）" hint="既定は切（ログはメモリだけ）。書くと書き込みが増えます" value={draft.storage.log_file} disabled={dis} onChange={(on) => set({ storage: { ...draft.storage, log_file: on } })} />
        <Toggle label="アクセスのログを出す（再起動で効く）" hint="既定は切" value={draft.storage.access_log} disabled={dis} onChange={(on) => set({ storage: { ...draft.storage, access_log: on } })} />
        {saveButton}
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
            <div className="space-y-2" data-testid="model-chain">
              <p className={label}>次のモデル（失敗が続いたとき、上から順に切り替える。空でよい）</p>
              {draft.llm.fallback_models.map((m, i) => (
                <div key={i} className="flex items-center gap-2">
                  <span className="num w-5 shrink-0 text-xs text-zinc-500">{i + 2}</span>
                  <div className="min-w-0 flex-1"><ModelInput name={`${i + 2} 番目のモデル`} value={m} disabled={dis} onChange={(v) => set({ llm: { ...draft.llm, fallback_models: draft.llm.fallback_models.map((x, j) => (j === i ? v : x)) } })} /></div>
                  <button type="button" disabled={dis} aria-label={`${i + 2} 番目のモデルを消す`} onClick={() => set({ llm: { ...draft.llm, fallback_models: draft.llm.fallback_models.filter((_, j) => j !== i) } })} className={`${ghost} mt-1`}>消す</button>
                </div>
              ))}
              {draft.llm.fallback_models.length < 4 && <button type="button" disabled={dis} onClick={() => set({ llm: { ...draft.llm, fallback_models: [...draft.llm.fallback_models, ""] } })} className={ghost}>モデルを足す</button>}
              <p className="text-[11px] text-zinc-500">429（混み合い）・空の答え・検査落ちが直近 5 回で 3 回続いたら次のモデルへ移り、30 分後に 1 番目へ戻します。</p>
              {data.model_chain && data.model_chain.chain.length > 1 && <p className="num text-xs" role="status" data-testid="chain-status">いま使っているモデル: <span className="font-mono">{data.model_chain.current}</span>{data.model_chain.return_in_seconds !== null ? `（あと約 ${Math.ceil(data.model_chain.return_in_seconds / 60)} 分で 1 番目へ戻します）` : ""}</p>}
              <PaidNote names={[draft.llm.prep_model, ...draft.llm.fallback_models]} />
            </div>
            <Toggle label="要素の取り出し（物と fact）を、下ごしらえと同じ呼び出しで行う" hint="記事ごとに、物（モデル・ソフト・組織など）と事実を取り出して「物」の画面にためます。AI の取り出しなので、間違いを含みます。呼び出しの回数は増えません。" value={draft.llm.extract_enabled} disabled={dis} onChange={(on) => set({ llm: { ...draft.llm, extract_enabled: on } })} />
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
              onChange={(e) => set({ llm: { ...draft.llm, daily_limit: num(e.target.value, 1, 5000, 800) } })} className={`${field} mt-1 w-full`} /></label>
          <label className={label}>1 分の呼び出しの上限（回。1〜100）
            <input type="number" min={1} max={100} aria-label="1 分の上限" value={draft.llm.minute_limit} disabled={dis}
              onChange={(e) => set({ llm: { ...draft.llm, minute_limit: num(e.target.value, 1, 100, 15) } })} className={`${field} mt-1 w-full`} /></label>
        </div>
        <div className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700" data-testid="llm-stats" role="status">
          <p className="num"><span className="font-semibold">今日の呼び出し {stats.total} / {stats.daily_limit} 回</span>
            <span className="text-zinc-500">（{Object.entries(stats.calls).map(([k, n]) => `${PURPOSE_LABEL[k] ?? k} ${n}`).join("・")}）</span></p>
          <p className="mt-1 text-zinc-500">失敗 {stats.failed} 回{stats.last_error ? ` · 直近の理由: ${stats.last_error}` : ""}。上限に近づくと止めて、次の日に回します。</p>
        </div>
        <p className="text-[11px] text-zinc-500">LLM が使えないときは、まとめを作りません（鍵が未設定のときも同じです）。</p>
        {saveButton}
      </Card>

      <GatewayCard value={draft.llm.gateway} onChange={(g) => set({ llm: { ...draft.llm, gateway: g } })} chain={data.model_chain} disabled={dis} onSave={commit} canSave={dirty && !save.isPending && !saveChannels.isPending} />

      <Card id="news-budget" title="無料 LLM の予算と割り当て" hint="1 日の上限を、用途ごとに分けます。自分の割り当てを使い切った用途は予備から借り、予備も尽きたら翌日まで待ちます。割り当ての合計は 1 日の上限を超えられません。">
        <div className="grid gap-2 sm:grid-cols-2" data-testid="quota-grid">
          {QUOTA_ORDER.map((k) => (
            <label key={k} className={label}>{data.quota_labels?.[k] ?? k}（回）
              <input type="number" min={0} max={5000} aria-label={`${data.quota_labels?.[k] ?? k}の割り当て`} data-testid={`quota-${k}`} value={draft.llm.quota[k] ?? 0} disabled={dis}
                onChange={(e) => set({ llm: { ...draft.llm, quota: { ...draft.llm.quota, [k]: num(e.target.value, 0, 5000, 0) } } })} className={`${field} mt-1 w-full`} />
              <span className="num text-[11px] text-zinc-500">今日 {k === "spare" ? stats.spare_used : (stats.calls[k] ?? 0)} 回使用</span></label>
          ))}
        </div>
        {(() => {
          const sum = Object.values(draft.llm.quota).reduce((a, b) => a + b, 0);
          const over = sum > draft.llm.daily_limit;
          return <p role="status" data-testid="quota-sum" className={`num text-xs font-medium ${over ? "text-red-700 dark:text-red-400" : "text-zinc-600 dark:text-zinc-300"}`}>合計 {sum} / 1 日の上限 {draft.llm.daily_limit} 回{over ? "（上限を超えているので保存できません）" : ""}</p>;
        })()}
        <div className="grid gap-3 sm:grid-cols-2">
          <Toggle label="余った枠を夜に後回しの仕事へ回す" hint="使い残した枠の分だけ、取り出しが済んでいない古い記事の取り出し直しをします" value={draft.llm.backfill_enabled} disabled={dis} onChange={(on) => set({ llm: { ...draft.llm, backfill_enabled: on } })} />
          <label className={label}>夜の始まり（時。0〜23）
            <input type="number" min={0} max={23} aria-label="夜の始まり" value={draft.llm.night_from} disabled={dis || !draft.llm.backfill_enabled}
              onChange={(e) => set({ llm: { ...draft.llm, night_from: num(e.target.value, 0, 23, 21) } })} className={`${field} mt-1 w-full`} /></label>
        </div>
        <p className="num text-xs text-zinc-500" data-testid="budget-leftover">今日使える後回しの残り {stats.leftover} 回（{stats.night_from} 時以降に使います。今日はすでに {stats.calls.backfill ?? 0} 回）</p>
        <button type="button" onClick={commit} disabled={!dirty || save.isPending || saveChannels.isPending || Object.values(draft.llm.quota).reduce((a, b) => a + b, 0) > draft.llm.daily_limit} className={primary}>保存</button>
      </Card>
    </>
  );
}
