import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

/** ControlDeck との連携（backend/qnapnewshub/link_router.py）。管理者だけ。鍵の値は出てこない（名前と時刻だけ）。 */
export interface LinkPeer { id: number; name: string; fingerprint: string; created_at: string; last_seen_at: string; revoked: boolean }
export interface LinkSecret { name: string; received_at: string; peer_id: number; peer: string; present: boolean }
export interface LinkJobs { queued: number; leased: number; expired: number; dead: number; done_24h: number; queued_by_kind: Record<string, number> }
export interface LinkStatus {
  fingerprint: string; peers: LinkPeer[]; secrets: LinkSecret[]; pairing: { expires_at: string; wrong: number } | null; jobs: LinkJobs; allowed_secrets: string[];
}
export interface PairingStart { code: string; expires_at: string; expires_in: number; fingerprint: string; public_key: string }

const KEY = ["link"] as const;

/** つなぐ番号を出している間・相手が来るのを待つ間は 3 秒ごとに読む（つながったら一覧に出る）。 */
export const useLink = (poll: boolean) => useQuery({ queryKey: KEY, queryFn: () => api<LinkStatus>("/link"), refetchInterval: poll ? 3000 : false });

export function useStartPairing() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api<PairingStart>("/link/pairing", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: KEY }) });
}
export function useCancelPairing() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api<{ ok: boolean }>("/link/pairing", { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: KEY }) });
}
export function useRevokePeer() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: (id: number) => api<{ ok: boolean }>(`/link/peers/${id}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: KEY }) });
}
