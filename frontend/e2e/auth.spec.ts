/** ログイン・最初の管理者を作る画面（利用者がいなければ setup へ）・ログアウト。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, VIEWPORTS, expectNoHorizontalOverflow, shot } from "./helpers";

const PASSWORD = "correct-horse-battery";

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test.describe(`認証 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("ログインの画面が出て、はみ出さない", async ({ page }) => {
        await mockApi(page, newState({ authenticated: false }));
        await page.goto("/");
        await expect(page.getByRole("heading", { name: "ログイン" })).toBeVisible();
        await expect(page.getByTestId("brand")).toHaveText("ニュースハブ");
        await expectNoHorizontalOverflow(page);
        await shot(page, `login-${vp.name}-${scheme}`);
      });

      test("利用者がいなければ最初の管理者を作る画面が出る", async ({ page }) => {
        await mockApi(page, newState({ authenticated: false, setupNeeded: true }));
        await page.goto("/");
        await expect(page.getByRole("heading", { name: "最初の管理者を作る" })).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await shot(page, `setup-${vp.name}-${scheme}`);
      });
    });
  }
}

test("ログインできる。まちがえると理由が出る", async ({ page }) => {
  const state = newState({ authenticated: false });
  await mockApi(page, state);
  await page.goto("/news-not-a-page");
  await page.getByLabel("利用者名").fill("admin");
  await page.getByLabel("パスワード", { exact: true }).fill("wrong-password");
  await page.getByRole("button", { name: "ログイン" }).click();
  await expect(page.getByRole("alert")).toContainText("違います");
  await page.getByLabel("パスワード", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "ログイン" }).click();
  await expect(page.getByTestId("news-root")).toBeVisible();      // 知らない道からでもニュースへ
  await expect(page).toHaveURL(/\/$/);
});

test("最初の管理者は、パスワードが 12 文字未満・不一致のあいだは作れない", async ({ page }) => {
  const state = newState({ authenticated: false, setupNeeded: true });
  await mockApi(page, state);
  await page.goto("/");
  const submit = page.getByRole("button", { name: "管理者を作ってはじめる" });
  await page.getByLabel("利用者名").fill("admin");
  await page.getByLabel("パスワード", { exact: true }).fill("short");
  await page.getByLabel("パスワード（もう一度）").fill("short");
  await expect(submit).toBeDisabled();
  await page.getByLabel("パスワード", { exact: true }).fill(PASSWORD);
  await expect(page.getByRole("alert")).toContainText("一致しません");
  await expect(submit).toBeDisabled();
  await page.getByLabel("パスワード（もう一度）").fill(PASSWORD);
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByTestId("news-root")).toBeVisible();
  expect(state.calls.find((c) => c.path === "/auth/setup")?.body).toEqual({ username: "admin", password: PASSWORD });
});

test("QPKG（LAN に待ち受け）では、最初の管理者に一回限りの番号が要る", async ({ page }) => {
  // 目的: LAN の誰かが先に管理者を作るのを防ぐ番号の欄が出て、番号が無いと作れず、送るときは大文字で送る。
  // 根拠: QPKG は 0.0.0.0 で待ち受ける（2026-11-24 の設計。番号は QTS のシステムのログに出る）。
  const state = newState({ authenticated: false, setupNeeded: true, setupTokenRequired: true });
  await mockApi(page, state);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const submit = page.getByRole("button", { name: "管理者を作ってはじめる" });
  await expect(page.getByTestId("setup-token")).toBeVisible();
  await page.getByLabel("利用者名").fill("admin");
  await page.getByLabel("パスワード", { exact: true }).fill(PASSWORD);
  await page.getByLabel("パスワード（もう一度）").fill(PASSWORD);
  await expect(submit).toBeDisabled();
  await page.getByTestId("setup-token").fill("abcd-efgh-jkmn");
  await expect(submit).toBeEnabled();
  await shot(page, "setup-token-390");
  await submit.click();
  await expect(page.getByTestId("news-root")).toBeVisible();
  expect(state.calls.find((c) => c.path === "/auth/setup")?.body).toEqual({ username: "admin", password: PASSWORD, setup_token: "ABCD-EFGH-JKMN" });
});

test("ログアウトするとログインの画面へ戻る", async ({ page }) => {
  await mockApi(page, newState());
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/");
  await expect(page.locator("[data-testid=whoami]:visible")).toHaveText("admin");
  await page.getByRole("button", { name: "ログアウト" }).click();
  await expect(page.getByRole("heading", { name: "ログイン" })).toBeVisible();
});

test("セッションが切れたら（401）ログインの画面へ", async ({ page }) => {
  const state = newState();
  await mockApi(page, state);
  await page.goto("/");
  await expect(page.getByTestId("news-root")).toBeVisible();
  state.authenticated = false;
  await page.getByRole("link", { name: "設定" }).first().click();      // 設定の読み込みが 401 になる
  await expect(page.getByRole("heading", { name: "ログイン" })).toBeVisible();
});
