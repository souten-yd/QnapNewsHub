import { createPortal } from "react-dom";
import { useToasts } from "../stores";

// ---- トースト ----
export function Toasts() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);
  if (toasts.length === 0) return null;
  const colors = {
    success: "border-emerald-300 dark:border-emerald-800",
    error: "border-red-300 dark:border-red-800",
    info: "border-zinc-300 dark:border-zinc-700",
  };
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-[60] flex flex-col items-center gap-2 px-4" data-testid="toasts">
      {toasts.map((t) => t.action ? (
        <div key={t.id} role="status"
          className={`pointer-events-auto flex max-w-full items-center gap-1 rounded-xl border bg-white py-1 pl-4 pr-1 text-sm shadow-lg dark:bg-zinc-800 ${colors[t.kind]}`}>
          <span className="min-w-0 flex-1">{t.message}</span>
          <button type="button" onClick={() => { t.action!.onClick(); dismiss(t.id); }}
            className="min-h-10 shrink-0 rounded-lg px-3 font-semibold text-accent-700 hover:bg-accent-50 dark:text-accent-400 dark:hover:bg-accent-600/15">
            {t.action.label}
          </button>
        </div>
      ) : (
        <button key={t.id} role="status" onClick={() => dismiss(t.id)}
          className={`pointer-events-auto max-w-full rounded-xl border bg-white px-4 py-2.5 text-sm shadow-lg dark:bg-zinc-800 ${colors[t.kind]}`}>
          {t.message}
        </button>
      ))}
    </div>,
    document.body,
  );
}

// ---- スパークライン（軽量インライン SVG）----
export function Sparkline({ values, max = 100, className = "", fill = false, heightClass = "h-7" }: {
  values: (number | null)[]; max?: number; className?: string; fill?: boolean; heightClass?: string;
}) {
  const w = 100;
  const h = 28;
  const pts = values
    .map((v, i) => {
      if (v == null) return null;
      const x = (i / Math.max(1, values.length - 1)) * w;
      const y = h - (Math.min(v, max) / max) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .filter(Boolean)
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className={`block w-full ${heightClass} ${className}`} aria-hidden>
      {pts && fill && <polygon points={`0,${h} ${pts} ${w},${h}`} fill="currentColor" opacity="0.1" />}
      {pts && <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />}
    </svg>
  );
}

// ---- スケルトン ----
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-zinc-200 dark:bg-zinc-800 ${className}`} />;
}
