/** 設定: チャンネル・表示・好み・OpenRouter の鍵（値は画面に出ない）・LLM（モデル・今日の呼び出し・上限）・まとめの重点・待ち受け。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, VIEWPORTS, expectNoHorizontalOverflow, shot } from "./helpers";

// 見本の鍵は実行時に組み立てる（公開のリポジトリに鍵らしい値を書かない）
const FAKE_KEY = ["sk", "or", "v1", "e2e", "0123456789abcdef"].join("-");

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test.describe(`設定 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("設定が開き、はみ出さない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/settings");
        await expect(page.getByTestId("key-status")).toContainText("未設定");
        await expect(page.getByTestId("llm-stats")).toContainText("14 / 800");
        await expectNoHorizontalOverflow(page);
        await shot(page, `settings-top-${vp.name}-${scheme}`);
        await page.locator("#news-body").scrollIntoViewIfNeeded();
        await expect(page.getByTestId("body-per-day")).toHaveValue("200");
        await expect(page.getByTestId("compress-years")).toHaveValue("1");
        await shot(page, `settings-body-${vp.name}-${scheme}`);
        await page.locator("#news-key").scrollIntoViewIfNeeded();
        await shot(page, `settings-key-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}

test.describe("OpenRouter の鍵", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("鍵は伏せ字で入れ、保存したら入力欄を空にし、画面のどこにも値が出ない。消せる", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/settings");
    const input = page.locator('input[name="openrouter-key"]');
    await expect(input).toHaveAttribute("type", "password");
    await input.fill(FAKE_KEY);
    await expect(input).toHaveValue(FAKE_KEY);      // 入れている間だけ入力欄にある
    await page.getByRole("button", { name: "鍵を保存" }).click();
    await expect(page.getByTestId("key-status")).toContainText("鍵は入っています");
    await expect(input).toHaveValue("");
    expect(state.calls.find((c) => c.method === "PUT" && c.path === "/news/openrouter-key")?.body).toEqual({ key: FAKE_KEY });
    // 画面の文字・属性・保存先（localStorage）のどこにも値が残っていない
    expect(await page.evaluate(() => document.documentElement.outerHTML)).not.toContain(FAKE_KEY);
    expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(FAKE_KEY);
    expect(await page.locator("input").evaluateAll((els) => els.map((e) => (e as HTMLInputElement).value).join("|"))).not.toContain(FAKE_KEY);
    // 鍵の取得の応答は「入っているか」だけ
    expect(state.calls.filter((c) => c.method === "GET" && c.path === "/news/openrouter-key").length).toBeGreaterThan(0);
    await shot(page, "settings-key-saved-390");
    await page.getByRole("button", { name: "鍵を消す" }).click();
    await expect(page.getByTestId("key-status")).toContainText("未設定");
  });
});

test.describe("LLM・更新・表示・待ち受けの保存", () => {
  test.use({ viewport: { width: 1280, height: 900 } });

  test("モデル・上限・まとめの重点を保存でき、今日の呼び出しと失敗の理由が見える", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/settings");
    await expect(page.getByTestId("llm-stats")).toContainText("下ごしらえ 12");
    await expect(page.getByTestId("llm-stats")).toContainText("混み合っています");
    await page.getByLabel("まとめのモデル").fill("example/other-model:free");
    await page.getByLabel("1 日の上限").fill("120");
    await page.getByTestId("digest-focus").fill("AI 技術の最新動向");
    await page.locator("#news-llm").locator("xpath=..").getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("保存しました").first()).toBeVisible();
    const put = state.calls.find((c) => c.method === "PUT" && c.path === "/news/settings")!.body as { llm: Record<string, unknown> };
    expect(put.llm).toMatchObject({ digest_model: "example/other-model:free", daily_limit: 120, digest_focus: "AI 技術の最新動向" });
    await expect(page.getByTestId("llm-stats")).toContainText("/ 120");
  });

  test("表示のデザインを選ぶと保存され、ニュースの画面が変わる", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/settings");
    await page.locator("[data-design-option=newspaper]").click();
    await expect.poll(() => state.prefs.display.design).toBe("newspaper");
    await page.getByRole("link", { name: "ニュース" }).first().click();
    await expect(page.getByTestId("news-root")).toHaveAttribute("data-design", "newspaper");
  });

  test("好み: ミュートの語を足せる。チャットの話題の項目は無い", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/settings");
    await expect(page.getByText("チャットの話題を使う")).toHaveCount(0);
    await page.getByLabel("ミュートの語を追加").fill("広告");
    await page.getByLabel("ミュートの語を追加").press("Enter");
    await expect.poll(() => state.prefs.taste.mute_terms).toEqual(["広告"]);
  });

  test("待ち受けは保存すると再起動が要ると出る。ControlDeck 専用の項目（画像生成・RAG・CPU の LLM）は無い", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/settings");
    for (const gone of ["画像の無い記事のイメージ画像", "RAG", "CPU の LLM", "Models を開く"]) await expect(page.getByText(gone)).toHaveCount(0);
    await page.getByLabel("待ち受けるアドレス").selectOption("0.0.0.0");
    await page.locator("#news-listen").locator("xpath=..").getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("再起動すると効きます")).toBeVisible();
    expect(state.calls.find((c) => c.method === "PUT" && c.path === "/auth/hub")?.body).toMatchObject({ host: "0.0.0.0" });
  });
});
