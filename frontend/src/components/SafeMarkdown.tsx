import type { ReactNode } from "react";

/** Release のノート用の小さな Markdown 表示。HTML は解釈しない（すべて文字として出す）。リンクは https だけ。
 *  対応: 見出し（#〜###）・箇条書き（- * 1.）・コード（``` と `）・太字（**）・リンク（[文](https://…)）。 */
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https:\/\/[^\s)]+\))/g;

function inline(text: string, key: string): ReactNode[] {
  return text.split(INLINE).filter(Boolean).map((part, i) => {
    const k = `${key}-${i}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={k}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={k} className="rounded bg-zinc-100 px-1 font-mono text-[0.92em] dark:bg-zinc-800">{part.slice(1, -1)}</code>;
    const link = /^\[([^\]]+)\]\((https:\/\/[^\s)]+)\)$/.exec(part);
    if (link) return <a key={k} href={link[2]} target="_blank" rel="noopener noreferrer nofollow" className="text-accent-700 underline dark:text-accent-300">{link[1]}</a>;
    return part;
  });
}

export function SafeMarkdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      out.push(<pre key={`p${i}`} className="overflow-x-auto rounded-lg bg-zinc-100 p-2 font-mono text-[11px] dark:bg-zinc-800">{code.join("\n")}</pre>);
      continue;
    }
    const head = /^(#{1,3})\s+(.*)$/.exec(line);
    if (head) { out.push(<p key={`h${i}`} className={`mt-2 font-semibold ${head[1].length === 1 ? "text-sm" : "text-xs"}`}>{inline(head[2], `h${i}`)}</p>); i++; continue; }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const items: ReactNode[] = [];
      const start = i;
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) { items.push(<li key={`l${i}`}>{inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""), `l${i}`)}</li>); i++; }
      out.push(<ul key={`u${start}`} className="list-disc space-y-0.5 pl-5">{items}</ul>);
      continue;
    }
    if (line.trim() === "") { i++; continue; }
    out.push(<p key={`t${i}`}>{inline(line, `t${i}`)}</p>);
    i++;
  }
  return <div className="space-y-1.5 break-words text-xs leading-relaxed" data-testid="release-notes">{out}</div>;
}
