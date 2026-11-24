/** API クライアント。バックエンドの /api/v1 を cookie の session で呼ぶ。401 は画面側（認証の殻）へ知らせる。 */

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

/** FastAPI のエラー本文（detail）を、利用者に見せる 1 行の文にする。内部の情報は含まれない。 */
function detailText(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const issues = detail.flatMap((issue: unknown) => {
      const item = issue as { loc?: unknown; msg?: unknown };
      if (!item || typeof item.msg !== "string") return [];
      const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").map(String).join(".") : "";
      return [loc ? `${loc}: ${item.msg}` : item.msg];
    });
    if (issues.length > 0) return issues.join(" / ");
  }
  return fallback;
}

export async function api<T = unknown>(path: string, options: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...init } = options;
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  const res = await fetch(`/api/v1${path}`, { credentials: "same-origin", ...init, headers });
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* JSON でない応答 */
    }
    const message = detailText(body, res.status === 401 ? "ログインしてください" : `エラー (${res.status})`);
    // ログイン・最初の管理者・状態の確認の 401 は画面が自分で扱う。それ以外はセッション切れとして知らせる
    if (res.status === 401 && !path.startsWith("/auth/")) onUnauthorized?.();
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
