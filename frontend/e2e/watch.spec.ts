/** ウォッチ（/watch）。目的: 既定は空で案内が出る・名前で探して足せる・足すと「前回から」の変化（変化点・主張の条件・数値の差・関係・版・イベント）が出る・外せる・
 *  「見た」を押すと変化なしになる。320 と 1280・ライト／ダークではみ出さない。API は模擬（見本の数字）。
 *  根拠: ARCHITECTURE 12 章（ウォッチリストと変化）、利用者 2026-11-26 の指定（既定は空・画面で足す）、AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`ウォッチ ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("既定は空 → 足す → 変化が出る → 見た で変化なし → 外す", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/watch");
        await expect(page.getByTestId("watch-empty")).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await page.getByTestId("watch-search").fill("llama");
        await page.getByTestId("watch-add-btn").first().click();
        await expect(page.getByTestId("watch-row")).toHaveCount(1);
        const ch = page.getByTestId("watch-change").first();
        await expect(ch.getByTestId("watch-points")).toContainText("言及が急に増えた");
        await expect(ch.getByTestId("watch-claims")).toContainText("GPU・端末 R9700");
        await expect(ch.getByTestId("watch-claims")).toContainText("75");
        await expect(ch.getByTestId("watch-metrics")).toContainText("前 100・+60%");
        await expect(ch.getByTestId("watch-relations")).toContainText("supports");
        await expect(ch.getByTestId("watch-versions")).toContainText("b9000");
        await expect(ch.getByTestId("watch-events")).toContainText("Qwen 3.5 が公開");
        await expect(page.getByTestId("watch-changed")).toHaveText("1");
        await expectNoHorizontalOverflow(page);
        await shot(page, `watch-${vp.name}-${scheme}`);
        await page.getByTestId("watch-seen").click();
        await expect(page.getByTestId("watch-quiet")).toBeVisible();
        await expect(page.getByTestId("watch-changed")).toHaveText("0");
        await page.getByRole("button", { name: /を外す/ }).click();
        await expect(page.getByTestId("watch-empty")).toBeVisible();
        expect(state.calls.some((c) => c.method === "POST" && c.path === "/watch")).toBe(true);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}
