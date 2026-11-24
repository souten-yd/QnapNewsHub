// アイコンの PNG を SVG（frontend/icons/*.svg）から作る。./hub.sh icons から呼ぶ。
// 手元の道具が無くても作れるよう、Playwright のブラウザ（PLAYWRIGHT_BROWSERS_PATH）で SVG を描いて書き出す（install はしない）。
import { chromium } from "@playwright/test";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = (name) => readFileSync(path.join(root, "icons", name), "utf8");
const pub = path.join(root, "public");
const qpkg = path.resolve(root, "..", "packaging", "qpkg", "icons");
mkdirSync(pub, { recursive: true });
mkdirSync(qpkg, { recursive: true });

// [元の SVG, 出力先, 1 辺の大きさ]
const jobs = [
  ["icon.svg", path.join(pub, "favicon-16.png"), 16],
  ["icon.svg", path.join(pub, "favicon-32.png"), 32],
  ["icon-fullbleed.svg", path.join(pub, "apple-touch-icon.png"), 180],
  ["icon.svg", path.join(pub, "icon-192.png"), 192],
  ["icon.svg", path.join(pub, "icon-512.png"), 512],
  ["icon-maskable.svg", path.join(pub, "icon-maskable-512.png"), 512],
  ["icon.svg", path.join(qpkg, "icon-64.png"), 64],
  ["icon.svg", path.join(qpkg, "icon-80.png"), 80],
  ["icon-gray.svg", path.join(qpkg, "icon-64-gray.png"), 64],
  ["icon-gray.svg", path.join(qpkg, "icon-80-gray.png"), 80],
];

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ deviceScaleFactor: 1 });
  for (const [name, out, size] of jobs) {
    await page.setViewportSize({ width: size, height: size });
    await page.setContent(`<style>html,body{margin:0;background:transparent}svg{display:block;width:${size}px;height:${size}px}</style>${src(name)}`);
    await page.screenshot({ path: out, omitBackground: true, clip: { x: 0, y: 0, width: size, height: size } });
    console.log(`${path.relative(path.resolve(root, ".."), out)} (${size}px)`);
  }
} finally {
  await browser.close();
}
copyFileSync(path.join(root, "icons", "icon.svg"), path.join(pub, "favicon.svg"));
