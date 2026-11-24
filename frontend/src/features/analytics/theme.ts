import { useEffect, useState } from "react";

/** 色は分類ごとに一定: 同じ分類・チャンネル・取得先は、どのグラフでも同じ色。 */
export const PALETTE = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16"];
export const OTHER_COLOR = "#94a3b8";
const KIND_COLORS: Record<string, string> = { news: PALETTE[0], release: PALETTE[2], model: PALETTE[4], forum: PALETTE[1], reddit: PALETTE[3] };
const LANG_COLORS: Record<string, string> = { ja: PALETTE[3], en: PALETTE[0] };
const ORIGIN_COLORS: Record<string, string> = { official: PALETTE[2], press: PALETTE[0], blog: PALETTE[1], community: PALETTE[4], paper: PALETTE[5], other: OTHER_COLOR };

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}
export function colorOf(by: string, key: string, given?: string): string {
  if (key === "_other" || key === "-") return OTHER_COLOR;
  if (given) return given;
  if (by === "kind" && KIND_COLORS[key]) return KIND_COLORS[key];
  if (by === "lang" && LANG_COLORS[key]) return LANG_COLORS[key];
  if (by === "origin" && ORIGIN_COLORS[key]) return ORIGIN_COLORS[key];
  if (by === "none") return PALETTE[0];
  return PALETTE[hash(key) % PALETTE.length];
}

/** ライト・ダークの切り替え（html の dark クラス）を見る。グラフの文字・目盛り・枠の色もこれで変える。 */
export function useIsDark(): boolean {
  const read = () => document.documentElement.classList.contains("dark");
  const [dark, setDark] = useState(read);
  useEffect(() => {
    const ob = new MutationObserver(() => setDark(read()));
    ob.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => ob.disconnect();
  }, []);
  return dark;
}
export const chartColors = (dark: boolean) => ({ text: dark ? "#d4d4d8" : "#3f3f46", axis: dark ? "#52525b" : "#d4d4d8", grid: dark ? "#3f3f46" : "#e4e4e7", bg: "transparent", tip: dark ? "#27272a" : "#ffffff" });
