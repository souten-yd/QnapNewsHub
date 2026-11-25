/** Things（旧「物」）の全体の地図・主題の入口（MOC）・つなぐ候補の API（backend/qnapnewshub/things_router.py と対）。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { EntityType } from "./news";
import type { GraphEdge, KnowledgeCardData, RelStatus, RelType } from "./kg";

export interface OverviewNode { id: number; name: string; type: EntityType; article_count: number; degree: number; recent: number; community: number }
export interface OverviewEdge { id: number; source: number; target: number; type: RelType; directed: boolean; confidence: number; status: RelStatus; evidence_count: number; last_at: string }
export interface CommunityBrief { id: number; name: string; size: number; rep_id: number; shown: number; top: { id: number; name: string }[] }
export interface Overview {
  nodes: OverviewNode[]; edges: OverviewEdge[]; isolated: OverviewNode[]; communities: CommunityBrief[]; truncated: boolean; limit: number;
  totals: { with_relations: number; edges: number; shown_nodes: number; shown_edges: number };
}
export interface CommunityMembers { id: number; name: string; size: number; nodes: OverviewNode[]; edges: OverviewEdge[] }

export const useOverview = () => useQuery({ queryKey: ["things", "overview"], queryFn: () => api<Overview>("/things/map/overview?limit=1500&isolated=200"), staleTime: 60_000, retry: false });
export const communityMembers = (id: number) => api<CommunityMembers>(`/things/map/community/${id}?limit=300`);
export type { GraphEdge };

export interface MocThing { id: number; name: string; type: EntityType; article_count: number; last_seen: string; definition: string; recent_articles?: number }
export interface MocEvent { id: number; title: string; n_articles: number; last_at: string; summary: string; things: string[] }
export interface Moc { kind: "community" | "channel"; id: number | string; title: string; size: number; things: MocThing[]; events: MocEvent[]; questions: { entity_id: number; entity: string; question: string }[] }
export interface MocIndex { communities: { id: number; name: string; size: number }[]; channels: { id: string; name: string; color: string }[] }
export const useMocIndex = () => useQuery({ queryKey: ["things", "moc"], queryFn: () => api<MocIndex>("/things/moc"), retry: false });
export const useMoc = (kind: string, id: string) => useQuery({ queryKey: ["things", "moc", kind, id], queryFn: () => api<Moc>(`/things/moc/${kind}/${id}`), retry: false });

export function useDecideMention(entityId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ articleId, accept }: { articleId: number; accept: boolean }) => api<{ status: string }>(`/things/${entityId}/mentions/${articleId}/${accept ? "confirm" : "dismiss"}`, { method: "POST", json: {} }),
    onSuccess: (_r, v) => {
      qc.setQueryData<KnowledgeCardData>(["kg", "card", entityId], (c) => (c ? { ...c, mentions: c.mentions.filter((m) => m.article_id !== v.articleId) } : c));
      void qc.invalidateQueries({ queryKey: ["entities"] });
    },
  });
}

/** 名寄せ（同じ物の別表記）。確かなものは夜に自動で寄せ、迷うものは候補として管理者が確定する。寄せた記録は元に戻せる。 */
export interface MergeSide { id: number; name: string; type: EntityType; article_count: number }
export interface MergeCandidate { id: number; reason: string; score: number; keep: MergeSide; drop: MergeSide }
export interface MergeRecord { id: number; winner: { id: number; name: string }; loser: { id: number; name: string }; mode: "auto" | "confirmed"; reason: string; merged_at: string; undone: boolean; can_undo: boolean }
export const MERGE_REASON: Record<string, string> = { rule_type: "表記は同じだが種類が違う", rule_paren: "括弧を除くと同じ", cross_script: "日本語と英語の表記（同じ出来事の記事に別々に出る）", rule: "表記の規則（会社の語尾・版の書き方）", undone: "戻した組" };
export const useMergeCandidates = () => useQuery({ queryKey: ["things", "merge"], queryFn: () => api<{ items: MergeCandidate[]; total: number; merges: MergeRecord[] }>("/things/merge-candidates?limit=30"), retry: false });
export function useMergeAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ path, json }: { path: string; json?: unknown }) => api<unknown>(`/things${path}`, { method: "POST", json: json ?? {} }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["things"] }); void qc.invalidateQueries({ queryKey: ["entities"] }); },
  });
}
