import { useState, type FormEvent } from "react";
import { useLogin, useSetup } from "../api/auth";
import { field, primary } from "../features/news/settingsUi";

function Shell({ title, hint, children }: { title: string; hint: string; children: React.ReactNode }) {
  return (
    <main className="safe-top safe-bottom mx-auto flex min-h-full max-w-sm flex-col justify-center p-6">
      <p className="text-center text-2xl font-bold tracking-tight" data-testid="brand">ニュースハブ</p>
      <h1 className="mt-6 text-lg font-semibold">{title}</h1>
      <p className="mt-1 text-xs leading-relaxed text-zinc-500">{hint}</p>
      {children}
    </main>
  );
}

export function LoginPage() {
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const submit = (e: FormEvent) => { e.preventDefault(); login.mutate({ username, password }); };
  return (
    <Shell title="ログイン" hint="ニュースハブの管理者のアカウントでログインします。">
      <form onSubmit={submit} className="mt-4 space-y-3">
        <label className="block text-xs text-zinc-500">利用者名
          <input aria-label="利用者名" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} className={`${field} mt-1 w-full`} /></label>
        <label className="block text-xs text-zinc-500">パスワード
          <input aria-label="パスワード" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} className={`${field} mt-1 w-full`} /></label>
        {login.isError && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{login.error instanceof Error ? login.error.message : "ログインできませんでした"}</p>}
        <button type="submit" disabled={login.isPending || !username || !password} className={`${primary} w-full`}>{login.isPending ? "確認中…" : "ログイン"}</button>
      </form>
    </Shell>
  );
}

/** 利用者が 1 人もいないときだけ出る。ここで作ったアカウントが管理者になる。 */
export function SetupPage() {
  const setup = useSetup();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const mismatch = again !== "" && again !== password;
  const submit = (e: FormEvent) => { e.preventDefault(); if (!mismatch) setup.mutate({ username, password }); };
  return (
    <Shell title="最初の管理者を作る" hint="はじめての起動です。この画面で作るアカウントが管理者になります（パスワードは 12 文字以上）。">
      <form onSubmit={submit} className="mt-4 space-y-3">
        <label className="block text-xs text-zinc-500">利用者名
          <input aria-label="利用者名" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} className={`${field} mt-1 w-full`} /></label>
        <label className="block text-xs text-zinc-500">パスワード（12 文字以上）
          <input aria-label="パスワード" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} className={`${field} mt-1 w-full`} /></label>
        <label className="block text-xs text-zinc-500">パスワード（もう一度）
          <input aria-label="パスワード（もう一度）" type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} className={`${field} mt-1 w-full`} /></label>
        {mismatch && <p role="alert" className="text-xs text-red-600 dark:text-red-400">パスワードが一致しません</p>}
        {setup.isError && <p role="alert" className="text-xs text-red-600 dark:text-red-400">{setup.error instanceof Error ? setup.error.message : "作れませんでした"}</p>}
        <button type="submit" disabled={setup.isPending || !username || password.length < 12 || mismatch || !again} className={`${primary} w-full`}>{setup.isPending ? "作っています…" : "管理者を作ってはじめる"}</button>
      </form>
    </Shell>
  );
}
