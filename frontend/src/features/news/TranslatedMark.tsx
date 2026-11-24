/** 英語の抜粋を訳した概要の印。開くと元の抜粋が見られる（題は訳さない）。 */
import type { NewsItem } from "../../api/news";

export function TranslatedMark({ item }: { item: Pick<NewsItem, "translated" | "summary_original"> }) {
  if (!item.translated) return null;
  return (
    <details className="nw-orig" data-testid="translated-mark">
      <summary aria-label="訳（元の抜粋を見る）">訳</summary>
      {item.summary_original && <p lang="en">{item.summary_original}</p>}
    </details>
  );
}

/** 本文を取り出してある記事の印（本文はニュースハブでは出さない。元のページを開く）。 */
export function BodyMark({ item }: { item: Pick<NewsItem, "has_body"> }) {
  if (!item.has_body) return null;
  return <span className="nw-body" data-testid="body-mark" title="本文を取り出して保存してあります（下ごしらえと検索の質に使います。本文は元のページで読めます）">本文あり</span>;
}
