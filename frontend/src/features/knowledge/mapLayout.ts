/** 全体の地図の配置（サーバーが決めた「まとまり」ごとに塊を作る。Cytoscape の cose を 1,500 点に流さない＝開いて 1 秒以内に絵が出るように）。
 *  - まとまり（community）を大きい順に、らせん（黄金角）の上へ、重ならない位置に置く。まとまり 0（どれにも入らない）は最後。
 *  - 塊の中は次数の多い順にひまわりの種の並び（中心ほど次数が大きい）。
 *  - あとから増えた点（検索・まとまりを開く）は、つながる点の近く（無ければ塊の外側）へ置く。
 *  純粋な関数（ブラウザに依存しない）。同じ入力は同じ結果。 */
export interface LayoutNode { id: number; community: number; degree: number }
export interface Pos { x: number; y: number }
const GOLDEN = 2.399963229728653;
const SPACING = 24;

export const groupRadius = (n: number) => SPACING * Math.sqrt(Math.max(1, n)) + 24;

export function layoutOverview(nodes: LayoutNode[]): { pos: Map<number, Pos>; centers: Map<number, Pos & { r: number }> } {
  const groups = new Map<number, LayoutNode[]>();
  for (const n of nodes) {
    const key = n.community || 0;
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(n);
  }
  const order = [...groups.keys()].sort((a, b) => (a === 0 ? 1 : b === 0 ? -1 : groups.get(b)!.length - groups.get(a)!.length || a - b));
  const centers = new Map<number, Pos & { r: number }>();
  const placed: (Pos & { r: number })[] = [];
  for (const key of order) {
    const r = groupRadius(groups.get(key)!.length);
    let c: Pos = { x: 0, y: 0 };
    if (placed.length) {
      for (let t = 1; t < 4000; t += 1) {
        const rad = 12 * Math.sqrt(t) * 3, ang = t * GOLDEN;
        c = { x: rad * Math.cos(ang), y: rad * Math.sin(ang) };
        if (placed.every((p) => Math.hypot(p.x - c.x, p.y - c.y) >= p.r + r + 30)) break;
      }
    }
    const entry = { ...c, r };
    centers.set(key, entry);
    placed.push(entry);
  }
  const pos = new Map<number, Pos>();
  for (const [key, members] of groups) {
    const c = centers.get(key)!;
    members.sort((a, b) => b.degree - a.degree || a.id - b.id).forEach((n, i) => {
      const rad = SPACING * Math.sqrt(i + 0.5), ang = i * GOLDEN;
      pos.set(n.id, { x: c.x + rad * Math.cos(ang), y: c.y + rad * Math.sin(ang) });
    });
  }
  return { pos, centers };
}

/** あとから増えた点の置き場所: つながる点（pos にある）の重心の近く。無ければ塊の中心の外側、それも無ければ原点の周り。 */
export function placeNear(id: number, neighbors: number[], pos: Map<number, Pos>, fallback?: Pos): Pos {
  const near = neighbors.map((n) => pos.get(n)).filter((p): p is Pos => !!p);
  const base = near.length ? { x: near.reduce((s, p) => s + p.x, 0) / near.length, y: near.reduce((s, p) => s + p.y, 0) / near.length } : fallback ?? { x: 0, y: 0 };
  const ang = ((id * 2654435761) % 360) * (Math.PI / 180), rad = 30 + (id % 7) * 6;
  return { x: base.x + rad * Math.cos(ang), y: base.y + rad * Math.sin(ang) };
}

/** 孤立した点（表示を選んだときだけ出る）は、全体の外側へ散らす（円には並べない。Obsidian の孤立ノードのように、周りにばらまく）。決まった式（同じ入力は同じ位置）。 */
export function scatterOutside(ids: number[], reach: number): Map<number, Pos> {
  const out = new Map<number, Pos>();
  ids.forEach((id) => {
    const h = Math.imul(id, 2654435761) >>> 0;
    const ang = ((h % 3600) / 3600) * Math.PI * 2;
    const u = ((Math.imul(id, 40503) >>> 0) % 1000) / 1000;
    const r = reach * (1.08 + 0.45 * u * u);
    out.set(id, { x: r * Math.cos(ang), y: r * Math.sin(ang) });
  });
  return out;
}

/** 点の半径（世界の単位。夜の配置の座標と同じ単位）。幅は 2〜9（最大は最小の約 4.5 倍）。重要度を cap（上位 5% の値）で割って平方根で縮め、cap 以上は頭打ち。
 *  backend/qnapnewshub/mapgrav.py の radius と同じ式（式を変えたら両方を直す。試験が文字列で確かめる）。 */
export const RADIUS_MIN = 2;
export const RADIUS_MAX = 9;
export const radiusOf = (importance: number, cap = 1) => RADIUS_MIN + (RADIUS_MAX - RADIUS_MIN) * Math.min(1, cap > 0 ? Math.sqrt(Math.max(0, importance) / cap) : 0);

/** 重要度の上位 5% の値（頭打ちの基準）。 */
export const sizeCap = (imps: number[]) => {
  const v = imps.filter((x) => x > 0).sort((a, b) => a - b);
  return v.length ? v[Math.min(v.length - 1, Math.floor(v.length * 0.95))] : 1;
};

/** 重要度が保存されていない点（夜の計算の前・あとから足した点）の代わり: 関係の数を log で縮めた 0〜0.6。 */
export const fallbackImportance = (degree: number, maxDegree: number) => (maxDegree > 0 ? (0.6 * Math.log1p(Math.max(0, degree))) / Math.log1p(maxDegree) : 0);

/** 色の分け方（まとまり／種類）。まとまりの色は、大きいまとまりから順にパレットを割り当てる。Advanced Graph View の Nebula のような青・紫・桃・金・水色。 */
export const CLUSTER_DARK = ["#6ea8ff", "#a78bfa", "#f472b6", "#fbbf24", "#22d3ee", "#fb7185", "#34d399", "#c4b5fd"];
export const CLUSTER_LIGHT = ["#2563eb", "#7c3aed", "#db2777", "#d97706", "#0891b2", "#e11d48", "#059669", "#6d28d9"];
export const NO_CLUSTER = { dark: "#8a8fb0", light: "#7c819b" };
