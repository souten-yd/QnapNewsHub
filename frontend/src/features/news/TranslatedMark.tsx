/** 機械翻訳・LLM の訳の印。開くと原文（題・抜粋）が見られる。機械翻訳には本文の訳（あれば）と「本文を訳す」ボタン（無ければ要求）も出す。 */
import { useState } from "react";
import type { NewsItem } from "../../api/news";
import { useArticleTranslation, useRequestTranslation } from "../../api/translate";
import { useToasts } from "../../stores";

/** 画面に出す題。機械翻訳の題があればそれ（原文は印を開くと見られる）。 */
export function shownTitle(item: Pick<NewsItem, "title" | "mt">): string {
  return item.mt?.title || item.title;
}

function MtBody({ item }: { item: NewsItem }) {
  const toast = useToasts((s) => s.show);
  const [open, setOpen] = useState(false);
  const got = useArticleTranslation(item.id, open);
  const ask = useRequestTranslation(item.id);
  const body = got.data?.body_ja || "";
  return (
    <div className="mt-1" data-testid="mt-body">
      {!open ? (
        <button type="button" className="text-xs font-medium text-accent-700 underline dark:text-accent-300" onClick={() => setOpen(true)}>{item.mt?.body ? "本文の訳を見る" : "本文を訳す"}</button>
      ) : body ? (
        <div className="whitespace-pre-wrap text-xs leading-relaxed" lang="ja">
          {body}
          {got.data?.partial && <p className="mt-1 text-amber-700 dark:text-amber-400">{got.data.untranslated_mark} が付いた文は検査に通らなかったので原文のままです。</p>}
        </div>
      ) : (
        <button type="button" disabled={ask.isPending || got.isLoading} className="text-xs font-medium text-accent-700 underline disabled:opacity-50 dark:text-accent-300"
          onClick={() => ask.mutate(undefined, { onError: (e) => toast(e instanceof Error ? e.message : "訳せませんでした", "error") })}>{ask.isPending ? "訳しています…（十数秒かかります）" : "本文を訳す"}</button>
      )}
    </div>
  );
}

export function TranslatedMark({ item }: { item: NewsItem }) {
  if (!item.translated && !item.mt) return null;
  const mt = item.mt;
  const label = mt ? (mt.hq ? "高品質の機械翻訳" : "機械翻訳") : "訳";
  return (
    <details className="nw-orig" data-testid={mt ? "mt-mark" : "translated-mark"}>
      <summary aria-label={`${label}（原文を見る）`}>{label}</summary>
      {mt?.title && mt.title !== item.title && <p lang="en" data-testid="mt-original-title">{item.title}</p>}
      {item.summary_original && <p lang="en">{item.summary_original}</p>}
      {mt && <p className="text-xs text-zinc-500">エンジン: {mt.engine}{mt.partial ? "（訳せなかった文があります）" : ""}</p>}
      {mt && <MtBody item={item} />}
    </details>
  );
}

/** 本文を取り出してある記事の印（本文はニュースハブでは出さない。元のページを開く）。 */
export function BodyMark({ item }: { item: Pick<NewsItem, "has_body"> }) {
  if (!item.has_body) return null;
  return <span className="nw-body" data-testid="body-mark" title="本文を取り出して保存してあります（下ごしらえと検索の質に使います。本文は元のページで読めます）">本文あり</span>;
}
