/** 更新の画面。E2E の目的: 設定の「アプリの更新」（新しい版あり／なし・ノートの安全な表示・確認の画面・進み具合・結果・Linux の手順・お休み中）、
 *  ホームの一行の知らせ・☰ の印・診断の更新の状態が、320 と 1280・ライト／ダークではみ出さず、管理者の操作が API へ正しく送られること。
 *  根拠: 2026-11-24 利用者の構想（更新の機能）、docs/UPDATE.md、AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test } from "@playwright/test";
import { UPDATE_BASE, UPDATE_LATEST, mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;
const available = () => newState({ update: { ...structuredClone(UPDATE_BASE), latest: UPDATE_LATEST, update_available: true, rollback: "0.1.0" } });

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`更新 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("新しい版あり: ノートは安全に表示され、確認の画面を経て更新が始まる", async ({ page }) => {
        const state = available();
        await mockApi(page, state);
        await page.goto("/settings");
        const card = page.locator("section", { has: page.locator("#app-update") });
        await card.scrollIntoViewIfNeeded();
        await expect(card.getByTestId("update-current")).toHaveText("0.1.1");
        await expect(card.getByTestId("update-latest")).toContainText("0.2.0");
        const notes = card.getByTestId("release-notes");
        await expect(notes).toContainText("更新の機能");
        await expect(notes.locator("script, img")).toHaveCount(0);                          // HTML は無効（文字として出る）
        await expect(notes).toContainText("<script>");
        expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
        await expect(notes.locator("a")).toHaveCount(1);                                      // https のリンクだけ。javascript: は出ない
        await expect(notes.locator("a")).toHaveAttribute("rel", /noopener/);
        await expectNoHorizontalOverflow(page);
        await shot(page, `update-card-${vp.name}-${scheme}`);
        await card.getByRole("button", { name: "更新する" }).click();
        const dlg = page.getByTestId("update-confirm");
        await expect(dlg).toContainText("0.2.0 に更新しますか");
        await expect(dlg).toContainText("データは残ります");
        await shot(page, `update-confirm-${vp.name}-${scheme}`);
        expect(state.calls.some((c) => c.path === "/update/install")).toBe(false);          // 確認の前には送らない
        await dlg.getByRole("button", { name: "やめる" }).click();
        await expect(dlg).toHaveCount(0);
        await card.getByRole("button", { name: "更新する" }).click();
        await page.getByTestId("update-confirm").getByRole("button", { name: "更新する" }).click();
        const call = state.calls.find((c) => c.path === "/update/install");
        expect(call?.method).toBe("POST");
        expect(call?.body).toEqual({ version: "0.2.0" });
        await expect(card.getByTestId("update-progress")).toContainText("取得しています");
        await expect(card.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
        await expectNoHorizontalOverflow(page);
        await shot(page, `update-progress-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("新しい版なし・試験版の切り替え・今すぐ確認", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/settings");
        const card = page.locator("section", { has: page.locator("#app-update") });
        await card.scrollIntoViewIfNeeded();
        await expect(card.getByTestId("update-latest")).toContainText("最新です");
        await expect(card.getByRole("button", { name: "更新する" })).toHaveCount(0);
        await card.getByText("試験版（prerelease）も対象にする").click();
        await expect.poll(() => state.calls.find((c) => c.path === "/update/settings")?.body).toEqual({ include_prerelease: true });
        await card.getByRole("button", { name: "今すぐ確認" }).click();
        await expect.poll(() => state.calls.some((c) => c.method === "POST" && c.path === "/update/check")).toBe(true);
        await expectNoHorizontalOverflow(page);
        await shot(page, `update-none-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("前回の結果（成功・失敗とログ）と、お休み中・Linux の表示", async ({ page }) => {
        const ok = newState({ update: { ...structuredClone(UPDATE_BASE), last: { kind: "update", from: "0.1.0", to: "0.1.1", rc: 10, at: new Date().toISOString(), ok: true, message: "更新しました（0.1.0 → 0.1.1）", log: "" } } });
        await mockApi(page, ok);
        await page.goto("/settings");
        await expect(page.getByTestId("update-last")).toContainText("更新しました");
        await page.getByTestId("update-last").scrollIntoViewIfNeeded();
        await shot(page, `update-result-ok-${vp.name}-${scheme}`);

        const ng = newState({ update: { ...structuredClone(UPDATE_BASE), last: { kind: "update", from: "0.1.0", to: "0.1.1", rc: 3, at: new Date().toISOString(), ok: false, message: "更新に失敗しました（インストーラーの終了コード 3）", log: "ERROR: disk full" } } });
        const p2 = await page.context().newPage();
        await mockApi(p2, ng);
        await p2.goto("/settings");
        await expect(p2.getByTestId("update-last")).toContainText("失敗しました");
        await expect(p2.getByTestId("update-log")).toContainText("disk full");
        await p2.getByTestId("update-last").scrollIntoViewIfNeeded();
        await expectNoHorizontalOverflow(p2);
        await shot(p2, `update-result-ng-${vp.name}-${scheme}`);

        const rest = available();
        rest.update.rest = { active: true, ends_at: "2026-11-25T06:00:00" };
        const p3 = await page.context().newPage();
        await mockApi(p3, rest);
        await p3.goto("/settings");
        await expect(p3.getByTestId("update-rest")).toContainText("06:00");
        await expect(p3.getByRole("button", { name: "更新する" })).toBeDisabled();

        const linux = newState({ update: { ...structuredClone(UPDATE_BASE), install: "linux", can_install: false, latest: UPDATE_LATEST, update_available: true, manual_steps: ["./hub.sh setup && ./hub.sh build"] } });
        const p4 = await page.context().newPage();
        await mockApi(p4, linux);
        await p4.goto("/settings");
        await expect(p4.getByTestId("update-manual")).toContainText("自動では入れません");
        await expect(p4.getByRole("button", { name: "更新する" })).toHaveCount(0);
        await p4.getByTestId("update-manual").scrollIntoViewIfNeeded();
        await shot(p4, `update-linux-${vp.name}-${scheme}`);
      });

      test("ホームの一行の知らせと ☰ の印、診断の更新", async ({ page }) => {
        await mockApi(page, available());
        await page.goto("/");
        await expect(page.getByTestId("update-notice")).toContainText("新しい版 0.2.0");
        await expectNoHorizontalOverflow(page);
        await shot(page, `home-update-notice-${vp.name}-${scheme}`);
        if (vp.width < 768) {
          await expect(page.getByTestId("update-mark-menu")).toBeVisible();
          await page.getByRole("button", { name: "メニューを開く" }).click();
        }
        await expect(page.locator('[data-testid="update-mark"]:visible').first()).toBeVisible();
        await shot(page, `menu-update-mark-${vp.name}-${scheme}`);
        await page.goto("/diagnostics");
        await expect(page.getByTestId("update-version")).toContainText("0.1.1");
        await expect(page.getByTestId("update-latest-diag")).toContainText("0.2.0");
        await expectNoHorizontalOverflow(page);
        const quiet = await page.context().newPage();
        await mockApi(quiet, newState());
        await quiet.goto("/");
        await expect(quiet.getByTestId("news-root")).toBeVisible();
        await expect(quiet.getByTestId("update-notice")).toHaveCount(0);
      });
    });
  }
}
