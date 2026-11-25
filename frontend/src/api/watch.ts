/** ウォッチリストと変化の要約の API の型と取得（backend/qnapnewshub/watch_router.py と対）。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { EntityType } from "./news";

export interface WatchRow { id: number; entity: { id: number; name: string; type: EntityType }; created_at: string; seen_at: string }
export interface WatchClaim { id: number; kind: string; text: string; at: string; value?: string; cond?: Record<string, string>; src?: string; grounded?: boolean; doc?: number }
export interface WatchMetric { subject: string; metric: string; value: number; unit: string; at: string; cond?: string; previous?: number; pct?: number | null }
export interface WatchRelation { id: number; type: string; subject: string; object: string; conf: number; status: string; at: string }
export interface WatchVersion { doc: number; kind: string; title: string; version: string; at: string; src: string; url: string }
export interface WatchEvent { id: number; title: string; n_articles: number; n_sources: number; has_primary: boolean; last_at: string }
export interface WatchPoint { kind: "metric" | "mentions"; line: string; direction: "up" | "down" }
export interface WatchChange {
  thing: { id: number; name: string; type: EntityType }; since: string; claims: WatchClaim[]; metrics: WatchMetric[]; relations: WatchRelation[]; versions: WatchVersion[];
  events: WatchEvent[]; change_points: WatchPoint[]; quiet: boolean;
}
export interface WatchChanges { generated_at: string; watched: boolean; items: WatchChange[]; changed: number; llm: boolean }

/** 条件の欄の日本語（backend/qnapnewshub/bench.py の COND_LABEL と同じ）。 */
export const COND_LABEL: Record<string, string> = { model: "モデル", quant: "量子化", hw: "GPU・端末", engine: "エンジン", ctx: "文脈の長さ", version: "版" };

const KEY = ["watch"] as const;
export const useWatch = () => useQuery({ queryKey: [...KEY, "list"], queryFn: () => api<{ items: WatchRow[] }>("/watch"), retry: false });
export const useWatchChanges = () => useQuery({ queryKey: [...KEY, "changes"], queryFn: () => api<WatchChanges>("/watch/changes"), retry: false });

export function useWatchActions() {
  const qc = useQueryClient();
  const done = () => qc.invalidateQueries({ queryKey: KEY });
  return {
    add: useMutation({ mutationFn: (entityId: number) => api<WatchRow>("/watch", { method: "POST", json: { entity_id: entityId } }), onSuccess: done }),
    remove: useMutation({ mutationFn: (entityId: number) => api<{ removed: boolean }>(`/watch/${entityId}`, { method: "DELETE" }), onSuccess: done }),
    seen: useMutation({ mutationFn: (entityIds: number[] | null) => api<{ marked: number }>("/watch/seen", { method: "POST", json: { entity_ids: entityIds } }), onSuccess: done }),
  };
}
