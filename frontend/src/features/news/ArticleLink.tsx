import type { ReactNode } from "react";
import type { NewsItem } from "../../api/news";

const external = (url: string) => /^https?:\/\//.test(url);

/** 記事へのリンク。ニュースハブにはリーダーが無いので、いつも元のページを新しいタブで開く（noopener noreferrer）。
 *  開いたら onOpen（好みの記録）。url の無い記事は文字だけ。 */
export function ArticleLink({ item, onOpen, className, label, children }: {
  item: NewsItem; onOpen?: () => void; className?: string; label?: string; children: ReactNode;
}) {
  if (!item.url) return <>{children}</>;
  return <a href={item.url} className={className} aria-label={label} {...(external(item.url) ? { target: "_blank", rel: "noopener noreferrer" } : {})} onClick={onOpen} onAuxClick={onOpen}>{children}</a>;
}
