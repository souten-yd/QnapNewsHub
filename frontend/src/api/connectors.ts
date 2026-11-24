import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

/** 収集の connector（backend/qnapnewshub/connectors.py・connectors_router.py）。管理者だけ。鍵の値は返らない（has_key だけ）。 */
export interface ConnectorTerms {
  id: string; status: "confirmed" | "partial" | "per_site" | "unconfirmed"; license: string; storage: string; redistribution: "allowed" | "conditional" | "forbidden" | "unknown";
  rate: string; tos_url: string; notes: string; checked_at: string; ai_use: string;
}
export interface ConnectorLast { at: string; ok: boolean; count: number; error: string; skipped: string; ms: number; note?: string; stopped?: boolean }
export type SettingValue = string[] | number | boolean | string | { name: string; url: string }[];
export interface Connector {
  id: string; kind: string; kind_label: string; label: string; desc: string; adapted: boolean; scheduled: boolean; enabled: boolean; interval_minutes: number;
  daily_items: number; daily_mb: number; settings: Record<string, SettingValue>; has_key_option: boolean; has_key: boolean; terms_ack: boolean; needs_ack: boolean; terms: ConnectorTerms;
  usage: { day: string; items: number; mb: number; requests: number }; budget_stop: { at: string; reason: string } | null; last: ConnectorLast | null; last_ok_at: string;
}
export interface ConnectorList {
  connectors: Connector[]; kinds: Record<string, string>; promotion: Record<string, boolean | number>; tiers: Record<string, Record<string, number>>; tier_labels: Record<string, string>;
  rest: boolean; limits: { interval: [number, number]; daily_items: number; daily_mb: number };
}
export interface ConnectorPatch {
  enabled?: boolean; interval_minutes?: number; daily_items?: number; daily_mb?: number; terms_ack?: boolean; settings?: Record<string, SettingValue>;
}

const KEY = ["connectors"] as const;
export const useConnectors = () => useQuery({ queryKey: KEY, queryFn: () => api<ConnectorList>("/connectors"), staleTime: 10_000 });

export function useConnectorMutations() {
  const qc = useQueryClient();
  const done = () => qc.invalidateQueries({ queryKey: KEY });
  return {
    patch: useMutation({ mutationFn: (v: { id: string; body: ConnectorPatch }) => api<Connector>(`/connectors/${v.id}`, { method: "PUT", json: v.body }), onSuccess: done }),
    promotion: useMutation({ mutationFn: (body: Record<string, boolean | number>) => api("/connectors/promotion", { method: "PUT", json: body }), onSuccess: done }),
    setKey: useMutation({ mutationFn: (v: { id: string; value: string }) => api(`/connectors/${v.id}/key`, { method: "PUT", json: { value: v.value } }), onSuccess: done }),
    delKey: useMutation({ mutationFn: (id: string) => api(`/connectors/${id}/key`, { method: "DELETE" }), onSuccess: done }),
    run: useMutation({ mutationFn: (id: string) => api<{ ran: boolean; reason: string; count: number; error?: string }>(`/connectors/${id}/run`, { method: "POST" }), onSuccess: done }),
  };
}
