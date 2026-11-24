/** イベント（同じ出来事の記事のまとまり）・ニュースの一覧の「ほか N 件」・分析の「新着の傾向」「独立したイベント」「物のホットトピック」。
 *  E2E の目的: イベントの一覧・ページ・ニュースのまとめ・新着の傾向の節（数字・今日の傾向の解説とリンク・新しく出た物）が 320 と 1280・ライト／ダークではみ出さず、AI の印が出ること。API は模擬（見本の数字）。
 *  根拠: ARCHITECTURE 4 章（イベント）・7.5 章（新着の傾向）、利用者 2026-11-24 の指定、AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`イベント ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("イベントの一覧: 記事の数・出どころ・一次情報の印・初報からの時間・物の札・AI の要約。一次情報で絞れる", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/events");
        await expect(page.getByTestId("event-row")).toHaveCount(3);
        const first = page.getByTestId("event-row").first();
        await expect(first.getByTestId("event-count")).toContainText("記事 6 件");
        await expect(first).toContainText("出どころ 5");
        await expect(first.getByTestId("primary-mark")).toBeVisible();
        await expect(first).toContainText("初報 4 時間前");
        await expect(first.getByTestId("event-entities")).toContainText("Qwen 3.5");
        await expect(first.getByTestId("event-summary")).toContainText("AI の要約");
        await expect(page.getByTestId("event-row").nth(1).getByTestId("primary-mark")).toHaveCount(0);
        await expectNoHorizontalOverflow(page);
        await shot(page, `events-list-${vp.name}-${scheme}`);
        await page.getByTestId("event-primary").check();
        await expect(page.getByTestId("event-row")).toHaveCount(1);
        await page.getByTestId("event-sort").getByRole("radio", { name: "記事の多い順" }).click();
        expect(state.calls.some((c) => c.path === "/events")).toBe(true);
        expect(state.unhandled).toEqual([]);
      });

      test("イベントのページ: 記事が時刻順・出どころの種類ごとに絞れる。見つからないときは案内", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/events");
        await page.getByTestId("event-link").first().click();
        await expect(page.getByTestId("event-title")).toContainText("Qwen 3.5");
        await expect(page.getByTestId("event-since")).toContainText("初報 4 時間前");
        await expect(page.getByTestId("event-summary")).toContainText("Alibaba");
        const rows = page.getByTestId("event-article");
        await expect(rows).toHaveCount(4);
        await expect(rows.first()).toContainText("公式");
        await expect(rows.first()).toContainText("代表");
        await expectNoHorizontalOverflow(page);
        await shot(page, `event-page-${vp.name}-${scheme}`);
        await page.getByTestId("event-origins").getByRole("radio", { name: /報道/ }).click();
        await expect(rows).toHaveCount(2);
        await page.getByTestId("event-origins").getByRole("radio", { name: /すべて/ }).click();
        await expect(rows).toHaveCount(4);
        await expect(rows.first().getByRole("link")).toHaveAttribute("rel", "noopener noreferrer");
        await page.goto("/events/99");
        await expect(page.getByRole("alert")).toContainText("見つかりません");
        expect(state.unhandled).toEqual([]);
      });

      test("ニュースの一覧: 同じイベントの記事は 1 つにまとまり「ほか N 件」からイベントのページへ行ける", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/");
        await expect(page.getByTestId("news-root")).toBeVisible();
        await page.getByRole("tab", { name: "新着" }).click();
        const link = page.getByTestId("news-event");
        await expect(link).toHaveCount(1);
        await expect(link).toContainText("ほか 3 件");
        await expect(link).toContainText("一次情報あり");
        await expectNoHorizontalOverflow(page);
        await shot(page, `news-event-${vp.name}-${scheme}`);
        await link.click();
        await expect(page.getByTestId("event-page")).toBeVisible();
      });

      test("分析: 独立したイベント・新着の傾向（今日の傾向の解説とリンク・新しく出た物）・物のホットトピック", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/analytics");
        await expect(page.getByTestId("card-events")).toContainText("612 件");
        await expect(page.getByTestId("card-events")).toContainText("まとまり 41 件");
        await expect(page.getByTestId("hot-list").getByTestId("hot-entity-mark")).toHaveCount(1);
        await expect(page.getByTestId("hot-list")).toContainText("Qwen 3.5");
        const sec = page.getByTestId("sec-arrivals");
        await expect(sec).toBeVisible();
        await expect(sec.getByTestId("trend-note")).toContainText("AI の解説");
        await expect(sec.getByTestId("trend-note-line")).toHaveCount(2);
        await expect(sec.getByTestId("arrivals-table")).toContainText("980 件");
        await expect(sec.getByTestId("arrivals-table")).toContainText("一次情報の率");
        await expect(sec.getByTestId("arrivals-new")).toContainText("Zorblax Labs");
        await expect(sec.getByTestId("arrivals-surging")).toContainText("Qwen 3.5");
        await expect(sec.getByTestId("arrivals-channels")).toContainText("AI・LLM");
        await sec.scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(page);
        await shot(page, `analytics-arrivals-${vp.name}-${scheme}`);
        await sec.getByRole("link", { name: /イベント V1/ }).click();
        await expect(page.getByTestId("event-page")).toBeVisible();
        expect(state.unhandled).toEqual([]);
      });

      test("分析: 解説も新しい物も無いときの空の表示", async ({ page }) => {
        const state = newState();
        state.analyticsEmpty = true;
        await mockApi(page, state);
        await page.goto("/analytics");
        await expect(page.getByTestId("trend-note-empty")).toBeVisible();
        await expect(page.getByTestId("arrivals-new-empty")).toBeVisible();
        await expectNoHorizontalOverflow(page);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}
