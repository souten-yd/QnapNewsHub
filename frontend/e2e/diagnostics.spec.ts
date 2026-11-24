/** 診断の画面・お休みモードの設定。E2E の目的: 診断の 4 区分と自己点検・診断の束・お休みの節が、320 と 1280・ライト／ダークではみ出さず、
 *  お休みの説明（停電で消えうる）・未保存の件数・ホームの一行の知らせが出ること。根拠: ARCHITECTURE 9・9.5、AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`診断・お休み ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("診断の画面が開き、自己点検ができ、はみ出さない", async ({ page }) => {
        const state = newState({ diagProblems: true });
        await mockApi(page, state);
        await page.goto("/diagnostics");
        await expect(page.getByTestId("diag-page")).toBeVisible();
        await expect(page.getByTestId("diag-problems")).toContainText("取得が止まった可能性");
        await expect(page.getByTestId("fetch-stalled")).toContainText("1 件");
        await expect(page.getByTestId("llm-total")).toContainText("14 / 800");
        await expect(page.getByTestId("unsaved")).toContainText("記事 12 件");
        await expect(page.getByTestId("io-write")).toContainText("1.0 MB");
        await expectNoHorizontalOverflow(page);
        await shot(page, `diag-top-${vp.name}-${scheme}`);
        await page.getByRole("button", { name: "点検する" }).click();
        await expect(page.getByTestId("selfcheck")).toContainText("LLM の鍵");
        await expect(page.getByTestId("selfcheck")).toContainText("直し方");
        await page.getByTestId("selfcheck").scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(page);
        await shot(page, `diag-selfcheck-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("お休みモードの節: 入れると時刻と 3 つの動きと停電の説明が出て、保存できる", async ({ page }) => {
        const state = newState({ restActive: true });
        await mockApi(page, state);
        await page.goto("/settings");
        await expect(page.getByTestId("rest-active")).toBeVisible();
        const card = page.locator("section", { has: page.locator("#news-rest") });
        await card.getByText("お休みモードを使う").click();
        await expect(card.getByTestId("rest-warning")).toContainText("停電");
        await expect(card.getByRole("radio")).toHaveCount(3);
        await card.getByLabel("お休みの開始").fill("23:00");
        await card.getByLabel("お休みの終わり").fill("05:30");
        await card.getByRole("radio", { name: "集める・分析・保存をすべて休む" }).check();
        await card.scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(page);
        await shot(page, `settings-rest-${vp.name}-${scheme}`);
        await card.getByRole("button", { name: "保存" }).click();
        await expect(page.getByText("保存しました").first()).toBeVisible();
        const put = state.calls.find((c) => c.method === "PUT" && c.path === "/news/settings")?.body as { rest: unknown };
        expect(put.rest).toEqual({ enabled: true, start: "23:00", end: "05:30", mode: "stop_all" });
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}

test.describe("ホームの一行の知らせ", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("問題のあるときだけ出て、押すと診断へ行く。無ければ出ない", async ({ page }) => {
    const quiet = newState();
    await mockApi(page, quiet);
    await page.goto("/");
    await expect(page.getByTestId("news-root")).toBeVisible();
    await expect(page.getByTestId("diag-notice")).toHaveCount(0);
    const bad = newState({ diagProblems: true });
    const p2 = await page.context().newPage();
    await mockApi(p2, bad);
    await p2.goto("/");
    await expect(p2.getByTestId("diag-notice")).toContainText("止まった可能性");
    await shot(p2, "home-notice-390");
    await p2.getByTestId("diag-notice").click();
    await expect(p2.getByTestId("diag-page")).toBeVisible();
  });

  test("メニュー（☰）に「診断」がある", async ({ page }) => {
    await mockApi(page, newState());
    await page.goto("/");
    await page.getByRole("button", { name: "メニューを開く" }).click();
    await page.getByTestId("nav-drawer").getByRole("link", { name: "診断" }).click();
    await expect(page.getByTestId("diag-page")).toBeVisible();
  });
});
