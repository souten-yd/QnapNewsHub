/** 認証と待ち受けの設定 API（backend/qnapnewshub/auth_router.py）。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

export interface AuthStatus { setup_needed: boolean; authenticated: boolean; username: string | null }
export interface HubSettings { host: string; port: number; cookie_secure: boolean }

export const AUTH_KEY = ["auth", "status"] as const;

export const useAuthStatus = () =>
  useQuery({ queryKey: AUTH_KEY, queryFn: () => api<AuthStatus>("/auth/status"), staleTime: 30_000, retry: false });

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: { username: string; password: string }) => api<{ username: string }>("/auth/login", { method: "POST", json: b }),
    onSuccess: () => qc.invalidateQueries({ queryKey: AUTH_KEY }),
  });
}
export function useSetup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: { username: string; password: string }) => api<{ username: string }>("/auth/setup", { method: "POST", json: b }),
    onSuccess: () => qc.invalidateQueries({ queryKey: AUTH_KEY }),
  });
}
export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<{ ok: boolean }>("/auth/logout", { method: "POST" }),
    // ログアウトしたら取ってあった記事・設定を捨てる（次の人に見せない）
    onSettled: () => { qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" }); qc.invalidateQueries({ queryKey: AUTH_KEY }); },
  });
}

export const useHubSettings = () =>
  useQuery({ queryKey: ["auth", "hub"], queryFn: () => api<HubSettings>("/auth/hub"), staleTime: 15_000 });
export function useSaveHubSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: HubSettings) => api<HubSettings & { restart_needed: boolean }>("/auth/hub", { method: "PUT", json: b }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth", "hub"] }),
  });
}
