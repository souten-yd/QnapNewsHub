/** 軽い翻訳の API（backend/qnapnewshub/translate_router.py）。モデルの取得・消去・状態は管理者だけ。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { TranslateDownload } from "./diag";

export interface TranslateStatus {
  engine: string; untranslated_mark: string; default_glossary: Record<string, string>; llm_state: string;
  model: { id: string; title: string; repo: string; rev: string; license: string; license_note: string; size: number; installed: boolean; loaded: boolean };
  download: TranslateDownload;
  stats: { units: number; cache_hit_pct: number | null; fail_pct: number | null; chars_per_sec: number; load_ms: number; load_error: string };
  counts: { titles: number; bodies: number; today: number; fail_pct: number | null; cache: number };
}
export interface ArticleTranslation { translated: boolean; title_ja?: string; summary_ja?: string; body_ja?: string; engine?: string; hq?: boolean; partial?: boolean; untranslated_mark?: string }

const key = ["translate"] as const;
export const useTranslateStatus = (poll: boolean) =>
  useQuery({ queryKey: [...key, "status"], queryFn: () => api<TranslateStatus>("/translate/status"), refetchInterval: poll ? 2_000 : 30_000 });
export function useFetchModel() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api("/translate/model", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: key }) });
}
export function useDeleteModel() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api("/translate/model", { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: key }) });
}
export const useTryTranslate = () => useMutation({ mutationFn: (text: string) => api<{ ja: string; units: number; failed: number }>("/translate/try", { method: "POST", json: { text } }) });
export const useArticleTranslation = (id: number, enabled: boolean) =>
  useQuery({ queryKey: [...key, "article", id], queryFn: () => api<ArticleTranslation>(`/translate/articles/${id}`), enabled });
export function useRequestTranslation(id: number) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => api<ArticleTranslation>(`/translate/articles/${id}`, { method: "POST" }), onSuccess: (r) => qc.setQueryData([...key, "article", id], r) });
}
