/** 本物のバックエンド（./hub.sh e2e が起こす一時の置き場）に対して、API を模擬せずに流す 1 本。
 *  最初の管理者を作る → ニュース（見本の記事は seed_backend.py が入れた）→ 設定の鍵 → 取得先の管理 → ログアウト → ログイン。
 *  外への通信はしない（記事の画像と元のページは page.route で受ける）。 */
import { expect, test } from "@playwright/test";
import { expectNoHorizontalOverflow, shot } from "./helpers";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");
const PASSWORD = ["real", "e2e", "password", "2026"].join("-");
const FAKE_KEY = ["sk", "or", "v1", "real", "0123456789abcdef"].join("-");

test.describe.configure({ mode: "serial" });

for (const [width, height, scheme] of [[390, 844, "light"], [1280, 800, "dark"]] as const) {
  test(`本物のバックエンドで通し ${width}px ${scheme}`, async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, locale: "ja-JP", hasTouch: width < 768 });
    await context.route("https://img.example.test/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: PNG }));
    const page = await context.newPage();
    const user = "e2e390";      // 最初の 1 本が作った管理者
    const firstRun = width === 390;      // 最初の 1 本だけが「利用者がいない」状態から始まる

    await page.goto("/");
    if (firstRun) {
      await expect(page.getByRole("heading", { name: "最初の管理者を作る" })).toBeVisible();
      await shot(page, `real-setup-${width}-${scheme}`);
      await page.getByLabel("利用者名").fill(user);
      await page.getByLabel("パスワード", { exact: true }).fill(PASSWORD);
      await page.getByLabel("パスワード（もう一度）").fill(PASSWORD);
      await page.getByRole("button", { name: "管理者を作ってはじめる" }).click();
    } else {
      // 2 本目: 管理者はもういる。setup には戻らずログインの画面になる
      await expect(page.getByRole("heading", { name: "ログイン" })).toBeVisible();
      await page.getByLabel("利用者名").fill(user);
      await page.getByLabel("パスワード", { exact: true }).fill(PASSWORD);
      await page.getByRole("button", { name: "ログイン" }).click();
    }

    // ニュース: 見本の記事（本物の /news/home）
    await expect(page.getByTestId("news-root")).toBeVisible();
    await expect(page.getByTestId("onair-title")).toBeVisible();
    await expect(page.getByTestId("onair-title").getByRole("link")).toHaveAttribute("target", "_blank");
    await expectNoHorizontalOverflow(page);
    await shot(page, `real-news-${width}-${scheme}`);
    await page.getByRole("tab", { name: "新着" }).click();
    const noImg = page.locator("[data-news-card]", { hasText: "画像のない記事" });
    await expect(noImg).toHaveCount(1);
    await expect(noImg.locator("img")).toHaveCount(0);
    await expect(page.locator("[data-news-card]", { hasText: "新しい量子化" }).locator("img")).toHaveCount(1);
    // 本物の操作の保存（隠す → 一覧から消える → 元に戻す）
    const card = page.locator("[data-news-card]", { hasText: "NAS で動かす" });
    await card.getByRole("button", { name: "隠す" }).click();
    await expect(card).toHaveCount(0);
    await page.getByRole("button", { name: "元に戻す" }).click();
    await expect(page.locator("[data-news-card]", { hasText: "NAS で動かす" })).toHaveCount(1);

    // 設定: 鍵（本物の暗号化の保存）
    await page.goto("/settings");
    await expect(page.getByTestId("llm-stats")).toBeVisible();
    if (firstRun) {
      await expect(page.getByTestId("key-status")).toContainText("未設定");
      await page.locator('input[name="openrouter-key"]').fill(FAKE_KEY);
      await page.getByRole("button", { name: "鍵を保存" }).click();
      await expect(page.getByTestId("key-status")).toContainText("鍵は入っています");
      await expect(page.locator('input[name="openrouter-key"]')).toHaveValue("");
      await page.reload();
    }
    await expect(page.getByTestId("key-status")).toContainText("鍵は入っています");
    expect(await page.evaluate(() => document.documentElement.outerHTML)).not.toContain(FAKE_KEY);
    // 本物の API が鍵の本文を返さない
    const keyResponse = await page.evaluate(async () => (await fetch("/api/v1/news/openrouter-key", { credentials: "same-origin" })).text());
    expect(keyResponse).not.toContain(FAKE_KEY);
    expect(JSON.parse(keyResponse)).toEqual({ set: true });
    await expectNoHorizontalOverflow(page);
    await shot(page, `real-settings-${width}-${scheme}`);

    // 取得先の管理（本物の目録・取得先の一覧）
    await page.goto("/sources");
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
    await expect(page.getByText("読み込めませんでした")).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await shot(page, `real-sources-${width}-${scheme}`);

    // ログアウト → ログインの画面。まちがったパスワードは断られる
    if (width < 768) await page.getByRole("button", { name: "メニューを開く" }).click();
    await page.getByRole("button", { name: "ログアウト" }).click();
    await expect(page.getByRole("heading", { name: "ログイン" })).toBeVisible();
    await page.getByLabel("利用者名").fill(user);
    await page.getByLabel("パスワード", { exact: true }).fill("wrong-wrong-wrong");
    await page.getByRole("button", { name: "ログイン" }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await page.getByLabel("パスワード", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "ログイン" }).click();
    await expect(page.getByRole("heading", { name: "取得先の管理", level: 1 })).toBeVisible();      // ログインのあとは開いていた画面へ戻る
    await context.close();
  });
}
