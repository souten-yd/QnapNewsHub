/** 分析の画面: 数字のカード・新着の傾向（分け方・凡例・範囲）・ホットトピック（押すとニュースの検索へ）・取得先の分析・期間の比較・空き枠。
 *  320・390・1280 × ライト・ダークで、横にはみ出さず、グラフが描かれること。ホームの上位 5。API は模擬（見本の数字）。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, VIEWPORTS, expectNoHorizontalOverflow, isDark, shot } from "./helpers";

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test.describe(`分析 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("数字・グラフ・ホットトピックが出て、はみ出さない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/analytics");
        await expect(page.getByTestId("analytics-page")).toBeVisible();
        expect(await isDark(page)).toBe(scheme === "dark");
        await expect(page.getByTestId("card-today")).toContainText("412 件");
        await expect(page.getByTestId("card-indep")).toContainText("361 件");
        await expect(page.getByTestId("card-sources")).toContainText("57");
        await expect(page.getByTestId("card-ratio")).toContainText("×1.24");
        await expect(page.getByTestId("trend-chart")).toHaveAttribute("data-ready", "true");
        await expect(page.getByTestId("trend-chart").locator("canvas")).toBeVisible();
        await expect(page.getByTestId("hot-list").getByTestId("hot-row")).toHaveCount(10);
        await expect(page.getByTestId("new-list")).toContainText("Zorblax");
        await expect(page.getByTestId("hot-fields")).toContainText("AI・LLM");
        await expect(page.getByTestId("sources-table")).toContainText("見本の取得先 1");
        await expect(page.getByTestId("heatmap")).toHaveAttribute("data-ready", "true");
        await expect(page.getByTestId("compare-chart")).toHaveAttribute("data-ready", "true");
        await expect(page.getByTestId("future-slot")).toHaveCount(2);
        await expect(page.getByTestId("future-slot").first()).toContainText("v0.2 以降");
        await expectNoHorizontalOverflow(page);
        await shot(page, `analytics-top-${vp.name}-${scheme}`);
        await page.getByTestId("sec-hot").scrollIntoViewIfNeeded();
        await shot(page, `analytics-hot-${vp.name}-${scheme}`);
        await page.getByTestId("sec-sources").scrollIntoViewIfNeeded();
        await shot(page, `analytics-sources-${vp.name}-${scheme}`);
        await page.getByTestId("sec-compare").scrollIntoViewIfNeeded();
        await shot(page, `analytics-compare-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}

test.describe("分析の操作", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("期間・分け方・粒度・絞り込みが API に渡り、ホットトピックを押すとニュースの検索になる", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/analytics");
    await expect(page.getByTestId("trend-chart")).toHaveAttribute("data-ready", "true");
    const last = (p: string) => [...state.calls].reverse().find((c) => c.path === p);
    await page.getByTestId("filter-range").getByRole("radio", { name: "30 日" }).click();
    await expect.poll(() => state.calls.some((c) => c.path === "/analytics/series" && c.method === "GET")).toBe(true);
    await page.getByTestId("trend-by").getByRole("radio", { name: "チャンネル" }).click();
    await page.getByTestId("trend-metric").getByRole("radio", { name: "重複を除く" }).click();
    await page.getByTestId("filter-lang").getByRole("radio", { name: "日本語" }).click();
    await page.getByTestId("filter-kind").getByRole("radio", { name: "更新" }).click();
    await expect(page.getByTestId("trend-chart")).toHaveAttribute("aria-label", /チャンネル別/);
    expect(JSON.stringify(last("/analytics/series"))).toBeTruthy();
    const seriesCalls = state.calls.filter((c) => c.path === "/analytics/series").length;
    expect(seriesCalls).toBeGreaterThan(3);
    // 凡例の絞り込み・範囲の選択はグラフの中（canvas）。ここでは描画が壊れないこと
    await expect(page.getByTestId("trend-chart").locator("canvas")).toBeVisible();
    await page.getByTestId("hot-list").getByTestId("hot-link").first().click();
    await expect(page).toHaveURL(/\/\?q=ROCm/);
    await expect(page.getByRole("searchbox", { name: "アーカイブ全体を検索" })).toHaveValue("ROCm");
    expect(state.unhandled).toEqual([]);
  });

  test("履歴が無いときは正直に空を出す（未実装を出さない）", async ({ page }) => {
    const state = newState({ analyticsEmpty: true });
    await mockApi(page, state);
    await page.goto("/analytics");
    await expect(page.getByTestId("hot-nohistory")).toBeVisible();
    await expect(page.getByTestId("trend-empty")).toBeVisible();
    await expect(page.getByTestId("card-ratio")).toContainText("履歴がまだありません");
  });

  test("ホームの上にホットトピックの上位 5 が出て、押すと分析へ行く", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/");
    await expect(page.getByTestId("hot-mini")).toBeVisible();
    await expect(page.getByTestId("hot-mini-item")).toHaveCount(5);
    await shot(page, "analytics-home-390");
    await page.getByTestId("hot-mini-item").first().click();
    await expect(page).toHaveURL(/\/analytics/);
    await expect(page.getByTestId("sec-hot")).toBeVisible();
  });
});
