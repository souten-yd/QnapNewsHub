/** 知識グラフ API の型と取得（backend/qnapnewshub/kg_router.py と対）。読む API は既定で public の範囲だけ。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "./client";
import type { EntityType } from "./news";

export const REL_TYPES = ["uses", "supports", "replaces", "competes", "part_of", "implements", "cites", "contradicts", "co_occurs", "other"] as const;
export type RelType = (typeof REL_TYPES)[number];
export const REL_LABEL: Record<RelType, string> = {
  uses: "使う", supports: "対応する", replaces: "置き換える", competes: "競う", part_of: "一部", implements: "実装する", cites: "引用する",
  contradicts: "矛盾する", co_occurs: "一緒に出る", other: "その他の関係",
};
/** 型ごとの色（ライト・ダークの両方で背景と 3:1 以上。線は色だけに頼らず、推測・集計は破線にする）。 */
export const REL_COLOR: Record<RelType, string> = {
  uses: "#2563eb", supports: "#0d9488", replaces: "#d97706", competes: "#dc2626", part_of: "#7c3aed", implements: "#0891b2", cites: "#65a30d",
  contradicts: "#be185d", co_occurs: "#71717a", other: "#a1a1aa",
};
export type RelStatus = "extracted" | "inferred" | "statistical" | "confirmed";
export const STATUS_LABEL: Record<RelStatus, string> = { extracted: "AI の取り出し", inferred: "推測", statistical: "集計", confirmed: "確認済み" };

export interface GraphNode { id: number; name: string; type: EntityType; article_count: number; sensitivity: string; last_seen: string }
export interface GraphEdge {
  id: number; source: number; target: number; type: RelType; directed: boolean; confidence: number; status: RelStatus; evidence_count: number;
  first_at: string; last_at: string; sensitivity: string;
}
export interface NeighborsResult { seeds: GraphNode[]; levels: { depth: number; nodes: GraphNode[]; edges: GraphEdge[] }[]; truncated: boolean }
export interface NeighborsQuery { ids: number[]; types?: string[]; depth?: number; limit?: number; min_confidence?: number; kinds?: string[]; since?: string | null }

export const neighbors = (q: NeighborsQuery) => api<NeighborsResult>("/graph/neighbors", { method: "POST", json: q });
export const useSeeds = () => useQuery({ queryKey: ["graph", "seeds"], queryFn: () => api<{ items: GraphNode[] }>("/graph/seeds?limit=12"), staleTime: 60_000, retry: false });

export interface CardEvidence { document: { id: number; kind: string; title: string; url: string }; quote: string; start: number | null; end: number | null; page: number | null; quote_hash: string; grounded: boolean }
export interface CardClaim { id: number; kind: string; text: string; value: string; ai_extracted: boolean; model: string; sensitivity: string; evidences: CardEvidence[] }
export interface CardRelation extends GraphEdge { direction: "out" | "in" | "both"; other: { id: number; name: string; type: EntityType }; status_label: string }
export interface HistoryEntry { claim_id: number; t: string; value: string; text: string; title: string; url: string; mark: "first" | "same" | "change" | "contradiction"; from?: string; ai_extracted: boolean }
export interface KnowledgeCardData {
  id: number; name: string; type: EntityType; sensitivity: string; ai_use: string;
  overview: { text: string; model: string; at: string; ai: boolean } | null;
  elements: { aliases: string[]; first_seen: string; last_seen: string; article_count: number; topics: string[] };
  timeline: { id: number; title: string; url: string | null; source: string; published_at: string }[];
  claims: CardClaim[]; relations: CardRelation[];
  history: { series: { kind: string; entries: HistoryEntry[] }[]; changes: number; contradictions: number };
  note: { body: string; updated_at: string };
}
export const useCard = (id: number) => useQuery({ queryKey: ["kg", "card", id], queryFn: () => api<KnowledgeCardData>(`/kg/entities/${id}/card`), enabled: Number.isFinite(id), staleTime: 30_000, retry: false });
export function useSaveNote(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: string) => api<{ body: string; updated_at: string }>(`/kg/entities/${id}/note`, { method: "PUT", json: { body } }),
    onSuccess: (note) => qc.setQueryData<KnowledgeCardData>(["kg", "card", id], (c) => (c ? { ...c, note } : c)),
  });
}

export interface ExportQuery { ids?: number[]; types?: string[]; since?: string | null; min_articles?: number; limit?: number; include_notes?: boolean; confidential?: boolean }
/** 書き出し（zip）。範囲を指定して押したときだけ呼ぶ。 */
export async function exportZip(q: ExportQuery): Promise<{ blob: Blob; files: number }> {
  const res = await fetch("/api/v1/kg/export", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Requested-With": "NewsHub" }, body: JSON.stringify(q) });
  if (!res.ok) throw new ApiError(res.status, "書き出せませんでした");
  return { blob: await res.blob(), files: Number(res.headers.get("X-Export-Files") ?? 0) };
}
