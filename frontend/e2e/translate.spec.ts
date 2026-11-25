/** 翻訳: 設定の節（モデルの取得・対訳表・試し訳）・記事の「機械翻訳」の印と原文の切り替え・本文の訳の要求・診断の節。 */
import { expect, test } from "@playwright/test";
import { HOME, ITEMS, mockApi, newState } from "./mock";
import { SCHEMES, VIEWPORTS, expectNoHorizontalOverflow, shot } from "./helpers";

const MT_ITEM = { ...ITEMS[0], id: 1, title: "Faster quantization lands in llama.cpp", summary: "軽い量子化が入りました。", translated: true, mt_summary: true, summary_original: "A faster quantization path lands.",
  mt: { title: "llama.cpp にさらに速い量子化", summary: "軽い量子化が入りました。", engine: "quickmt-en-ja@c09e98b8:int8:beam2", hq: false, at: "2026-11-25T01:00:00Z", body: false, partial: false } };

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test.describe(`翻訳 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("記事に機械翻訳の印・原文の切り替え・本文を訳す", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.route("**/api/v1/news/home*", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...HOME, onair: MT_ITEM }) }));
        await page.goto("/");
        await expect(page.getByTestId("onair-title")).toContainText("llama.cpp にさらに速い量子化");
        const mark = page.getByTestId("mt-mark").first();
        await expect(mark).toContainText("機械翻訳");
        await mark.locator("summary").click();
        await expect(page.getByTestId("mt-original-title").first()).toContainText("Faster quantization lands");
        await page.getByTestId("mt-body").first().getByRole("button", { name: "本文を訳す" }).click();
        await page.getByTestId("mt-body").first().getByRole("button", { name: "本文を訳す" }).click();
        await expect(page.getByTestId("mt-body").first()).toContainText("〔未訳〕 が付いた文は検査に通らなかった");
        await expectNoHorizontalOverflow(page);
        await shot(page, `translate-mark-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("設定の翻訳: モデルを取得・対訳表・試し訳", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/settings");
        const card = page.locator("#news-translate");
        await card.scrollIntoViewIfNeeded();
        await expect(page.getByTestId("tr-model")).toContainText("まだ入っていません");
        await expect(page.getByTestId("tr-model")).toContainText("CC-BY-4.0");
        await expect(page.getByTestId("tr-glossary")).toHaveValue("prefill = プリフィル");
        await page.getByTestId("tr-fetch").click();
        await expect(page.getByTestId("tr-model")).toContainText("入っています");
        await page.getByRole("button", { name: "訳してみる" }).click();
        await expect(page.getByTestId("tr-trial")).toContainText("量子化はプリフィル");
        await page.getByTestId("tr-glossary").fill("prefill = プリフィル\nlatency = 遅延");
        await page.getByRole("button", { name: "保存" }).nth(0).waitFor();
        await expectNoHorizontalOverflow(page);
        await shot(page, `settings-translate-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("診断に翻訳の節", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/diagnostics");
        await expect(page.getByTestId("diag-translate")).toContainText("保存した訳");
        await expect(page.getByTestId("tr-cache")).toContainText("25 %");
        await expect(page.getByTestId("tr-fail")).toContainText("3.3");
        await expectNoHorizontalOverflow(page);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}
