import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { AUTH_KEY, useAuthStatus } from "./api/auth";
import { setUnauthorizedHandler } from "./api/client";
import { Toasts } from "./components/ui";
import { AppLayout } from "./layouts/AppLayout";
import { LoginPage, SetupPage } from "./pages/Auth";
import NewsPage from "./pages/News";
import AnalyticsPage from "./pages/Analytics";
import EntitiesPage, { EntityPage } from "./pages/Entities";
import NewsSourcesPage from "./pages/NewsSources";
import SettingsPage from "./pages/Settings";
import DiagnosticsPage from "./pages/Diagnostics";

/** 状態（管理者がいるか・ログイン中か）で、最初の管理者を作る画面・ログイン・本体を切り替える。 */
export default function App() {
  const qc = useQueryClient();
  const status = useAuthStatus();
  useEffect(() => {
    setUnauthorizedHandler(() => { void qc.invalidateQueries({ queryKey: AUTH_KEY }); });
  }, [qc]);
  useEffect(() => {
    const shell = status.data?.authenticated === true;
    document.documentElement.classList.toggle("nh-shell", shell);
    return () => document.documentElement.classList.remove("nh-shell");
  }, [status.data?.authenticated]);

  let body;
  if (status.isLoading) body = <div className="grid h-full place-items-center text-sm text-zinc-500" role="status">読み込み中…</div>;
  else if (status.isError || !status.data) body = (
    <div className="mx-auto max-w-md p-6" role="alert">
      <p className="text-sm">ニュースハブにつながりません。</p>
      <button type="button" onClick={() => status.refetch()} className="mt-3 min-h-11 rounded-xl border border-zinc-300 px-4 text-sm dark:border-zinc-700">もう一度試す</button>
    </div>
  );
  else if (status.data.setup_needed) body = <SetupPage />;
  else if (!status.data.authenticated) body = <LoginPage />;
  else body = (
    <Routes>
      <Route element={<AppLayout username={status.data.username ?? ""} />}>
        <Route path="/" element={<NewsPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/entities" element={<EntitiesPage />} />
        <Route path="/entities/:id" element={<EntityPage />} />
        <Route path="/sources" element={<NewsSourcesPage />} />
        <Route path="/diagnostics" element={<DiagnosticsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
  return <>{body}<Toasts /></>;
}
