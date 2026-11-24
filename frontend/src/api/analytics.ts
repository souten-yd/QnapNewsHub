/** 分析 API の型と取得（backend/qnapnewshub/analytics_router.py と対）。集計の表だけを読む。 */
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export type Range = "24h" | "7d" | "30d" | "90d";
export type By = "none" | "kind" | "channel" | "source" | "lang" | "origin";
export interface Filters { kind: string; lang: string }

export interface Overview {
  today: number; today_indep: number; last24h: number; last24h_indep: number; sources_today: number; sources_8d: number;
  prev7_daily_avg: number | null; prev7_days: number; ratio_24h: number | null; ratio_today: number | null; day: string; built_at: string; unsaved: number;
}
export interface SeriesItem { key: string; label: string; counts: number[]; total: number; color?: string; other_n?: number }
export interface Series { range: Range; grain: "hour" | "day"; by: By; metric: "all" | "indep"; buckets: string[]; series: SeriesItem[]; total: number }
export interface HotTopic {
  term: string; label: string; score: number | null; count: number; count_all: number; baseline_mean: number; baseline_sd: number; growth_pct: number | null; sources: number; spark: number[];
}
export interface HotTopics {
  window_hours: number; baseline_days: number; enough_history: boolean; spark_days: string[]; top: HotTopic[]; new: HotTopic[];
  fields: { channel: string; name: string; color: string; top: HotTopic[] }[]; method: string;
}
export interface SourceRow {
  key: string; name: string; origin: string; articles: number; independent: number; runs: number; success_pct: number | null; not_modified_pct: number | null;
  image_pct: number | null; dup_pct: number | null; new: number; daily: number[];
}
export interface SourcesAnalysis { range: Range; days: string[]; sources: SourceRow[]; total_sources: number }
export interface CompareRow { key: string; label: string; this: number; prev: number; this_indep: number; prev_indep: number; delta: number; pct: number | null }
export interface Compare { by: string; this_week: string[]; prev_week: string[]; rows: CompareRow[]; total_this: number; total_prev: number }
export interface AnalyticsFilters { kinds: { id: string; label: string }[]; langs: { id: string; label: string }[]; channels: { id: string; name: string; color: string }[] }

const key = ["analytics"] as const;
const qs = (o: Record<string, string>) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== "")).toString();
const opts = { staleTime: 60_000, retry: false } as const;

export const useAnalyticsFilters = () => useQuery({ queryKey: [...key, "filters"], queryFn: () => api<AnalyticsFilters>("/analytics/filters"), staleTime: 300_000 });
export const useOverview = (f: Filters) => useQuery({ queryKey: [...key, "overview", f], queryFn: () => api<Overview>(`/analytics/overview?${qs({ ...f })}`), ...opts });
export const useSeries = (p: { range: Range; grain: string; by: By; metric: string } & Filters) =>
  useQuery({ queryKey: [...key, "series", p], queryFn: () => api<Series>(`/analytics/series?${qs({ ...p })}`), ...opts });
export const useHotTopics = (f: Filters) => useQuery({ queryKey: [...key, "hot", f], queryFn: () => api<HotTopics>(`/analytics/hot-topics?${qs({ ...f })}`), ...opts });
export const useSourcesAnalysis = (p: { range: Range } & Filters) => useQuery({ queryKey: [...key, "sources", p], queryFn: () => api<SourcesAnalysis>(`/analytics/sources?${qs({ ...p })}`), ...opts });
export const useCompare = (p: { by: string } & Filters) => useQuery({ queryKey: [...key, "compare", p], queryFn: () => api<Compare>(`/analytics/compare?${qs({ ...p })}`), ...opts });
