import { useEffect, useState } from "react";
import { Toggle } from "../models/formControls";
import { useNewsSettings, useSaveNewsSettings, type TranslateSettings } from "../../api/news";
import { useDeleteModel, useFetchModel, useTranslateStatus, useTryTranslate } from "../../api/translate";
import { useToasts } from "../../stores";
import { Card, field, ghost, primary } from "./settingsUi";

const label = "block text-xs text-zinc-500";
const mb = (n: number) => `${Math.round(n / 1048576)} MB`;
const num = (v: string, low: number, high: number, fallback: number) => Math.max(low, Math.min(high, Number(v) || fallback));

/** 設定 → 翻訳。エンジン・モデルの取得（利用者の操作）・スレッド・上限・対訳表・試し訳。 */
export function TranslateCard() {
  const settings = useNewsSettings();
  const save = useSaveNewsSettings();
  const toast = useToasts((s) => s.show);
  const fetchModel = useFetchModel();
  const deleteModel = useDeleteModel();
  const trial = useTryTranslate();
  const [draft, setDraft] = useState<TranslateSettings | null>(null);
  const [glossary, setGlossary] = useState("");
  const [sample, setSample] = useState("Quantization improves prefill speed on llama.cpp. See https://example.org/en/docs for details.");
  const current = settings.data?.settings.translate;
  const downloading = false;
  const status = useTranslateStatus(downloading);
  const dl = status.data?.download;
  useEffect(() => {
    if (current) {
      setDraft(current);
      setGlossary(Object.entries(current.glossary).map(([k, v]) => `${k} = ${v}`).join("\n"));
    }
  }, [current]);
  if (!draft || !settings.data) return null;
  const dirty = JSON.stringify(draft) !== JSON.stringify(current) || glossary !== Object.entries(current?.glossary ?? {}).map(([k, v]) => `${k} = ${v}`).join("\n");
  const set = (p: Partial<TranslateSettings>) => setDraft({ ...draft, ...p });
  const fail = (e: unknown) => toast(e instanceof Error ? e.message : "できませんでした", "error");
  const commit = () => {
    const table: Record<string, string> = {};
    for (const line of glossary.split("\n")) {
      const at = line.indexOf("=");
      if (at > 0 && line.slice(at + 1).trim()) table[line.slice(0, at).trim()] = line.slice(at + 1).trim();
    }
    save.mutate({ ...settings.data!.settings, translate: { ...draft, glossary: table } }, { onSuccess: () => toast("保存しました", "success"), onError: fail });
  };
  const m = status.data?.model;
  const busy = dl?.phase === "downloading";
  return (
    <Card id="news-translate" title="翻訳" hint="英語の記事の題・概要・本文を軽いエンジン（QuickMT 英→日・CPU・int8）で日本語にします。目的は、日本語の問いで英語の記事を引けるようにすることです（訳は全文検索の索引にも入ります）。URL・コード・版・Things の名前は訳さず、訳が検査に通らなかった文は原文のまま残します。">
      <label className={label}>エンジン
        <select aria-label="翻訳のエンジン" data-testid="tr-engine" value={draft.engine} onChange={(e) => set({ engine: e.target.value as TranslateSettings["engine"] })} className={`${field} mt-1 w-full`}>
          <option value="quickmt">QuickMT 英→日（軽い・CPU）</option><option value="off">切（翻訳しない）</option>
        </select></label>
      <div className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700" data-testid="tr-model">
        <p className="font-semibold">モデル: {m?.title ?? "—"} <span className="font-normal text-zinc-500">（{m?.repo}・版 {m?.rev}・{m ? mb(m.size) : ""}・{m?.license}）</span></p>
        <p className="mt-1 text-zinc-500">{m?.license_note}。取得は Hugging Face（https）から、ファイルごとに sha256 を確かめます。</p>
        <p role="status" className="mt-1 font-medium">{m?.installed ? <span className="text-emerald-700 dark:text-emerald-400">入っています{m.loaded ? "（メモリに読み込み済み）" : ""}</span> : busy ? `取得中… ${dl ? mb(dl.done) : ""} / ${dl ? mb(dl.total) : ""}（${dl?.file}）` : dl?.phase === "error" ? <span className="text-red-700 dark:text-red-400">{dl.error}</span> : "まだ入っていません（翻訳は動きません）"}</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {!m?.installed && <button type="button" data-testid="tr-fetch" disabled={busy || fetchModel.isPending} onClick={() => fetchModel.mutate(undefined, { onError: fail })} className={primary}>モデルを取得する</button>}
          {m?.installed && <button type="button" disabled={deleteModel.isPending} onClick={() => deleteModel.mutate(undefined, { onSuccess: () => toast("モデルを消しました", "success"), onError: fail })} className={`${ghost} text-red-600`}>モデルを消す</button>}
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className={label}>スレッド数（CPU のコア。QNAP は 2 が目安）
          <input type="number" min={1} max={16} aria-label="翻訳のスレッド数" value={draft.threads} onChange={(e) => set({ threads: num(e.target.value, 1, 16, 2) })} className={`${field} mt-1 w-full`} /></label>
        <label className={label}>バッチ（1〜128）
          <input type="number" min={1} max={128} aria-label="翻訳のバッチ" value={draft.batch} onChange={(e) => set({ batch: num(e.target.value, 1, 128, 32) })} className={`${field} mt-1 w-full`} /></label>
        <label className={label}>beam（1〜8。大きいほど遅く、少し正確）
          <input type="number" min={1} max={8} aria-label="翻訳の beam" value={draft.beam} onChange={(e) => set({ beam: num(e.target.value, 1, 8, 2) })} className={`${field} mt-1 w-full`} /></label>
      </div>
      <label className={label}>CPU の命令（空なら自動。AVX の無い機械で不具合があれば GENERIC）
        <select aria-label="CPU の命令" value={draft.cpu_isa} onChange={(e) => set({ cpu_isa: e.target.value })} className={`${field} mt-1 w-full`}>
          {["", "GENERIC", "SSE4_2", "AVX", "AVX2", "AVX512", "NEON"].map((v) => <option key={v} value={v}>{v || "自動"}</option>)}
        </select></label>
      <Toggle label="本文も訳す（重要な記事を空いている時間に）" hint="重要＝チャンネルに合う・監視のThings・ホットトピック。残りの記事は、開いたときに訳します" value={draft.body_enabled} onChange={(on) => set({ body_enabled: on })} />
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={label}>本文を訳す 1 日の件数の上限
          <input type="number" min={0} max={5000} aria-label="本文の翻訳の 1 日の件数" value={draft.body_per_day} onChange={(e) => set({ body_per_day: num(e.target.value, 0, 5000, 100) })} className={`${field} mt-1 w-full`} /></label>
        <label className={label}>本文の最大の文字数（先頭から）
          <input type="number" min={1000} max={60000} aria-label="本文の翻訳の最大の文字数" value={draft.body_max_chars} onChange={(e) => set({ body_max_chars: num(e.target.value, 1000, 60000, 20000) })} className={`${field} mt-1 w-full`} /></label>
      </div>
      <Toggle label="記事を開いたとき本文を訳せる" value={draft.ondemand} onChange={(on) => set({ ondemand: on })} />
      <Toggle label="重要な記事の高品質の翻訳を ControlDeck に頼む" hint="仕事（translate_hq）を作ります。ControlDeck が借りたときだけ動きます" value={draft.hq_jobs} onChange={(on) => set({ hq_jobs: on })} />
      <label className={label}>対訳表（1 行に「英語 = 日本語」。既定: {Object.entries(status.data?.default_glossary ?? {}).slice(0, 4).map(([k, v]) => `${k}→${v}`).join("・")} ほか）
        <textarea aria-label="対訳表" data-testid="tr-glossary" rows={4} value={glossary} onChange={(e) => setGlossary(e.target.value)} spellCheck={false} className={`${field} mt-1 h-auto w-full py-2 font-mono text-xs`} placeholder="prefill = プリフィル" /></label>
      <label className={label}>訳さずそのまま残す語（1 行に 1 語）
        <textarea aria-label="そのまま残す語" rows={2} value={draft.keep.join("\n")} onChange={(e) => set({ keep: e.target.value.split("\n").map((v) => v.trim()).filter(Boolean) })} spellCheck={false} className={`${field} mt-1 h-auto w-full py-2 font-mono text-xs`} /></label>
      <button type="button" onClick={commit} disabled={!dirty || save.isPending} className={primary}>保存</button>
      <div className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700">
        <p className="font-semibold">試し訳（保存しません）</p>
        <textarea aria-label="試し訳の英文" rows={3} value={sample} onChange={(e) => setSample(e.target.value)} maxLength={500} className={`${field} mt-1 h-auto w-full py-2`} />
        <button type="button" disabled={!m?.installed || trial.isPending} onClick={() => trial.mutate(sample, { onError: fail })} className={`${ghost} mt-1`}>{trial.isPending ? "訳しています…" : "訳してみる"}</button>
        {trial.data && <p className="mt-1 whitespace-pre-wrap" lang="ja" data-testid="tr-trial">{trial.data.ja}{trial.data.failed > 0 && `（検査に通らなかった文 ${trial.data.failed}）`}</p>}
      </div>
    </Card>
  );
}
