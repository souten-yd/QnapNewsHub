import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useLogout } from "../api/auth";
import { useUpdateNotice } from "../api/update";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { useTheme, type Theme } from "../stores";

/** 画面の殻。携帯は左から出るドロワー（下部ナビは置かない）、PC は折りたためるサイドバー。 */
const SIDEBAR_KEY = "nh-sidebar";

function readCollapsed(): boolean {
  try { return localStorage.getItem(SIDEBAR_KEY) === "min"; } catch { return false; }
}

const icon = (path: ReactNode) => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>{path}</svg>
);
const IconMenu = () => icon(<><path d="M4 6h16" /><path d="M4 12h16" /><path d="M4 18h16" /></>);
const NAV: { to: string; label: string; end?: boolean; icon: ReactNode }[] = [
  { to: "/", label: "ニュース", end: true, icon: icon(<><path d="M4 5h13a2 2 0 0 1 2 2v12H6a2 2 0 0 1-2-2z" /><path d="M8 9h7" /><path d="M8 13h7" /></>) },
  { to: "/analytics", label: "分析", icon: icon(<><path d="M4 20V10" /><path d="M10 20V4" /><path d="M16 20v-7" /><path d="M22 20H2" /></>) },
  { to: "/entities", label: "物", icon: icon(<><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="8" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M8 7l8 1M7 8l4 8M17 10l-4 6" /></>) },
  { to: "/sources", label: "取得先の管理", icon: icon(<><path d="M4 11a9 9 0 0 1 9 9" /><path d="M4 4a16 16 0 0 1 16 16" /><circle cx="5" cy="19" r="1" /></>) },
  { to: "/diagnostics", label: "診断", icon: icon(<><path d="M3 12h4l3-8 4 16 3-8h4" /></>) },
  { to: "/settings", label: "設定", icon: icon(<><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" /></>) },
];
const THEMES: [Theme, string][] = [["system", "自動"], ["light", "ライト"], ["dark", "ダーク"]];

function NavPanel({ compact, onNavigate, username }: { compact: boolean; onNavigate?: () => void; username: string }) {
  const logout = useLogout();
  const theme = useTheme((s) => s.theme);
  const update = useUpdateNotice();
  const setTheme = useTheme((s) => s.setTheme);
  return (
    <div className="flex h-full flex-col">
      <nav aria-label="メニュー" className="flex-1 space-y-1 p-2">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end} onClick={onNavigate} title={n.label}
            className={({ isActive }) => `flex min-h-11 items-center gap-3 rounded-xl px-3 text-sm font-medium ${isActive ? "bg-accent-600/10 text-accent-700 dark:text-accent-300" : "text-zinc-700 hover:bg-zinc-200/60 dark:text-zinc-300 dark:hover:bg-zinc-800"}`}>
            {n.icon}<span className={compact ? "sr-only" : ""}>{n.label}</span>
            {n.to === "/settings" && update.data?.available && <span data-testid="update-mark" role="img" aria-label="新しい版があります" title={update.data.message ?? ""} className="ml-auto h-2.5 w-2.5 rounded-full bg-accent-600" />}
          </NavLink>
        ))}
      </nav>
      {!compact && (
        <div className="space-y-2 border-t border-zinc-200/80 p-3 dark:border-zinc-800/80">
          <div role="radiogroup" aria-label="テーマ" className="grid grid-cols-3 gap-1">
            {THEMES.map(([id, label]) => (
              <button key={id} type="button" role="radio" aria-checked={theme === id} onClick={() => setTheme(id)}
                className={`min-h-10 rounded-lg border text-xs ${theme === id ? "border-accent-600 bg-accent-600/10 font-semibold" : "border-zinc-300 dark:border-zinc-700"}`}>{label}</button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-xs text-zinc-500" data-testid="whoami">{username}</span>
            <button type="button" onClick={() => logout.mutate()} disabled={logout.isPending} className="min-h-10 rounded-lg border border-zinc-300 px-3 text-xs dark:border-zinc-700">ログアウト</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function AppLayout({ username }: { username: string }) {
  const updateNotice = useUpdateNotice();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawer, setDrawer] = useState(false);
  const location = useLocation();
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);

  const toggleSidebar = () => {
    const next = !collapsed;
    setCollapsed(next);
    try { localStorage.setItem(SIDEBAR_KEY, next ? "min" : "full"); } catch { /* 覚えられなくても動く */ }
  };
  useEffect(() => setDrawer(false), [location.pathname]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setDrawer(false); menuButton.current?.focus(); } };
    window.addEventListener("keydown", onKey);
    drawerRef.current?.querySelector<HTMLElement>("a,button")?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);
  useEffect(() => {
    const query = matchMedia("(min-width: 768px)");
    const onChange = () => query.matches && setDrawer(false);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const iconBtn = "grid h-11 w-11 shrink-0 place-items-center rounded-xl text-zinc-600 hover:bg-zinc-200/60 dark:text-zinc-300 dark:hover:bg-zinc-800";
  return (
    <div className="flex h-full overflow-hidden">
      {/* PC: 折りたためるサイドバー（☰ で開閉。状態は端末に覚える） */}
      <aside data-shell-sidebar data-collapsed={collapsed}
        className={`hidden shrink-0 flex-col border-r border-zinc-200/80 bg-zinc-100/60 transition-[width] duration-200 ease-out motion-reduce:transition-none dark:border-zinc-800/80 dark:bg-zinc-900/60 md:flex ${collapsed ? "w-[60px]" : "w-64"}`}>
        <div className="safe-top flex h-14 items-center gap-1 px-2">
          <button type="button" onClick={toggleSidebar} aria-label={collapsed ? "サイドバーを開く" : "サイドバーを閉じる"} aria-expanded={!collapsed} className={iconBtn}><IconMenu /></button>
          {!collapsed && <span className="truncate text-base font-bold tracking-tight" data-testid="brand">ニュースハブ</span>}
        </div>
        <div className="min-h-0 flex-1"><NavPanel compact={collapsed} username={username} /></div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* 携帯の上の行: ☰ とニュースハブの名前。PC ではサイドバーが担うので出さない */}
        <header data-shell-topbar className="safe-top sticky top-0 z-30 shrink-0 border-b border-zinc-200/80 bg-zinc-50 dark:border-zinc-800/80 dark:bg-zinc-950 md:hidden">
          <div className="flex h-14 items-center gap-1 px-3">
            <button ref={menuButton} type="button" onClick={() => setDrawer(true)} aria-label="メニューを開く" aria-haspopup="dialog" aria-expanded={drawer} className={`${iconBtn} relative -ml-1.5`}><IconMenu />{updateNotice.data?.available && <span data-testid="update-mark-menu" aria-hidden className="absolute right-2 top-2 h-2.5 w-2.5 rounded-full bg-accent-600 ring-2 ring-zinc-50 dark:ring-zinc-950" />}</button>
            <span className="pl-1 text-lg font-bold tracking-tight" data-testid="brand">ニュースハブ</span>
          </div>
        </header>
        <main data-shell-main className="safe-bottom min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain">
          <ErrorBoundary resetKey={location.pathname}><Outlet /></ErrorBoundary>
        </main>
      </div>

      {/* 携帯: 左から出るドロワー */}
      <div className={`fixed inset-0 z-50 md:hidden ${drawer ? "" : "pointer-events-none"}`} aria-hidden={!drawer}>
        <div onClick={() => setDrawer(false)} className={`absolute inset-0 bg-black/40 transition-opacity motion-reduce:transition-none ${drawer ? "opacity-100" : "opacity-0"}`} />
        <aside ref={drawerRef} role="dialog" aria-modal="true" aria-label="メニュー" data-testid="nav-drawer" {...(drawer ? {} : ({ inert: "" } as object))}
          className={`safe-top safe-bottom absolute inset-y-0 left-0 flex w-72 max-w-[85%] flex-col bg-zinc-50 shadow-xl transition-transform duration-200 motion-reduce:transition-none dark:bg-zinc-950 ${drawer ? "translate-x-0" : "-translate-x-full"}`}>
          <div className="flex h-14 items-center justify-between px-3">
            <span className="text-lg font-bold tracking-tight">ニュースハブ</span>
            <button type="button" onClick={() => { setDrawer(false); menuButton.current?.focus(); }} aria-label="メニューを閉じる" className={iconBtn}>×</button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto"><NavPanel compact={false} username={username} onNavigate={() => setDrawer(false)} /></div>
        </aside>
      </div>
    </div>
  );
}
