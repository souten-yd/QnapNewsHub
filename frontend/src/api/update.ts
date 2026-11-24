import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

/** 更新（backend/qnapnewshub/updater.py）。確認・設定・更新・戻すは管理者だけ。 */
export interface UpdateRelease { version: string; tag: string; prerelease: boolean; notes: string; url: string; published_at: string; size: number }
export interface UpdateJob { phase: "idle" | "downloading" | "verifying" | "flushing" | "installing" | "error"; kind: "" | "update" | "rollback"; target: string; message: string; percent: number; started_at: string }
export interface UpdateLast { kind: "update" | "rollback"; from: string; to: string; rc: number; at: string; ok: boolean; message: string; log: string }
export interface UpdateStatus {
  version: string; install: "qpkg" | "qpkg-sim" | "linux"; can_install: boolean; include_prerelease: boolean; checked_at: string; checking: boolean; error: string;
  latest: UpdateRelease | null; update_available: boolean; rollback: string | null; job: UpdateJob; last: UpdateLast | null;
  rest: { active: boolean; ends_at: string | null }; etag_hits: number; checks: number; manual_steps: string[];
}
export interface UpdateNotice { available: boolean; version: string; message: string | null }

const KEY = ["update"] as const;
export const BUSY = ["downloading", "verifying", "flushing", "installing"];

/** 更新の途中・再起動を待っている間（waiting）は 1.5 秒ごとに読む。サーバーが入れ替わる間の失敗は静かに繰り返す。 */
export const useUpdate = (waiting: boolean) => useQuery({
  queryKey: [...KEY, "status"], queryFn: () => api<UpdateStatus>("/update"),
  refetchInterval: (query) => (waiting || BUSY.includes(query.state.data?.job.phase ?? "") ? 1500 : false), retry: waiting ? true : 1, retryDelay: 1500,
});
export const useUpdateNotice = () => useQuery({ queryKey: [...KEY, "notice"], queryFn: () => api<UpdateNotice>("/update/notice"), staleTime: 5 * 60_000, retry: false });

function useStatusMutation<V>(fn: (v: V) => Promise<UpdateStatus>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: (s) => { qc.setQueryData([...KEY, "status"], s); void qc.invalidateQueries({ queryKey: [...KEY, "notice"] }); } });
}
export const useCheckUpdate = () => useStatusMutation(() => api<UpdateStatus>("/update/check", { method: "POST" }));
export const useSaveUpdateSettings = () => useStatusMutation((include_prerelease: boolean) => api<UpdateStatus>("/update/settings", { method: "PUT", json: { include_prerelease } }));
export const useInstallUpdate = () => useStatusMutation((version: string) => api<UpdateStatus>("/update/install", { method: "POST", json: { version } }));
export const useRollbackUpdate = () => useStatusMutation((version: string) => api<UpdateStatus>("/update/rollback", { method: "POST", json: { version } }));
