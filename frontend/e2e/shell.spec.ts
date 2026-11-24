/** 画面の殻: ☰ のドロワー（携帯）・折りたためるサイドバー（PC）・下部ナビが無いこと・ダーク / ライト・名前（ロゴは文字だけ）。 */
import { expect, test } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, VIEWPORTS, expectNoHorizontalOverflow, isDark, shot } from "./helpers";

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    test.describe(`殻 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("ニュースが開き、名前が文字で出て、はみ出さない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/");
        await expect(page.getByTestId("news-root")).toBeVisible();
        await expect(page.locator('[data-testid="brand"]:visible').first()).toHaveText("ニュースハブ");
        expect(await isDark(page)).toBe(scheme === "dark");
        await expect(page.locator("img[alt='ニュースハブ'], header img, aside img")).toHaveCount(0);      // ロゴは文字だけ
        await expectNoHorizontalOverflow(page);
        await shot(page, `shell-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}

test.describe("携帯の ☰ ドロワー", () => {
  test.use({ viewport: { width: 320, height: 700 }, hasTouch: true });

  test("☰ で左から出て、4 つの行き先があり、選ぶと閉じる。下部ナビは無い", async ({ page }) => {
    await mockApi(page, newState());
    await page.goto("/");
    const drawer = page.getByTestId("nav-drawer");
    await expect(drawer).toHaveAttribute("inert", "");
    const menu = page.getByRole("button", { name: "メニューを開く" });
    await menu.click();
    await expect(drawer).not.toHaveAttribute("inert", "");
    await expect.poll(async () => (await drawer.boundingBox())?.x ?? -999).toBeGreaterThanOrEqual(-1);      // 左から出ている（動きが終わるまで待つ）
    const links = drawer.getByRole("link");
    await expect(links).toHaveText(["ニュース", "取得先の管理", "診断", "設定"]);
    await shot(page, "drawer-320");
    // 画面の下端に固定のナビが無い
    const bottomFixed = await page.evaluate(() => [...document.querySelectorAll("nav, footer")].filter((el) => {
      const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
      return (cs.position === "fixed" || cs.position === "sticky") && r.bottom >= window.innerHeight - 2 && r.height > 0 && r.top > window.innerHeight / 2 && !el.closest("[data-testid=nav-drawer]");
    }).length);
    expect(bottomFixed).toBe(0);
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveAttribute("inert", "");
    await menu.click();
    await drawer.getByRole("link", { name: "設定" }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await expect(drawer).toHaveAttribute("inert", "");
    await expect(page.getByRole("heading", { name: "設定", level: 1 })).toBeVisible();
  });

  test("テーマを切り替えるとダークになり、読み直しても覚えている", async ({ page }) => {
    await mockApi(page, newState());
    await page.goto("/");
    await page.getByRole("button", { name: "メニューを開く" }).click();
    await page.getByRole("radio", { name: "ダーク" }).click();
    expect(await isDark(page)).toBe(true);
    await page.reload();
    expect(await isDark(page)).toBe(true);
    await page.getByRole("button", { name: "メニューを開く" }).click();
    await page.getByRole("radio", { name: "ライト" }).click();
    expect(await isDark(page)).toBe(false);
  });
});

test.describe("PC のサイドバー", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("☰ で折りたたみ・開き、読み直しても覚えている。携帯の上の行は出ない", async ({ page }) => {
    await mockApi(page, newState());
    await page.goto("/");
    const side = page.locator("[data-shell-sidebar]");
    await expect(side).toBeVisible();
    await expect(page.locator("[data-shell-topbar]")).toBeHidden();
    expect((await side.boundingBox())!.width).toBeGreaterThan(200);
    await page.getByRole("button", { name: "サイドバーを閉じる" }).click();
    await expect.poll(async () => (await side.boundingBox())!.width).toBeLessThan(80);
    await shot(page, "sidebar-collapsed-1280");
    await page.reload();
    await expect.poll(async () => (await side.boundingBox())!.width).toBeLessThan(80);
    await page.getByRole("button", { name: "サイドバーを開く" }).click();
    await expect.poll(async () => (await side.boundingBox())!.width).toBeGreaterThan(200);
    await side.getByRole("link", { name: "取得先の管理" }).click();
    await expect(page).toHaveURL(/\/sources$/);
  });
});
