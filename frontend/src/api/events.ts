/** イベント API の型と取得（backend/qnapnewshub/events_router.py と対）。イベント＝同じ出来事を報じる 2 件以上の記事のまとまり。 */
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { EntityType } from "./news";

export interface EventEntity { id: number; name: string; type: EntityType; n: number }
export interface EventRow {
  id: number; rep_id: number; title: string; n_articles: number; n_sources: number; has_primary: boolean; first_at: string; last_at: string;
  entities: EventEntity[]; summary: string; summary_model: string;
}
export interface EventArticle {
  id: number; title: string; url: string | null; source: string; source_key: string; kind: string; origin: string; origin_label: string;
  published_at: string; summary: string; is_rep: boolean; is_dup: boolean;
}
export interface EventDetail extends EventRow { articles: EventArticle[]; origins: { origin: string; label: string; n: number }[]; hours_since_first: number }
export interface EventList { items: EventRow[]; total: number }
export type EventSort = "recent" | "size" | "first";

const keyOf = ["events"] as const;
export const useEvents = (p: { sort: EventSort; days: number; primary: boolean }) =>
  useQuery({ queryKey: [...keyOf, "list", p], queryFn: () => api<EventList>(`/events?sort=${p.sort}&days=${p.days}&limit=50${p.primary ? "&primary=true" : ""}`), staleTime: 30_000, retry: false });
export const useEvent = (id: number) => useQuery({ queryKey: [...keyOf, id], queryFn: () => api<EventDetail>(`/events/${id}`), enabled: Number.isFinite(id), staleTime: 30_000, retry: false });

/** 初報からの時間（「3 時間前」「2 日前」）。 */
export function sinceLabel(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} 分前`;
  if (hours < 48) return `${Math.round(hours)} 時間前`;
  return `${Math.round(hours / 24)} 日前`;
}
export const hoursSince = (iso: string, now = Date.now()) => Math.max(0, (now - new Date(iso).getTime()) / 3_600_000);
