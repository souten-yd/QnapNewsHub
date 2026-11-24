import { BodyMark, TranslatedMark } from "./TranslatedMark";
import { useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { ENTITY_TYPE_LABEL, KIND_STYLE, relativeTime, useNewsAction, type NewsAction, type NewsItem } from "../../api/news";
import { useToasts } from "../../stores";
import { chNo } from "./newsTheme";
import { ArticleLink } from "./ArticleLink";

export interface CardChannel { id: string; name: string; color: string; number: number }

/** 16:9 の画像。無い・読めないときは画像の部分を出さず、チャンネルの色の細いアクセントライン（高さ 5px）だけにする
 *  （2026-11-23 利用者「無意味なアイコンを貼ったデカデカとしたエリア…画像がないんだったらアクセントラインだけにして、表示を小さくする」）。 */
export function Media({ item, color, className = "card-media", children }: { item: NewsItem; color?: string; className?: string; children?: React.ReactNode }) {
  const [broken, setBroken] = useState(false);
  const has = Boolean(item.image_url) && !broken;
  return (
    <div className={`${className}${has ? "" : " nw-noimg"}`} data-noimg={has ? undefined : "true"} style={color ? ({ "--ch": color } as CSSProperties) : undefined}>
      {has && <img src={item.image_url!} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setBroken(true)} />}
      {has && item.image_ai && <span className="nw-ai" data-testid="ai-image">AI 生成のイメージ</span>}
      {has && children}
    </div>
  );
}


/** 放送局のカード。label は NOW・TODAY・THIS WEEK など（札）。read は既読を薄くするとき。 */
export function NewsCard({ item, channel, label, hideRemoves = false, dimRead = false, onTopic }: {
  item: NewsItem; channel?: CardChannel | null; label?: string; hideRemoves?: boolean; dimRead?: boolean; onTopic?: (topic: string) => void;
}) {
  const act = useNewsAction();
  const toast = useToasts((s) => s.show);
  const style = KIND_STYLE[item.kind] ?? KIND_STYLE.news;
  const color = channel?.color;
  const send = (action: NewsAction, on = true) => act.mutate({ id: item.id, action, on });
  const toggle = (action: NewsAction) => send(action, !item.state[action]);
  const hide = () => {
    send("hide", true);
    if (hideRemoves) toast("隠しました", "info", { label: "元に戻す", onClick: () => send("hide", false) });
  };
  const open = () => send("open");
  return (
    <article data-news-card className={`nw-card${dimRead && item.state.open ? " read" : ""}`} style={color ? ({ "--ch": color } as CSSProperties) : undefined}>
      <Media item={item} color={color}>{label && <span className="nw-badge">{label}</span>}</Media>
      <div className="card-body">
        <p className="card-meta">
          <span className="dot" aria-hidden />
          {channel && <span className="nw-kicker" style={{ color: "inherit" }}>{chNo(channel.number)}</span>}
          <span>{style.icon} {style.label}</span>
          <span className="src">{item.source}</span>
          <span>· {relativeTime(item.published_at)}</span>
          <BodyMark item={item} />
          {!!item.event && item.event.others > 0 && <Link to={`/events/${item.event.id}`} data-testid="news-event" title={`同じ出来事を報じる記事のまとまり（出どころ ${item.event.sources}）`} className="font-semibold text-accent-700 dark:text-accent-300">· ほか {item.event.others} 件{item.event.primary ? "（一次情報あり）" : ""}</Link>}
          {!!item.also && !item.event && <span title={(item.also_sources ?? []).join("、")} data-testid="news-also">· ほか {item.also} か所が報道</span>}
        </p>
        <h3 className="card-title"><ArticleLink item={item} onOpen={open}>{item.title}</ArticleLink></h3>
        {item.summary && <p className="card-sum">{item.summary}</p>}
        <TranslatedMark item={item} />
        {item.topics.length > 0 && (
          <ul className="card-chips" aria-label="話題">
            {item.topics.slice(0, 4).map((topic) => <li key={topic}><button type="button" onClick={() => onTopic?.(topic)}>{topic}</button></li>)}
          </ul>
        )}
        {!!item.entities?.length && (
          <ul className="card-ents" aria-label="物（AI の取り出し）" data-testid="card-entities">
            {item.entities.map((e) => <li key={e.id}><Link to={`/entities/${e.id}`} title={`${ENTITY_TYPE_LABEL[e.type] ?? e.type}（AI の取り出し）`}>{e.name}</Link></li>)}
          </ul>
        )}
        <div className="card-foot">
          {item.url ? <ArticleLink item={item} onOpen={open} className="card-read" label="読む →">読む →</ArticleLink> : <span />}
          {<div className="card-acts" role="group" aria-label="操作">
            <button type="button" aria-pressed={item.state.like} onClick={() => toggle("like")} title="もっと見たい">👍<span className="sr-only"> もっと</span></button>
            <button type="button" aria-pressed={item.state.dislike} onClick={() => toggle("dislike")} title="興味なし">👎<span className="sr-only"> 興味なし</span></button>
            <button type="button" aria-pressed={item.state.star} onClick={() => toggle("star")} title="保存">{item.state.star ? "★" : "☆"}<span className="sr-only"> 保存</span></button>
            <button type="button" onClick={hide}>隠す</button>
          </div>}
        </div>
      </div>
    </article>
  );
}
