/** まとめの本文から概要の行を取り出す（ControlDeck の NewsHomeCard.tsx から移した部分。ホームのカードはニュースハブに無い）。 */

const clean = (line: string) => line.replace(/^[-・*]\s*/, "").replace(/\s*\[\d{1,3}\]/g, "").trim();
const TOP3 = /^#{1,4}\s*今日の\s*3\s*行/;

/** まとめの本文の概要の行。「## 今日の 3 行」があればその箇条書き（最大 4 行）、無ければ（古い形）最初の見出しでない 3 行。 */
export function digestLines(body: string): string[] {
  const rows = body.split("\n").map((l) => l.trim());
  const at = rows.findIndex((l) => TOP3.test(l));
  let picked: string[];
  if (at >= 0) {
    const rest = rows.slice(at + 1);
    const end = rest.findIndex((l) => l.startsWith("#"));
    picked = (end < 0 ? rest : rest.slice(0, end)).filter(Boolean);
  } else {
    picked = rows.filter((l) => l && !l.startsWith("#")).slice(0, 3);
  }
  return picked.map(clean).filter(Boolean).slice(0, 4);
}

