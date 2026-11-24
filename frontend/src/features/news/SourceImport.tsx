import { useState } from "react";
import { commitImport, previewImport, type ImportPreview, type ImportResult, type ImportRow } from "../../api/news";
import { useQueryClient } from "@tanstack/react-query";
import { useToasts } from "../../stores";

/** CSV・OPML の取り込み（確認の表 → 「通信確認してから追加」か「そのまま追加（無効で）」）と、OPML・CSV の書き出し。
 *  確認の表までは何も保存せず、通信もしない。重複（同じ URL）は足さず、数を出す。 */
const btn = "min-h-9 shrink-0 rounded-lg border border-zinc-300 px-3 text-xs hover:border-zinc-400 disabled:opacity-40 dark:border-zinc-700";
const STATUS_LABEL = { new: "追加できる", duplicate: "重複", invalid: "使えない" } as const;
const STATUS_STYLE = { new: "text-emerald-700 dark:text-emerald-300", duplicate: "text-zinc-500", invalid: "text-red-700 dark:text-red-300" } as const;
const CHUNK = 25;

const toBase64 = (buf: ArrayBuffer): string => {
  let bin = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
};

export function SourceImport({ origins, onDone }: { origins: Record<string, string>; onDone: () => void }) {
  const toast = useToasts((s) => s.show);
  const qc = useQueryClient();
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [mode, setMode] = useState<"check" | "direct">("check");
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState("");

  const onFile = async (file: File | undefined) => {
    setResult(null); setError(""); setPreview(null);
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { setError("ファイルが大きすぎます（2MB まで）"); return; }
    setBusy("読み込み中…");
    try {
      const p = await previewImport(toBase64(await file.arrayBuffer()));
      setPreview(p);
      setPicked(new Set(p.rows.filter((r) => r.status === "new").map((r) => r.line)));
    } catch (e) { setError(e instanceof Error ? e.message : "読み込めませんでした"); }
    setBusy("");
  };

  const run = async () => {
    if (!preview) return;
    const rows = preview.rows.filter((r) => picked.has(r.line) && r.status === "new");
    const total: ImportResult = { added: 0, duplicate: 0, invalid: 0, failed: [], mode };
    setResult(null); setError("");
    try {
      for (let i = 0; i < rows.length; i += CHUNK) {
        setBusy(mode === "check" ? `通信確認して追加中… ${Math.min(i + CHUNK, rows.length)} / ${rows.length}` : "追加中…");
        const r = await commitImport(rows.slice(i, i + CHUNK) as ImportRow[], mode);
        total.added += r.added; total.duplicate += r.duplicate; total.invalid += r.invalid; total.failed.push(...r.failed);
      }
      setResult(total);
      toast(`${total.added} 件を足しました${mode === "direct" ? "（無効で）" : ""}`, "success");
      setPreview(null);
      void qc.invalidateQueries({ queryKey: ["news"] });
      onDone();
    } catch (e) { setError(e instanceof Error ? e.message : "追加できませんでした"); }
    setBusy("");
  };

  const choose = picked.size;
  return (
    <section aria-label="取り込みと書き出し" className="space-y-4" data-testid="news-import-panel">
      <div className="space-y-2 rounded-xl border border-zinc-200 p-3 dark:border-zinc-700">
        <h2 className="text-sm font-semibold">取り込み（CSV・OPML）</h2>
        <p className="text-xs leading-relaxed text-zinc-500">
          CSV は列の名前が url・feed・xmlUrl・フィード・アドレス など、name・名前・サイト名 など、category・カテゴリ・分類 など（あれば origin・種類、interval・間隔、enabled・有効）。
          OPML は他の RSS リーダーの書き出しをそのまま読めます。確認の表を見てから追加します（この段階では保存も通信もしません）。
        </p>
        <input type="file" accept=".csv,.tsv,.txt,.opml,.xml,text/csv,text/xml,text/x-opml" aria-label="取り込むファイル" data-testid="news-import-file" disabled={!!busy}
          onChange={(e) => void onFile(e.target.files?.[0])} className="block w-full text-xs file:mr-2 file:min-h-9 file:rounded-lg file:border file:border-zinc-300 file:bg-transparent file:px-3 dark:file:border-zinc-700" />
        {busy && <p role="status" className="text-xs text-zinc-500">{busy}</p>}
        {error && <p role="alert" className="text-xs text-red-600" data-testid="news-import-error">{error}</p>}
      </div>

      {preview && (
        <div className="space-y-2 rounded-xl border border-zinc-200 p-3 dark:border-zinc-700" data-testid="news-import-preview">
          <h3 className="text-sm font-semibold">確認の表（{preview.format === "opml" ? "OPML" : "CSV"}・{preview.total} 行）</h3>
          <p className="text-xs" data-testid="news-import-counts">追加できる {preview.counts.new} 件 · 重複 {preview.counts.duplicate} 件 · 使えない {preview.counts.invalid} 件
            {preview.room < preview.counts.new && <span className="text-amber-700 dark:text-amber-300"> · 登録の上限まで残り {preview.room} 件（超えた分は足せません）</span>}</p>
          <div className="max-h-80 overflow-auto rounded-lg border border-zinc-200 dark:border-zinc-700">
            <table className="w-full min-w-[34rem] text-left text-[11px]">
              <thead className="sticky top-0 bg-zinc-50 text-zinc-500 dark:bg-zinc-800"><tr>
                <th className="px-2 py-1"><input type="checkbox" aria-label="追加できる行をすべて選ぶ" checked={choose > 0 && choose === preview.counts.new}
                  onChange={(e) => setPicked(e.target.checked ? new Set(preview.rows.filter((r) => r.status === "new").map((r) => r.line)) : new Set())} /></th>
                <th className="px-2">名前</th><th className="px-2">URL</th><th className="px-2">分類</th><th className="px-2">種類</th><th className="px-2">結果</th></tr></thead>
              <tbody>
                {preview.rows.map((r) => (
                  <tr key={r.line} className="border-t border-zinc-100 align-top dark:border-zinc-800" data-testid="news-import-row" data-status={r.status}>
                    <td className="px-2 py-1"><input type="checkbox" aria-label={`${r.name || r.url} を足す`} disabled={r.status !== "new"} checked={picked.has(r.line)}
                      onChange={(e) => setPicked((p) => { const n = new Set(p); if (e.target.checked) n.add(r.line); else n.delete(r.line); return n; })} /></td>
                    <td className="px-2 py-1 break-words">{r.name}</td>
                    <td className="max-w-[14rem] break-all px-2 py-1 text-zinc-500">{r.url}</td>
                    <td className="px-2 py-1">{r.category || "—"}</td>
                    <td className="px-2 py-1">{origins[r.origin || r.origin_guess] ?? "—"}{!r.origin && <span className="text-zinc-400">（推定）</span>}</td>
                    <td className={`px-2 py-1 ${STATUS_STYLE[r.status]}`}>{STATUS_LABEL[r.status]}{r.reason && <span className="block text-zinc-500">{r.reason}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <fieldset className="space-y-1 text-xs">
            <legend className="font-medium">追加のしかた</legend>
            <label className="flex items-start gap-2"><input type="radio" name="import-mode" checked={mode === "check"} onChange={() => setMode("check")} data-testid="news-import-mode-check" />
              <span>通信確認してから追加（取れたものだけ有効で足す。取れなかったものは足さず理由を出す）</span></label>
            <label className="flex items-start gap-2"><input type="radio" name="import-mode" checked={mode === "direct"} onChange={() => setMode("direct")} data-testid="news-import-mode-direct" />
              <span>そのまま追加（通信せず、すべて無効で足す。あとで確かめて有効にする）</span></label>
          </fieldset>
          <button type="button" className={`${btn} border-accent-600 font-semibold`} disabled={!choose || !!busy} onClick={() => void run()} data-testid="news-import-run">{choose} 件を追加</button>
        </div>
      )}

      {result && (
        <div className="rounded-xl border border-zinc-200 p-3 text-xs dark:border-zinc-700" role="status" data-testid="news-import-result">
          <p className="font-medium">{result.added} 件を足しました{result.mode === "direct" ? "（無効で。取得先の一覧で確かめて有効にしてください）" : ""}。重複 {result.duplicate} 件 · 使えない {result.invalid} 件 · 取れなかった {result.failed.length} 件</p>
          {result.failed.length > 0 && <ul className="mt-1 list-disc pl-4 text-zinc-500">{result.failed.slice(0, 20).map((f) => <li key={f.url} className="break-all">{f.name || f.url}: {f.reason}</li>)}</ul>}
        </div>
      )}

      <div className="space-y-2 rounded-xl border border-zinc-200 p-3 dark:border-zinc-700">
        <h2 className="text-sm font-semibold">書き出し</h2>
        <p className="text-xs text-zinc-500">無効の取得先も含めます。書き出したファイルは、そのまま取り込めます（Hacker News・Hugging Face は URL が無いので含みません）。</p>
        <div className="flex flex-wrap gap-2">
          <a className={`${btn} inline-flex items-center`} href="/api/v1/news/sources/export?format=opml" download data-testid="news-export-opml">OPML で書き出す</a>
          <a className={`${btn} inline-flex items-center`} href="/api/v1/news/sources/export?format=csv" download data-testid="news-export-csv">CSV で書き出す</a>
        </div>
      </div>
    </section>
  );
}
