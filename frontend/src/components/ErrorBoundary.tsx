import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props { children: ReactNode; resetKey?: string }
interface State { error: Error | null }

/** 1 画面の例外でアプリ全体を白くしない。画面を移ったら再び試す。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };
  static getDerivedStateFromError(error: Error): State { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo): void { console.error("[error-boundary]", error, info.componentStack); }
  componentDidUpdate(previous: Props): void {
    if (this.state.error && previous.resetKey !== this.props.resetKey) this.setState({ error: null });
  }
  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="mx-auto max-w-3xl p-4 md:p-6" role="alert">
        <div className="rounded-2xl border border-red-200 bg-red-50 p-5 dark:border-red-900 dark:bg-red-950/40">
          <h2 className="text-base font-semibold text-red-800 dark:text-red-300">この画面を表示できませんでした</h2>
          <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-white/70 p-3 text-[11px] leading-4 text-red-900 dark:bg-black/30 dark:text-red-300">{error.name}: {error.message}</pre>
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" onClick={() => this.setState({ error: null })} className="min-h-11 rounded-xl bg-white px-4 text-sm font-semibold text-red-700 hover:bg-red-100 dark:bg-zinc-900 dark:text-red-300">もう一度開く</button>
            <button type="button" onClick={() => location.reload()} className="min-h-11 rounded-xl px-4 text-sm text-red-700 hover:bg-red-100 dark:text-red-300 dark:hover:bg-red-950">読み直す</button>
          </div>
        </div>
      </div>
    );
  }
}
