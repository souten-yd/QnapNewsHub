import { create } from "zustand";

// ---- テーマ（ライト・ダーク。端末ごとに覚える） ----
export type Theme = "system" | "dark" | "light";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* 覚えられなくても動く */
  }
}

function applyTheme(theme: Theme) {
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#09090b" : "#fafafa");
}

interface ThemeState {
  theme: Theme;
  setTheme: (t: Theme) => void;
}

export const useTheme = create<ThemeState>((set) => ({
  theme: (read("nh-theme") as Theme) || "system",
  setTheme: (theme) => {
    write("nh-theme", theme);
    set({ theme });
    applyTheme(theme);
  },
}));

applyTheme(useTheme.getState().theme);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(useTheme.getState().theme));

// ---- トースト ----
export interface Toast {
  id: number;
  message: string;
  kind: "success" | "error" | "info";
  /** 「元に戻す」など。押すと実行して通知を閉じる */
  action?: { label: string; onClick: () => void };
}

interface ToastState {
  toasts: Toast[];
  show: (message: string, kind?: Toast["kind"], action?: Toast["action"], ms?: number) => void;
  dismiss: (id: number) => void;
}

let toastId = 0;
export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  show: (message, kind = "success", action, ms) => {
    const id = ++toastId;
    set((st) => ({ toasts: [...st.toasts.slice(-2), { id, message, kind, action }] }));
    setTimeout(() => set((st) => ({ toasts: st.toasts.filter((t) => t.id !== id) })), ms ?? (action ? 6000 : 4000));
  },
  dismiss: (id) => set((st) => ({ toasts: st.toasts.filter((t) => t.id !== id) })),
}));
