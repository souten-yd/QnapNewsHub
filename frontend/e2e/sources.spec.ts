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

      test("次の取得の予定・いまの間隔（自動）・出どころの種類が出る", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        const row = page.getByTestId("news-src-row").first();
        await expect(row.getByTestId("news-src-next")).toContainText("分後");
        await expect(row.getByTestId("news-src-poll")).toContainText("自動");
        await expect(row.getByTestId("news-src-origin")).toHaveText("報道");
        await expectNoHorizontalOverflow(page);
        await row.getByTestId("news-src-next").scrollIntoViewIfNeeded();
        await shot(page, `sources-next-${vp.name}-${scheme}`);
      });

      test("CSV の取り込み: 確認の表（追加できる・重複・使えない）→ そのまま追加（無効で）", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        await page.getByTestId("news-tab-io").click();
        await page.getByTestId("news-import-file").setInputFiles({ name: "list.csv", mimeType: "text/csv", buffer: Buffer.from("name,url,category\n見本,https://import1.example.test/rss,ニュース\n") });
        await expect(page.getByTestId("news-import-counts")).toContainText("追加できる 2 件 · 重複 1 件 · 使えない 1 件");
        await expect(page.getByTestId("news-import-row")).toHaveCount(4);
        await expect(page.getByTestId("news-import-row").nth(2)).toHaveAttribute("data-status", "duplicate");
        await expectNoHorizontalOverflow(page);
        await shot(page, `import-csv-preview-${vp.name}-${scheme}`);
        await page.getByTestId("news-import-mode-direct").check();
        await page.getByTestId("news-import-run").click();
        await expect(page.getByTestId("news-import-result")).toContainText("2 件を足しました（無効で");
        const commit = state.calls.find((c) => c.path.includes("/import/commit"));
        expect((commit?.body as { mode: string }).mode).toBe("direct");
        await shot(page, `import-csv-done-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("OPML の取り込み: 確認の表を出し、通信確認してから追加を選べる。書き出しのリンクがある", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/sources");
        await page.getByTestId("news-tab-io").click();
        await page.getByTestId("news-import-file").setInputFiles({ name: "feeds.opml", mimeType: "text/x-opml", buffer: Buffer.from('<opml version="2.0"><body><outline xmlUrl="https://import1.example.test/rss" text="見本"/></body></opml>') });
        await expect(page.getByTestId("news-import-preview")).toBeVisible();
        await expect(page.getByTestId("news-import-mode-check")).toBeChecked();
        await page.getByTestId("news-import-row").first().getByRole("checkbox").uncheck();
        await expect(page.getByTestId("news-import-run")).toHaveText("1 件を追加");
        await expect(page.getByTestId("news-export-opml")).toHaveAttribute("href", /export\?format=opml/);
        await expect(page.getByTestId("news-export-csv")).toHaveAttribute("href", /export\?format=csv/);
        await expectNoHorizontalOverflow(page);
        await shot(page, `import-opml-preview-${vp.name}-${scheme}`);
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

  test("詳細で間隔を固定・出どころの種類を変えられ、API へ送られる", async ({ page }) => {
    const state = newState();
    await mockApi(page, state);
    await page.goto("/sources");
    await page.getByText("見本の取得先 1").first().click();
    await expect(page.getByTestId("news-detail-poll")).toContainText("304（変わりなし）の当たり 80%");
    await page.getByTestId("news-fixed").click();
    await page.getByTestId("news-origin").selectOption("blog");
    const sent = state.calls.filter((c) => c.path.includes("/manage/bulk")).map((c) => c.body as { action: string; origin?: string });
    expect(sent.map((b) => b.action)).toEqual(["fixed", "origin"]);
    expect(sent[1].origin).toBe("blog");
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
