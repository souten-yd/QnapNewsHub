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

/** 孤立した点は、全体の外側の輪に並べる。 */
export function ringPositions(ids: number[], radius: number): Map<number, Pos> {
  const out = new Map<number, Pos>();
  ids.forEach((id, i) => { const a = (i / Math.max(1, ids.length)) * Math.PI * 2; out.set(id, { x: radius * Math.cos(a), y: radius * Math.sin(a) }); });
  return out;
}
