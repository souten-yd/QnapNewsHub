export function Toggle({ label, hint, value, disabled = false, onChange }: { label: React.ReactNode; hint?: string; value: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return <label className="flex min-h-11 items-center justify-between gap-3 rounded-xl border border-zinc-200 px-3 py-2 dark:border-zinc-700">
    <span className="text-xs">{label}{hint && <span className="block text-[11px] font-normal text-zinc-500">{hint}</span>}</span>
    <input type="checkbox" checked={value} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="h-5 w-5 shrink-0 disabled:opacity-40" />
  </label>;
}
