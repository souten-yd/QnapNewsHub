/** ControlDeck とつなぐ画面（設定）と、診断の「つないだ ControlDeck」・「AI 生成のイメージ」の印の E2E。
 *  目的: つなぐ番号（6 桁・残り時間）とニュースハブの公開鍵の指紋・つないだ相手の一覧（最後に来た時刻）・取り消しの確認・受け取った鍵の名前（値は出ない）・仕事の数が、
 *  320 と 1280・ライト／ダークではみ出さず、管理者の操作が API へ正しく送られること。
 *  根拠: 2026-11-24 利用者の構想（ControlDeck との連携）、docs/DESIGN.md 3.1、AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test } from "@playwright/test";
import { ITEMS, LINK_PEERS, mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;
const SECRET = "sk-or-v1-" + "z".repeat(24);

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`つなぐ ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("番号を出す → 指紋と番号と残り時間 → つながると一覧に出る", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/settings");
        const card = page.locator("section", { has: page.locator("#link-controldeck") });
        await card.scrollIntoViewIfNeeded();
        await expect(card.getByTestId("link-fingerprint")).toHaveText("1a2b-3c4d-5e6f-7a8b-9c0d");
        await expect(card.getByTestId("link-empty")).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await shot(page, `link-empty-${vp.name}-${scheme}`);
        await card.getByRole("button", { name: "つなぐ番号を出す" }).click();
        await expect(card.getByTestId("link-code")).toHaveText("482913");
        await expect(card.getByTestId("link-remaining")).toContainText("あと 09:5");
        await expect(card.getByTestId("link-fingerprint")).toBeVisible();      // 番号と指紋が同時に見える
        expect(state.calls.find((c) => c.method === "POST" && c.path === "/link/pairing")).toBeTruthy();
        await expectNoHorizontalOverflow(page);
        await shot(page, `link-code-${vp.name}-${scheme}`);
        // ControlDeck がつないだ（番号は使われ、相手が一覧に出る）。画面は 3 秒ごとに読む
        state.link.pairing = null;
        state.link.peers = [LINK_PEERS[0]];
        await expect(card.getByTestId("link-peers")).toContainText("見本の ControlDeck", { timeout: 9000 });
        await expect(card.getByTestId("link-code")).toHaveCount(0);
        await expect(card.getByTestId("link-peers")).toContainText("最後に来た時刻");
      });

      test("LAN の URL の候補が写せる形で出る（127.0.0.1 待ち受けなら出ない）", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/settings");
        const card = page.locator("section", { has: page.locator("#link-controldeck") });
        await card.scrollIntoViewIfNeeded();
        await expect(card.getByTestId("link-lan-url")).toHaveText(["http://192.0.2.10:8787", "http://198.51.100.7:8787"]);
        await expect(card.getByTestId("link-lan-note")).toContainText("自動で答えます");
        await expectNoHorizontalOverflow(page);
        await shot(page, `link-lan-${vp.name}-${scheme}`);
        state.link.lan = { lan: false, discovery_port: 8788, answering: false, urls: [] };
        await page.reload();
        await card.scrollIntoViewIfNeeded();
        await expect(card.getByTestId("link-lan-none")).toBeVisible();
        await expect(card.getByTestId("link-lan-url")).toHaveCount(0);
      });

      test("つないだ相手の一覧・受け取った鍵（値は出ない）・仕事の数・取り消しの確認", async ({ page }) => {
        const state = newState({ link: { fingerprint: "1a2b-3c4d-5e6f-7a8b-9c0d", peers: structuredClone(LINK_PEERS), pairing: null, allowed_secrets: ["openrouter"],
          secrets: [{ name: "openrouter", received_at: new Date().toISOString(), peer_id: 2, peer: "見本の ControlDeck", present: true }],
          jobs: { queued: 4, leased: 2, expired: 1, dead: 0, done_24h: 37, queued_by_kind: { image: 4 } }, lan: { lan: false, discovery_port: 8788, answering: false, urls: [] } } });
        await mockApi(page, state);
        await page.goto("/settings");
        const card = page.locator("section", { has: page.locator("#link-controldeck") });
        await card.scrollIntoViewIfNeeded();
        await expect(card.getByTestId("link-peers").locator("li")).toHaveCount(2);
        await expect(card.getByTestId("link-peers")).toContainText("まだ来ていません");
        await expect(card.getByTestId("link-secrets")).toContainText("OpenRouter の鍵");
        expect(await card.innerText()).not.toContain(SECRET);
        await expect(card.getByTestId("link-jobs")).toContainText("借りている 2");
        await expect(card.getByTestId("link-jobs")).toContainText("期限切れ 1");
        await expectNoHorizontalOverflow(page);
        await shot(page, `link-peers-${vp.name}-${scheme}`);
        await card.getByRole("button", { name: "取り消す" }).first().click();
        const dlg = page.getByTestId("link-confirm");
        await expect(dlg).toContainText("取り消しますか");
        await shot(page, `link-confirm-${vp.name}-${scheme}`);
        expect(state.calls.some((c) => c.method === "DELETE" && c.path.startsWith("/link/peers"))).toBe(false);      // 確認の前には送らない
        await dlg.getByRole("button", { name: "やめる" }).click();
        await card.getByRole("button", { name: "取り消す" }).first().click();
        await page.getByTestId("link-confirm").getByRole("button", { name: "取り消す" }).click();
        await expect.poll(() => state.calls.some((c) => c.method === "DELETE" && c.path === "/link/peers/2")).toBe(true);
        await expect(card.getByTestId("link-peers").locator("li")).toHaveCount(1);
        expect(state.unhandled).toEqual([]);
      });

      test("診断の「つないだ ControlDeck」と、AI 生成の画像の印", async ({ page }) => {
        const state = newState({ items: [{ ...ITEMS[0], image_url: "https://img.example.test/1.png", image_ai: true }, ...ITEMS.slice(1)],
          link: { fingerprint: "1a2b-3c4d-5e6f-7a8b-9c0d", peers: structuredClone(LINK_PEERS), pairing: null, allowed_secrets: ["openrouter"],
            secrets: [{ name: "openrouter", received_at: new Date().toISOString(), peer_id: 2, peer: "見本", present: true }],
            jobs: { queued: 0, leased: 2, expired: 1, dead: 0, done_24h: 5, queued_by_kind: {} }, lan: { lan: false, discovery_port: 8788, answering: false, urls: [] } } });
        await mockApi(page, state);
        await page.goto("/diagnostics");
        const diag = page.getByTestId("diag-link");
        await diag.scrollIntoViewIfNeeded();
        await expect(diag.getByTestId("link-leased")).toContainText("2 件");
        await expect(diag.getByTestId("link-expired")).toContainText("1 件");
        await expect(diag).toContainText("openrouter");
        await expectNoHorizontalOverflow(page);
        await shot(page, `link-diag-${vp.name}-${scheme}`);
        await page.goto("/");
        await expect(page.getByTestId("ai-image").first()).toBeVisible();
        await expect(page.getByTestId("ai-image").first()).toHaveText("AI 生成のイメージ");
        await expectNoHorizontalOverflow(page);
        await shot(page, `link-ai-image-${vp.name}-${scheme}`);
      });
    });
  }
}
