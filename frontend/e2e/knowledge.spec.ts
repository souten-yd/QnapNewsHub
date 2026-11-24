/** 知識カード・知識の地図・Obsidian の書き出し（v0.5）。
 *  E2E の目的: ①カードに概要（AI の印）・関係（推測の印）・変化と矛盾・主張の根拠が出て、利用者のメモが打つだけで保存される ②地図が遅延読み込みで描かれ、始める物から段階的に広がり、型・確かさ・期間で絞れ、
 *  点を押すとカードへ行ける ③書き出しは押したときだけ zip を受け取り、機密の区分の既定は含めない。320px と 1280px・ライト／ダークではみ出さない。
 *  根拠: 利用者の指示 v0.5（知識カード・知識の地図・Obsidian の書き出し）・AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test, type Page } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;
const tap = (page: Page, id: number) => page.evaluate((i) => (document.querySelector("[data-testid=map-canvas]") as unknown as { __cy: { getElementById(s: string): { emit(e: string): void } } }).__cy.getElementById(String(i)).emit("tap"), id);

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`知識 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("知識カード: 概要・関係・変化と矛盾・根拠・メモ（打つとすぐ保存・読み直しても残る）", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities/2");
        const card = page.getByTestId("knowledge-card");
        await expect(card).toBeVisible();
        await expect(page.getByTestId("card-overview")).toContainText("AI の要約");
        await expect(page.getByTestId("card-relations").locator("li")).toHaveCount(3);
        await expect(page.getByTestId("card-relations").locator('li[data-status="inferred"]')).toContainText("推測");
        await expect(page.getByTestId("card-history").locator('li[data-mark="change"]')).toContainText("前は 1000");
        await expect(page.getByTestId("card-history").locator('li[data-mark="contradiction"]')).toHaveCount(2);
        await expect(page.getByRole("heading", { name: /変化 1 · 矛盾 2/ })).toBeVisible();
        const claim = page.getByTestId("card-claims").locator("li").first();
        await claim.locator("summary").click();
        await expect(claim).toContainText("p.3");
        await expect(claim).toContainText("Qwen 3.5 を公開");
        await expectNoHorizontalOverflow(page);
        await shot(page, `card-${vp.name}-${scheme}`);
        const note = page.getByTestId("card-note");
        await note.fill("自分の考察: R9700 で試す");
        await expect(page.getByTestId("note-state")).toContainText("保存しました");
        expect(state.kgNote).toBe("自分の考察: R9700 で試す");
        await page.reload();
        await expect(page.getByTestId("card-note")).toHaveValue("自分の考察: R9700 で試す");
        await page.getByTestId("card-open-map").click();
        await expect(page).toHaveURL(/\/map\?focus=2$/);
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-nodes", "4");      // Qwen・Alibaba・llama.cpp・GGUF
        expect(state.unhandled).toEqual([]);
      });

      test("カードの概要が無い物は「まだ概要はありません」・関係が無ければ空の表示", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities/4");
        await expect(page.getByTestId("card-no-overview")).toBeVisible();
        await expect(page.getByTestId("card-no-relations")).toBeVisible();
        await expect(page.getByTestId("card-no-history")).toBeVisible();
        expect(state.unhandled).toEqual([]);
      });

      test("知識の地図: 始める物から広げ、型・確かさ・期間で絞り、点を押してカードへ", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/map");
        await expect(page.getByTestId("map-empty")).toBeVisible();
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-ready", "true");
        await shot(page, `map-empty-${vp.name}-${scheme}`);
        await page.getByTestId("map-search").fill("llama");
        await page.getByTestId("map-result").first().click();
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-nodes", "5");
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-edges", "4");
        await expect(page.getByTestId("map-count")).toContainText("点 5 · 線 4");
        await expectNoHorizontalOverflow(page);
        await shot(page, `map-${vp.name}-${scheme}`);
        // 絞り込み（期間・確かさ・型）は読み込み済みの線を隠す。点・線は消さない
        await page.getByTestId("legend-co_occurs").click();
        await expect(page.getByTestId("map-count")).toContainText("1 本を隠しています");
        await page.getByTestId("legend-co_occurs").click();
        await page.getByTestId("map-filters").getByLabel("確かさ").selectOption("0.7");
        await expect(page.getByTestId("map-count")).toContainText("3 本を隠しています");
        await page.getByTestId("map-filters").getByLabel("確かさ").selectOption("0");
        await expect(page.getByTestId("map-count")).not.toContainText("隠しています");
        // 点を押す → 札 → 周りを広げる（段階的）。広げた先の「推測」の線は 200 日前が最後なので、期間 90 日で隠れる
        await tap(page, 2);
        await expect(page.getByTestId("map-selected")).toContainText("Qwen 3.5");
        await page.getByTestId("map-expand").click();
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-nodes", "7");
        await page.getByTestId("map-filters").getByLabel("期間").selectOption("90");
        await expect(page.getByTestId("map-count")).toContainText("1 本を隠しています");
        await page.getByTestId("map-filters").getByLabel("物の種類").selectOption("org");
        await expect(page.getByTestId("map-count")).toContainText("点 7");
        await expectNoHorizontalOverflow(page);
        await shot(page, `map-expanded-${vp.name}-${scheme}`);
        await page.getByTestId("map-open-card").click();
        await expect(page).toHaveURL(/\/entities\/2$/);
        await expect(page.getByTestId("knowledge-card")).toBeVisible();
        expect(state.unhandled).toEqual([]);
        const calls = state.calls.filter((c) => c.path === "/graph/neighbors");
        expect(calls.length).toBe(2);      // 一度に全部ではなく、始めと広げるの 2 回だけ
        expect((calls[1].body as { ids: number[] }).ids).toEqual([2]);
      });

      test("Obsidian の書き出し: 押したときだけ zip を受け取る。機密の区分は既定で含めない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/map");
        await expect(page.getByTestId("export-form")).toBeVisible();
        expect(state.calls.some((c) => c.path === "/kg/export")).toBe(false);      // 開いただけでは書き出さない
        await shot(page, `export-${vp.name}-${scheme}`);
        const dl = page.waitForEvent("download");
        await page.getByTestId("export-button").click();
        expect((await dl).suggestedFilename()).toBe("knowledge.zip");
        await expect(page.getByTestId("export-state")).toContainText("3 件");
        let body = state.calls.find((c) => c.path === "/kg/export")!.body as { confidential: boolean; min_articles: number };
        expect(body.confidential).toBe(false);
        expect(body.min_articles).toBe(3);
        await page.getByTestId("export-confidential").check();
        const dl2 = page.waitForEvent("download");
        await page.getByTestId("export-button").click();
        await dl2;
        body = state.calls.filter((c) => c.path === "/kg/export")[1].body as { confidential: boolean; min_articles: number };
        expect(body.confidential).toBe(true);
        await expectNoHorizontalOverflow(page);
        expect(state.unhandled).toEqual([]);
      });

      test("分析の画面の「知識の地図」の枠が地図へのリンクになり、メニューにも出る", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/analytics");
        await page.getByTestId("map-link").click();
        await expect(page).toHaveURL(/\/map$/);
        await expect(page.getByTestId("map-page")).toBeVisible();
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}
