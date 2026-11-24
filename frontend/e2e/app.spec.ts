/** アプリとアイコン: manifest・アイコン・iOS のホーム画面用の meta が配られること。アイコンの一覧を 1 枚に撮る。 */
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { shot } from "./helpers";

const ICONS = ["favicon-16.png", "favicon-32.png", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png"] as const;
const SIZES: Record<string, number> = { "favicon-16.png": 16, "favicon-32.png": 32, "apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512 };

test("manifest が配られ、名前・standalone・色・アイコンが揃っている。アイコンは本物の PNG", async ({ request }) => {
  const res = await request.get("/manifest.webmanifest");
  expect(res.ok()).toBe(true);
  const manifest = await res.json();
  expect(manifest).toMatchObject({ name: "ニュースハブ", display: "standalone", start_url: "/" });
  expect(manifest.short_name).toBeTruthy();
  expect(manifest.theme_color).toMatch(/^#[0-9a-f]{6}$/i);
  expect(manifest.background_color).toMatch(/^#[0-9a-f]{6}$/i);
  const purposes = manifest.icons.map((i: { purpose: string; sizes: string }) => `${i.purpose}:${i.sizes}`);
  expect(purposes).toEqual(expect.arrayContaining(["any:192x192", "any:512x512", "maskable:512x512"]));
  for (const icon of manifest.icons as { src: string; type: string }[]) {
    const r = await request.get(icon.src);
    expect(r.ok(), icon.src).toBe(true);
    expect(r.headers()["content-type"]).toContain(icon.type);
  }
  for (const name of ICONS) {
    const r = await request.get(`/${name}`);
    expect(r.ok(), name).toBe(true);
    const body = await r.body();
    expect([...body.subarray(0, 4)]).toEqual([0x89, 0x50, 0x4e, 0x47]);      // PNG の印
    expect(body.readUInt32BE(16), name).toBe(SIZES[name]);      // 幅
    expect(body.readUInt32BE(20), name).toBe(SIZES[name]);      // 高さ
  }
});

test("index.html に iOS のホーム画面用の指定がある（全画面・Safe Area・アイコン）", async ({ page }) => {
  await page.goto("/");
  const meta = (name: string) => page.locator(`head meta[name="${name}"]`).getAttribute("content");
  expect(await meta("viewport")).toContain("viewport-fit=cover");
  expect(await meta("apple-mobile-web-app-capable")).toBe("yes");
  expect(await meta("apple-mobile-web-app-status-bar-style")).toBeTruthy();
  expect(await meta("apple-mobile-web-app-title")).toBe("ニュースハブ");
  await expect(page.locator('head link[rel="apple-touch-icon"]')).toHaveAttribute("href", "/apple-touch-icon.png");
  await expect(page.locator('head link[rel="manifest"]')).toHaveAttribute("href", "/manifest.webmanifest");
  await expect(page).toHaveTitle("ニュースハブ");
});

test("上の Safe Area の余白が殻に効く（ノッチのある端末）", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => document.documentElement.style.setProperty("--cd-safe-area-top", "47px")));
  await page.route("**/api/v1/auth/status", (r) => r.fulfill({ contentType: "application/json", body: JSON.stringify({ setup_needed: false, authenticated: true, username: "admin" }) }));
  await page.route("**/api/v1/news/**", (r) => r.fulfill({ contentType: "application/json", body: "{}" }));
  await page.goto("/");
  const bar = page.locator("[data-shell-topbar]");
  await expect(bar).toBeVisible();
  const top = await bar.evaluate((el) => parseFloat(getComputedStyle(el).paddingTop));
  expect(top).toBe(47);
});

test("アイコンの一覧を 1 枚に撮る（QPKG 用も含む）", async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 520 });
  const figure = (name: string, src: string, big: boolean) => `<figure><img src="${src}" style="${big ? "width:160px" : ""}"><figcaption>${name}</figcaption></figure>`;
  const qpkg = ["icon-64.png", "icon-80.png", "icon-64-gray.png", "icon-80-gray.png"].map((n) =>
    figure(`qpkg/${n}`, `data:image/png;base64,${readFileSync(path.join("..", "packaging", "qpkg", "icons", n)).toString("base64")}`, false));
  await page.goto("/");
  const pub = ICONS.map((n) => figure(n, `/${n}`, n === "icon-512.png" || n === "icon-maskable-512.png"));
  await page.setContent(`<style>body{margin:16px;font:12px sans-serif;background:#888;color:#000;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end}figure{margin:0;text-align:center}</style>${[...pub, ...qpkg].join("")}`);
  await expect.poll(() => page.evaluate(() => [...document.images].every((i) => i.complete && i.naturalWidth > 0))).toBe(true);
  await shot(page, "icons-sheet");
});
