/** ニュースの画面: 放送局・新聞のデザイン・画像の無い記事・元のページを開く・まとめのバブル・新着・アーカイブ・検索・好み。 */
import { expect, test, type Page } from "@playwright/test";
import { ITEMS, mockApi, newState, type MockState } from "./mock";
import { SCHEMES, VIEWPORTS, expectNoHorizontalOverflow, shot } from "./helpers";

async function open(page: Page, state: MockState = newState()) {
  await mockApi(page, state);
  await page.goto("/");
  await expect(page.getByTestId("news-root")).toBeVisible();
  await expect(page.getByTestId("onair-title").or(page.getByTestId("np-band")).first()).toBeVisible();
  return state;
}

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test.describe(`デザイン ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("放送局: ON AIR・テロップ・数字・チャンネルの区画", async ({ page }) => {
        const state = await open(page);
        await expect(page.getByTestId("onair-title")).toContainText("新しい量子化");
        await expect(page.getByTestId("ticker-track")).toBeVisible();
        await expect(page.getByTestId("numbers")).toBeVisible();
        await expect(page.getByTestId("section-ai")).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await shot(page, `news-broadcast-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("新聞: 数字の帯・速報", async ({ page }) => {
        const state = newState();
        state.prefs.display.design = "newspaper";
        await open(page, state);
        await expect(page.getByTestId("np-band")).toBeVisible();
        await expect(page.getByTestId("np-flash")).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await shot(page, `news-newspaper-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("まとめのバブル: 開くと本文と出典が出る", async ({ page }) => {
        await open(page);
        const daily = page.getByTestId("digest-daily");
        await expect(daily).toHaveAttribute("data-open", "false");
        await expect(page.getByTestId("digest-hint").first()).toContainText("新しい量子化");
        await shot(page, `bubbles-closed-${vp.name}-${scheme}`);
        await daily.getByRole("button", { name: /今朝のまとめ/ }).click();
        await expect(daily).toHaveAttribute("data-open", "true");
        await expect(page.getByTestId("digest-three")).toBeVisible();
        await expect(page.getByTestId("digest-topic").first()).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await shot(page, `bubbles-open-${vp.name}-${scheme}`);
      });
    });
  }
}

test.describe("画像の無い記事は画像の部分を出さない", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("カードに画像・生成の印が無く、アクセントラインだけ。画像のある記事は画像が出る", async ({ page }) => {
    await open(page);
    await page.getByRole("tab", { name: "新着" }).click();
    const noImg = page.locator("[data-news-card]", { hasText: "画像のない記事" });
    await expect(noImg).toHaveCount(1);
    await expect(noImg.locator("img")).toHaveCount(0);
    const lineHeight = await noImg.locator("[data-noimg]").evaluate((el) => el.getBoundingClientRect().height);
    expect(lineHeight).toBeLessThanOrEqual(6);      // 細い線（5px）だけで、大きな箱にしない
    const withImg = page.locator("[data-news-card]", { hasText: "新しい量子化" });
    await expect(withImg.locator("img")).toHaveCount(1);
    await expect.poll(() => withImg.locator("img").evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
    await expect(page.getByText("AI 生成")).toHaveCount(0);
    await shot(page, "news-no-image-390");
  });

  test("ON AIR の記事に画像が無いときも、画像の箱を出さない", async ({ page }) => {
    const state = newState({ items: ITEMS.map((i) => ({ ...i, image_url: null })) });
    await open(page, state);
    await expect(page.getByTestId("onair-swipe").locator("img")).toHaveCount(0);
    await expect(page.locator("[data-news-card] img")).toHaveCount(0);
    const h = await page.locator(".nw-screen-inner").first().evaluate((el) => el.getBoundingClientRect().height);
    expect(h).toBeLessThanOrEqual(6);
    await shot(page, "news-onair-no-image-390");
  });
});

test.describe("記事は元のページを開く", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("新しいタブ・noopener noreferrer。ニュースハブの中にリーダーの道は無い", async ({ page, context }) => {
    await context.route("https://news.example.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>元のページ</title><h1>元の記事</h1>" }));
    const state = await open(page);
    const link = page.getByTestId("onair-title").getByRole("link");
    await expect(link).toHaveAttribute("href", ITEMS[0].url!);
    await expect(link).toHaveAttribute("target", "_blank");
    expect(await link.getAttribute("rel")).toMatch(/noopener/);
    expect(await link.getAttribute("rel")).toMatch(/noreferrer/);
    const popup = context.waitForEvent("page");
    await link.click();
    const opened = await popup;
    await expect(opened).toHaveURL(ITEMS[0].url!);
    await opened.close();
    await expect(page).toHaveURL(/\/$/);      // 元のタブは動かない
    expect(state.calls.some((c) => c.path === "/news/items/1/action" && (c.body as { action: string }).action === "open")).toBe(true);
    // すべての記事のリンクが外へ（リーダーの /news/read は無い）
    const hrefs = await page.locator("[data-news-card] a, [data-testid=onair-title] a").evaluateAll((els) => els.map((e) => (e as HTMLAnchorElement).getAttribute("href")));
    expect(hrefs.length).toBeGreaterThan(0);
    for (const h of hrefs.filter((x) => !x?.startsWith("/entities/") && !x?.startsWith("/events/"))) expect(h).toMatch(/^https:\/\//);      // 物の札・イベントの「ほか N 件」だけは画面の中
  });
});

test.describe("新着・アーカイブ・検索・好み", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("新着: チャンネルで絞る・隠す（元に戻せる）・検索", async ({ page }) => {
    const state = await open(page);
    await page.getByRole("tab", { name: "新着" }).click();
    await expect(page.locator("[data-news-card]")).toHaveCount(6);
    await page.getByRole("group", { name: "チャンネル" }).getByRole("button", { name: /インフラ/ }).click();
    await page.getByRole("button", { name: "全チャンネル" }).click();
    const card = page.locator("[data-news-card]", { hasText: "NAS で動かす" });
    await card.getByRole("button", { name: "隠す" }).click();
    await expect(page.getByText("隠しました")).toBeVisible();
    await expect(card).toHaveCount(0);
    expect(state.calls.some((c) => c.path === "/news/items/6/action" && JSON.stringify(c.body) === JSON.stringify({ action: "hide", on: true }))).toBe(true);
    await page.getByRole("button", { name: "元に戻す" }).click();
    await expect(page.locator("[data-news-card]", { hasText: "NAS で動かす" })).toHaveCount(1);
    // 👍・☆
    const first = page.locator("[data-news-card]").first();
    await first.getByRole("button", { name: /もっと/ }).click();
    await first.getByRole("button", { name: /保存/ }).click();
    expect(state.calls.filter((c) => /action$/.test(c.path)).map((c) => (c.body as { action: string }).action)).toEqual(expect.arrayContaining(["hide", "like", "star"]));
    // 検索
    await page.getByRole("button", { name: "検索を開く" }).click();
    await page.getByRole("searchbox", { name: "アーカイブ全体を検索" }).fill("GGUF");
    await expect(page.getByText("「GGUF」の検索結果")).toBeVisible();
    await expect(page.locator("[data-news-card]")).toHaveCount(1);
    await shot(page, "news-search-390");
  });

  test("アーカイブ: 日付の一覧から日の記事へ", async ({ page }) => {
    await open(page);
    await page.getByRole("tab", { name: "アーカイブ" }).click();
    await expect(page.getByText("保存した記事を見る")).toBeVisible();
    await page.locator(".nw-archive-days button").first().click();
    await expect(page.locator("[data-news-card]").first()).toBeVisible();
    await shot(page, "news-archive-390");
  });

  test("話題のボタンで話題の記事へ。興味の語を外せる", async ({ page }) => {
    await open(page);
    await page.getByRole("tab", { name: "新着" }).click();
    await page.locator("[data-news-card]").first().getByRole("button", { name: "llama.cpp" }).click();
    await expect(page.getByText("話題「llama.cpp」")).toBeVisible();
    await page.getByRole("button", { name: /話題の絞り込みを外す/ }).click();
    await expect(page.getByText("話題「llama.cpp」")).toHaveCount(0);
  });

  test("取り直すボタン", async ({ page }) => {
    const state = await open(page);
    await page.getByRole("button", { name: "取り直す" }).click();
    await expect(page.getByText(/取り直しました（新着 2 件）/)).toBeVisible();
    expect(state.calls.some((c) => c.method === "POST" && c.path === "/news/refresh")).toBe(true);
  });
});

test.describe("5 つのデザインがどれも開く", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
  for (const design of ["broadcast", "magazine", "newspaper", "finance", "text"] as const) {
    test(design, async ({ page }) => {
      const state = newState();
      state.prefs.display.design = design;
      await mockApi(page, state);
      await page.goto("/");
      await expect(page.getByTestId("news-root")).toHaveAttribute("data-design", design);
      await expect(page.getByText("放送を読み込めませんでした")).toHaveCount(0);
      await expect(page.locator("a[href^='https://news.example.test']").first()).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await shot(page, `design-${design}-390`);
    });
  }
});
