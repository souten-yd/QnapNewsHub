import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { UpdateLast } from "./update";

/** 診断（backend/qnapnewshub/diagnostics.py）。管理者だけ。鍵・URL の秘密は含まれない。 */
export interface DiagSource { key: string; name: string; kind: string; ok: boolean | null; reason: string; error: string; last_ok_at: string; last_at: string; interval_min: number; interval_auto: boolean; backoff: boolean; hit_rate: number | null; hits_304: number; conditional: number; new_7d: number; fail_streak: number; stalled: boolean; problems: string[]; fix: string }
export interface DiagModel { model: string; ok: number; fail: number; success_pct: number | null; rate_limited: number; avg_ms: number }
export interface DiagPoint { t: string; write_bytes: number; read_bytes: number; wchar: number; cpu_pct: number; rss: number }
export interface DiagFlush { at: string; reason: string; items: number; updates: number; patches: number; fetchlog: number; ok: boolean; error: string; ms: number; write_bytes: number }
export interface Diagnostics {
  generated_at: string;
  fetch: { total: number; stalled: number; hit_rate: number | null; hits_304: number; conditional: number; backoff: number; sources: DiagSource[] };
  llm: { day: string; calls: Record<string, number>; total: number; daily_limit: number; minute_limit: number; failed: number; last_error: string; key_set: boolean; models: DiagModel[]; rate_limited: number; unprocessed: number; prep_enabled: boolean; prep_model: string };
  storage: {
    io: { rchar: number; wchar: number; read_bytes: number; write_bytes: number; syscw: number }; series: DiagPoint[];
    last_flush: DiagFlush | null; last_flush_at: string; next_flush_at: string | null; flush_minutes: number; limits: { items: number; mb: number };
    unsaved: { items: number; updates: number; patches: number; fetchlog: number; states: number; total: number; bytes: number; inflight: number };
    flush_failures: number; last_error: string; files: { db: number; wal: number; auth_db: number; auth_wal: number }; disk: { free: number; total: number };
    rest: { enabled: boolean; active: boolean; mode: string; start: string; end: string; ends_at: string | null };
    rest_writes: { at: string; name: string; user: boolean }[]; rest_app_writes: number; writes_by_name: Record<string, number>; flush_history: DiagFlush[];
  };
  machine: { cpu_pct: number; load: number[]; rss: number; uptime_seconds: number; version: string; python: string; cpus: number };
  update: { version: string; install: string; can_install: boolean; checked_at: string; error: string; latest: string; include_prerelease: boolean; etag_hits: number; checks: number; job: string; last: UpdateLast | null; rollback: string | null };
  problems: { area: string; level: "error" | "warn"; text: string }[];
}
export interface SelfCheck { at: string; ok: boolean; checks: { name: string; ok: boolean; detail: string; fix: string }[] }
export interface DiagNotice { message: string | null; level: "error" | "warn" | null; count: number }

const key = ["diag"] as const;
export const useDiagnostics = () => useQuery({ queryKey: [...key, "all"], queryFn: () => api<Diagnostics>("/diagnostics"), refetchInterval: 30_000 });
export const useDiagNotice = () => useQuery({ queryKey: [...key, "notice"], queryFn: () => api<DiagNotice>("/diagnostics/notice"), staleTime: 60_000, retry: false });
export const useSelfCheck = () => useMutation({ mutationFn: () => api<SelfCheck>("/diagnostics/selfcheck", { method: "POST" }) });
export function useFlushNow() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api<DiagFlush>("/diagnostics/flush", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: key }) });
}
