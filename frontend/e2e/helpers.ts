import { expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import path from "node:path";

export const VIEWPORTS = [
  { name: "320", width: 320, height: 700 },
  { name: "390", width: 390, height: 844 },
  { name: "1280", width: 1280, height: 800 },
] as const;
export const SCHEMES = ["light", "dark"] as const;

/** 撮影。test-results/shots/ と、NEWSHUB_SHOTS_DIR（あれば）の両方へ写す。 */
export async function shot(page: Page, name: string): Promise<void> {
  const file = `${name}.png`;
  const dirs = [path.join("test-results", "shots"), process.env.NEWSHUB_SHOTS_DIR ?? ""].filter(Boolean);
  for (const dir of dirs) {
    mkdirSync(dir, { recursive: true });
    await page.screenshot({ path: path.join(dir, file), fullPage: false });
  }
}

/** 横にはみ出していないこと（320px で崩れていないか）。 */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const over = await page.evaluate(() => {
    const doc = document.documentElement;
    const main = document.querySelector("[data-shell-main]") as HTMLElement | null;
    return { page: doc.scrollWidth - doc.clientWidth, main: main ? main.scrollWidth - main.clientWidth : 0 };
  });
  expect(over.page, "ページが横にはみ出している").toBeLessThanOrEqual(1);
  expect(over.main, "main が横にはみ出している").toBeLessThanOrEqual(1);
}

export const isDark = (page: Page) => page.evaluate(() => document.documentElement.classList.contains("dark"));
