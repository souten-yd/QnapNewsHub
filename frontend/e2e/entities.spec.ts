/** 物（要素の取り出し）・カードの札・設定の予算とモデルの順番・診断の割り当て。
 *  E2E の目的: 物の一覧・物のページ・カードの札・設定の「予算と割り当て」「次のモデル」が 320 と 1280・ライト／ダークではみ出さず、AI の取り出しの印と有料モデルの注意が出ること。
 *  根拠: ARCHITECTURE 3・5.1（Entity・Claim・予算と割り当て・モデルの切り替え）、AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`Things ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("Things の一覧: 検索・種類で絞れ、物のページへ行ける。はみ出さない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities");
        await expect(page.getByTestId("entity-row")).toHaveCount(6);
        await expect(page.getByTestId("entities-total")).toContainText("6 件");
        await expectNoHorizontalOverflow(page);
        await shot(page, `entities-list-${vp.name}-${scheme}`);
        await page.getByLabel("Thingsを探す").fill("qwen");
        await expect(page.getByTestId("entity-row")).toHaveCount(1);
        await page.getByLabel("Thingsを探す").fill("");
        await page.getByLabel("種類").selectOption("org");
        await expect(page.getByTestId("entity-row")).toHaveCount(1);
        await page.getByLabel("種類").selectOption("");
        await page.getByLabel("Thingsを探す").fill("zzz");
        await expect(page.getByTestId("entities-empty")).toBeVisible();
        await page.getByLabel("Thingsを探す").fill("qwen");
        await page.getByTestId("entity-row").click();
        await expect(page.getByTestId("entity-name")).toContainText("Qwen 3.5");
        expect(state.unhandled).toEqual([]);
      });

      test("物のページ: facts に AI の取り出しの印・年表・一緒に出る物", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities/2");
        await expect(page.getByTestId("entity-page")).toBeVisible();
        await expect(page.getByTestId("ai-mark").first()).toContainText("AI の取り出し");
        await expect(page.getByTestId("entity-facts")).toContainText("Qwen 3.5 が公開された");
        await expect(page.getByTestId("entity-timeline").locator("li")).toHaveCount(3);
        await expect(page.getByTestId("entity-related")).toContainText("Alibaba");
        await expectNoHorizontalOverflow(page);
        await shot(page, `entity-page-${vp.name}-${scheme}`);
        await page.getByTestId("entity-related").getByRole("link", { name: /Alibaba/ }).click();
        await expect(page).toHaveURL(/\/entities\/3$/);
        expect(state.unhandled).toEqual([]);
      });

      test("ニュースのカードに物の札が付き、押すと物のページへ", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/");
        const tags = page.getByTestId("card-entities").first();
        await expect(tags).toBeVisible();
        await expect(tags).toContainText("llama.cpp");
        await expectNoHorizontalOverflow(page);
        await shot(page, `card-entity-tags-${vp.name}-${scheme}`);
        await tags.getByRole("link", { name: "llama.cpp" }).click();
        await expect(page).toHaveURL(/\/entities\/1$/);
        expect(state.unhandled).toEqual([]);
      });

      test("設定: 予算と割り当て（合計の検査）・次のモデルと有料の注意", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/settings");
        const budget = page.locator("section", { has: page.locator("#news-budget") });
        await expect(budget.getByTestId("quota-sum")).toContainText("合計 800 / 1 日の上限 800");
        await expect(budget.getByTestId("quota-prep")).toHaveValue("300");
        await budget.getByTestId("quota-prep").fill("350");
        await expect(budget.getByTestId("quota-sum")).toContainText("保存できません");
        await expect(budget.getByRole("button", { name: "保存" })).toBeDisabled();
        await budget.getByTestId("quota-spare").fill("20");
        await expect(budget.getByRole("button", { name: "保存" })).toBeEnabled();
        await budget.scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(page);
        await shot(page, `settings-budget-${vp.name}-${scheme}`);
        const chain = page.getByTestId("model-chain");
        await chain.getByRole("button", { name: "モデルを足す" }).click();
        await chain.getByLabel("2 番目のモデル", { exact: true }).fill("vendor/paid-model");
        await expect(chain.getByTestId("paid-warning")).toContainText("有料");
        await chain.getByLabel("2 番目のモデル", { exact: true }).fill("vendor/second:free");
        await expect(chain.getByTestId("paid-warning")).toHaveCount(0);
        await chain.scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(page);
        await shot(page, `settings-model-chain-${vp.name}-${scheme}`);
        await budget.getByRole("button", { name: "保存" }).click();
        await expect.poll(() => state.settings.llm.fallback_models).toEqual(["vendor/second:free"]);
        expect(state.settings.llm.quota.prep).toBe(350);
        expect(state.unhandled).toEqual([]);
      });

      test("ゲートウェイ: 接続を試してモデルを選べ、鍵は伏せ字で値が画面に残らない。順番と行き先ごとの成績が出る", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/settings");
        const card = page.locator("section", { has: page.locator("#news-gateway") });
        await card.getByText("ゲートウェイを使う").click();
        await card.getByTestId("gateway-url").fill("http://gateway.example.test:8765/api/v1/llm/v1");
        await card.getByTestId("gateway-test").click();
        await expect(card.getByTestId("gateway-result")).toContainText("つながりました");
        await expect(card.getByTestId("gateway-result")).toContainText("モデル 3 件");
        await expect(card.locator("#gateway-models option")).toHaveCount(3);
        const keyInput = card.locator('input[name="gateway-key"]');
        await expect(keyInput).toHaveAttribute("type", "password");
        await keyInput.fill("gw-e2e-0123456789");
        await card.getByRole("button", { name: "登録する" }).click();
        await expect(card.getByTestId("gateway-key-status")).toContainText("鍵は入っています");
        await expect(keyInput).toHaveValue("");
        expect(await page.content()).not.toContain("gw-e2e-0123456789");
        await expect(card.getByTestId("provider-order")).toContainText("OpenRouter");
        await card.scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(page);
        await shot(page, `settings-gateway-${vp.name}-${scheme}`);
        await card.getByRole("button", { name: "保存" }).click();
        await expect.poll(() => state.settings.llm.gateway.enabled).toBe(true);
        expect(state.settings.llm.gateway.base_url).toContain("gateway.example.test");
        state.gatewayDown = true;
        await card.getByTestId("gateway-test").click();
        await expect(card.getByTestId("gateway-result")).toContainText("つながりません");
        expect(state.unhandled).toEqual([]);
      });

      test("診断: 用途ごとの使い方・モデルの並びと切り替え・取り出した数", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/diagnostics");
        await expect(page.getByTestId("llm-quota")).toContainText("下ごしらえと取り出し");
        await expect(page.getByTestId("llm-current-model")).toContainText("second-model");
        await expect(page.getByTestId("llm-chain-events")).toContainText("切り替え");
        await expect(page.getByTestId("llm-knowledge")).toContainText("128 Things");
        await expect(page.getByTestId("llm-gateway")).toContainText("あと約 8 分");
        await page.getByTestId("llm-quota").scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(page);
        await shot(page, `diag-llm-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}
