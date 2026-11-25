/** 取得先の管理の「connector」の節の E2E。
 *  目的: connector の一覧（種類・入切・利用条件の状態）・今日の使用（件数と通信量）・予算で止めている知らせ・利用条件の詳細（ライセンス・保存・再配布・レート・公式の URL・確かめた日）・
 *  保存の段（L0〜L4）の数・昇格の規則・未確認の connector は確認の明示がないと入れられないこと・鍵の値が画面に残らないことが、320 と 1280・ライト／ダークではみ出さず、API へ正しく送られること。
 *  根拠: 利用者の指示（v0.7 前半・2026-11-25「取得先の管理の画面に connector の節」）、ControlDeck の PLAN-integrated-knowledge 8 章、AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;
const SECRET = "ghp_" + "q".repeat(24);

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`connector ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("一覧・予算の使用・止めている知らせ・段の数・昇格の規則", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        await page.getByTestId("news-tab-connector").click();
        await expect(page.getByTestId("connector-row")).toHaveCount(11);
        const disc = page.locator('[data-testid="connector-row"][data-id="github_discover"]');
        await expect(disc.getByTestId("connector-budget-stop")).toContainText("1 日の件数の予算");
        await expect(disc.getByTestId("connector-usage-items")).toContainText("300 / 300 件");
        await expect(disc.getByTestId("connector-usage-mb")).toContainText("4.8 / 10 MB");
        await expect(page.locator('[data-testid="connector-row"][data-id="hn"]').getByTestId("connector-terms-status")).toContainText("未確認");
        await expect(page.getByTestId("connector-tier-0")).toHaveText("640");
        await expect(page.getByTestId("connector-tier-3")).toHaveText("880");
        await expect(page.getByTestId("connector-rule-surge")).toBeChecked();
        await expectNoHorizontalOverflow(page);
        await shot(page, `connectors-${vp.name}-${scheme}`);
        await page.getByTestId("connector-rule-hot").click();
        await expect.poll(() => state.calls.find((c) => c.method === "PUT" && c.path === "/connectors/promotion")?.body).toEqual({ hot: false });
        expect(state.unhandled).toEqual([]);
      });

      test("利用条件の詳細・予算の保存・鍵（値は残らない）", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        await page.getByTestId("news-tab-connector").click();
        const row = page.locator('[data-testid="connector-row"][data-id="openalex"]');
        await row.getByTestId("connector-open").click();
        const terms = row.getByTestId("connector-terms");
        await expect(terms).toContainText("CC0");
        await expect(terms).toContainText("2026-11-25");
        await expect(terms.getByRole("link")).toHaveAttribute("href", "https://example.test/terms/api");
        await expectNoHorizontalOverflow(page);
        await shot(page, `connector-terms-${vp.name}-${scheme}`);
        await row.getByTestId("connector-daily-items").fill("50");
        await row.getByTestId("connector-save-budget").click();
        await expect.poll(() => state.calls.find((c) => c.method === "PUT" && c.path === "/connectors/openalex")?.body).toMatchObject({ daily_items: 50, daily_mb: 10, interval_minutes: 1440 });
        await row.getByLabel("OpenAlex（論文の書誌・引用数） の鍵").fill(SECRET);
        await row.getByTestId("connector-key").getByRole("button", { name: "保存" }).click();
        await expect(row.getByTestId("connector-key")).toContainText("入っています");
        expect(await page.locator("body").innerText()).not.toContain(SECRET);
        expect(await page.locator('input[type="password"]').first().inputValue()).toBe("");
      });

      test("新しい connector が一覧に出て、鍵の要るものは「鍵が要る」と出て、鍵を入れるまで入れられない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        await page.getByTestId("news-tab-connector").click();
        await expect(page.locator('[data-testid="connector-row"][data-id="boj"]')).toBeVisible();
        await expect(page.locator('[data-testid="connector-row"][data-id="boj"]').getByTestId("connector-key-badge")).toHaveCount(0);
        const row = page.locator('[data-testid="connector-row"][data-id="coingecko"]');
        await expect(row.getByTestId("connector-key-badge")).toHaveText("鍵が要る");
        await expect(row.getByTestId("connector-key-needed")).toContainText("鍵が要ります");
        await expect(row.getByTestId("connector-enabled")).toBeDisabled();
        await expectNoHorizontalOverflow(page);
        await shot(page, `connector-key-needed-${vp.name}-${scheme}`);
        await row.getByTestId("connector-open").click();
        await expect(row.getByTestId("connector-key")).toContainText("鍵（必須）");
        await row.getByLabel("CoinGecko（価格） の鍵").fill(SECRET);
        await row.getByTestId("connector-key").getByRole("button", { name: "保存" }).click();
        await expect(row.getByTestId("connector-key-badge")).toHaveText("鍵あり");
        await expect(row.getByTestId("connector-enabled")).toBeEnabled();
        await row.getByTestId("connector-enabled").click();
        await expect.poll(() => state.calls.find((c) => c.method === "PUT" && c.path === "/connectors/coingecko")?.body).toMatchObject({ enabled: true });
        expect(await page.locator("body").innerText()).not.toContain(SECRET);
        expect(state.unhandled).toEqual([]);
      });

      test("未確認の connector は確認の明示がないと入れられない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        await page.getByTestId("news-tab-connector").click();
        const row = page.locator('[data-testid="connector-row"][data-id="hn_search"]');
        await expect(row.getByTestId("connector-enabled")).toBeDisabled();
        await expect(row.getByTestId("connector-ack")).toBeVisible();
        await shot(page, `connector-ack-${vp.name}-${scheme}`);
        await row.getByTestId("connector-ack").locator("input").check();
        await row.getByTestId("connector-enabled").click();
        await expect.poll(() => state.calls.find((c) => c.method === "PUT" && c.path === "/connectors/hn_search")?.body).toMatchObject({ enabled: true, terms_ack: true });
        await expectNoHorizontalOverflow(page);
      });
    });
  }
}
