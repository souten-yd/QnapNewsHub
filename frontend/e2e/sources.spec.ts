/** 取得先の管理（/sources）。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, VIEWPORTS, expectNoHorizontalOverflow, shot } from "./helpers";

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test.describe(`取得先 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("一覧・失敗中・止めている取得先が出て、はみ出さない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        await expect(page.getByText("見本の取得先 1").first()).toBeVisible();
        await expect(page.getByText("失敗中の取得先").first()).toBeVisible();
        await expect(page.getByText("止めている取得先").first()).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await shot(page, `sources-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}

test.describe("取得先の操作", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("詳細を開くと履歴と記事（元のページへのリンク）が出る。ニュースへ戻れる", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/sources");
    await page.getByText("見本の取得先 1").first().click();
    const link = page.getByTestId("news-detail-articles").getByRole("link").first();
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("href", /^https:\/\/news\.example\.test\//);
    await shot(page, "sources-detail-390");
    await page.getByRole("link", { name: /ニュースへ戻る/ }).click();
    await expect(page.getByTestId("news-root")).toBeVisible();
  });

  test("目録（プリセット）の画面が開く", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/sources");
    await page.getByTestId("news-tab-add").click();
    await expect(page.getByText("見本の目録").first()).toBeVisible();
    expect(state.unhandled).toEqual([]);
  });
});
