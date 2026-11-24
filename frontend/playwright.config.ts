import { defineConfig } from "@playwright/test";

// ./hub.sh e2e は一時の置き場でバックエンドを起こして NEWSHUB_E2E_URL を渡す。
// ブラウザは PLAYWRIGHT_BROWSERS_PATH のものを使う（playwright install はしない）。
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 7_000 },
  // 失敗した試験の trace・画面写真は test-results/、一覧は playwright-report/ に残す
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.NEWSHUB_E2E_URL ?? "http://127.0.0.1:8787",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    browserName: "chromium",
    locale: "ja-JP",
    timezoneId: "Asia/Tokyo",
  },
});
