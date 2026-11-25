/** Things の脳（2026-11-25）: カードの帯・関係の型ごと・未解決の問い・主な数値・つなぐ候補・全体の地図・検索の強調と絞り・ローカル・主題の入口（MOC）・Obsidian の書き出し。
 *  E2E の目的（v0.5 の分も含む）: ①カードに概要（AI の印）・関係（推測の印）・変化と矛盾・主張の根拠が出て、利用者のメモが打つだけで保存される ②地図が遅延読み込みで描かれ、始める物から段階的に広がり、型・確かさ・期間で絞れ、
 *  点を押すとカードへ行ける ③書き出しは押したときだけ zip を受け取り、機密の区分の既定は含めない。320px と 1280px・ライト／ダークではみ出さない。
 *  銀河の見た目（2026-11-25）: 半径の幅の上限と比・孤立した点は既定で出ない・線の既定の色が 1 色・左上の設定の枠（色の分け方・型の保存）を確かめる。
 *  根拠: 利用者の指示 v0.5（知識カード・知識の地図・Obsidian の書き出し）・AGENTS.md「実画面（320px と PC の幅、ライト・ダーク）」。 */
import { expect, test, type Page } from "@playwright/test";
import { mockApi, newState } from "./mock";
import { SCHEMES, expectNoHorizontalOverflow, shot } from "./helpers";

const SIZES = [{ name: "320", width: 320, height: 700 }, { name: "1280", width: 1280, height: 900 }] as const;
const tap = (page: Page, id: number) => page.evaluate((i) => (document.querySelector("[data-testid=map-canvas]") as unknown as { __tap(n: number): void }).__tap(i), id);
/** 試験用の旗の数（hit 一致・dim 薄く・off 隠す・big いつも名前を出す点・sizeMax/sizeMin 点の大きさの幅）。 */
const stat = (page: Page, key: "hit" | "dim" | "off" | "big" | "sizeMax" | "sizeMin" | "edgeColors" | "isoShown" | "glowNodes") => page.evaluate((k) => (document.querySelector("[data-testid=map-canvas]") as unknown as { __stat(): Record<string, number> }).__stat()[k], key);

for (const vp of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`Things の脳 ${vp.name}px ${scheme}`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, hasTouch: vp.width < 768 });

      test("カード: 帯・関係の型ごと・問い・数値・つなぐ候補・変化と矛盾・根拠・メモ（打つとすぐ保存）", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities/2");
        await expect(page.getByTestId("knowledge-card")).toBeVisible();
        // 先頭の帯: 1 行の定義・種類・状態・確かさ・別名・根拠の数と新しさ・最後に確かめた日
        const band = page.getByTestId("card-brief");
        await expect(page.getByTestId("brief-definition")).toContainText("Alibaba が公開する汎用の大規模言語モデル");
        await expect(page.getByTestId("brief-state")).toHaveText("正式公開");
        await expect(page.getByTestId("brief-confidence")).toContainText("確かさ 0.97（高）");
        await expect(band).toContainText("通義千問 3.5");
        await expect(page.getByTestId("brief-evidence")).toContainText("根拠 17 件");
        await expect(page.getByTestId("brief-evidence")).toContainText("最後に確かめた日");
        await expect(page.getByTestId("card-overview")).toContainText("AI の要約");
        // 関係は型ごとの節（上位・使う・使われる）。畳める
        await expect(page.getByTestId("card-relations").locator("li")).toHaveCount(3);
        await expect(page.getByTestId("group-upper")).toContainText("Alibaba");
        await expect(page.getByTestId("group-upper").getByTestId("relation-definition")).toContainText("中国の IT 企業");
        await expect(page.getByTestId("group-used_by")).toContainText("llama.cpp");
        await expect(page.getByTestId("group-uses").locator('li[data-status="inferred"]')).toContainText("推測");
        await page.getByTestId("group-upper").locator("summary").click();
        await expect(page.getByTestId("group-upper").locator("li")).toBeHidden();
        await page.getByTestId("group-upper").locator("summary").click();
        // 未解決の問い・反対の見方・主な数値
        await expect(page.getByTestId("card-question")).toHaveCount(2);
        await expect(page.getByTestId("card-opposing")).toContainText("日本語に強い");
        await expect(page.getByTestId("card-metrics")).toContainText("25 tok/s");
        await expect(page.getByTestId("card-metrics")).toContainText("Metric");
        await expect(page.getByTestId("card-history").locator('li[data-mark="change"]')).toContainText("前は 1000");
        await expect(page.getByRole("heading", { name: /変化 1 · 矛盾 2/ })).toBeVisible();
        const claim = page.getByTestId("card-claims").locator("li").first();
        await claim.locator("summary").click();
        await expect(claim).toContainText("p.3");
        await expectNoHorizontalOverflow(page);
        await shot(page, `card-${vp.name}-${scheme}`);
        // つなぐ候補: 押すと確定（候補から消える）
        await expect(page.getByTestId("card-mentions").locator("li")).toHaveCount(2);
        await page.getByTestId("mention-confirm").first().click();
        await expect(page.getByTestId("card-mentions").locator("li")).toHaveCount(1);
        expect(state.calls.some((c) => c.method === "POST" && c.path === "/things/2/mentions/3/confirm")).toBe(true);
        await page.getByTestId("mention-dismiss").first().click();
        await expect(page.getByTestId("card-mentions")).toHaveCount(0);
        expect(state.calls.some((c) => c.path === "/things/2/mentions/4/dismiss")).toBe(true);
        const note = page.getByTestId("card-note");
        await note.fill("自分の考察: R9700 で試す");
        await expect(page.getByTestId("note-state")).toContainText("保存しました");
        expect(state.kgNote).toBe("自分の考察: R9700 で試す");
        await page.reload();
        await expect(page.getByTestId("card-note")).toHaveValue("自分の考察: R9700 で試す");
        await page.getByTestId("card-open-map").click();
        await expect(page).toHaveURL(/\/map\?focus=2$/);
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-view", "local");
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-nodes", "4");      // Qwen・Alibaba・llama.cpp・GGUF
        expect(state.unhandled).toEqual([]);
      });

      test("カードの定義・問いが無い Things は「まだ…」・関係が無ければ空の表示", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities/4");
        await expect(page.getByTestId("card-no-overview")).toBeVisible();
        await expect(page.getByTestId("brief-definition")).toContainText("まだありません");
        await expect(page.getByTestId("brief-state")).toHaveText("不明");
        await expect(page.getByTestId("card-no-relations")).toBeVisible();
        await expect(page.getByTestId("card-no-questions")).toBeVisible();
        await expect(page.getByTestId("card-no-history")).toBeVisible();
        await expectNoHorizontalOverflow(page);
        expect(state.unhandled).toEqual([]);
      });

      test("知識の地図: 最初は全体・検索は強調と絞り・ローカルは深さ 1〜3・絞り込みと凡例", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/map");
        const cv = page.getByTestId("map-canvas");
        await expect(cv).toHaveAttribute("data-ready", "true");
        await expect(cv).toHaveAttribute("data-view", "overview");
        await expect(cv).toHaveAttribute("data-nodes", "7");      // 検索なしで最初から全体（孤立した点は隠れている）
        await expect(cv).toHaveAttribute("data-edges", "7");
        await expect(page.getByTestId("map-count")).toContainText("関係のある Things 7 件のうち、主要な 7 点");
        expect(state.calls.filter((c) => c.path === "/things/map/overview").length).toBe(1);
        expect(state.calls.some((c) => c.path === "/graph/neighbors")).toBe(false);      // 全体はサーバーの 1 回で出る
        await expect(page.getByTestId("map-group")).toHaveCount(1);
        await page.getByTestId("map-settings-toggle").click();
        await expect(page.getByTestId("map-node-legend")).toContainText("ソフト");
        await expectNoHorizontalOverflow(page);
        await shot(page, `map-overview-${vp.name}-${scheme}`);
        // 点の大きさ＝重要度（重い点が大きく、軽い点が小さい）。ただし幅に上限がある（最大は最小の 4.5 倍以下）。孤立した点は既定で出ない。線の既定の色は 1 色（関係の型の色は触れた・選んだ点の線だけ）
        const smax = await stat(page, "sizeMax"), smin = await stat(page, "sizeMin");
        expect(smax).toBeGreaterThan(smin * 1.5);
        expect(smax).toBeLessThanOrEqual(smin * 4.5 + 1e-6);
        expect(await stat(page, "isoShown")).toBe(0);
        expect(await stat(page, "edgeColors")).toBe(1);
        expect(await stat(page, "glowNodes")).toBeGreaterThan(0);
        await page.getByTestId("map-iso").check();
        await expect(cv).toHaveAttribute("data-nodes", "8");      // 孤立した点の表示の切り替え
        await page.getByTestId("map-iso").uncheck();
        await expect(cv).toHaveAttribute("data-nodes", "7");
        // 設定の枠: 色の分け方の切り替え（既定はまとまりごと）・表示の型の保存と削除（端末に保存）
        await expect(page.getByTestId("map-color-cluster")).toHaveAttribute("aria-checked", "true");
        await page.getByTestId("map-color-kind").click();
        await expect(page.getByTestId("map-color-kind")).toHaveAttribute("aria-checked", "true");
        await page.getByTestId("map-color-cluster").click();
        await expect(page.getByTestId("map-preset").locator("option")).toHaveText(["型を選ぶ…", "銀河", "関係を読む"]);
        await page.getByTestId("map-preset-name").fill("試しの型");
        await page.getByTestId("map-preset-save").click();
        await expect(page.getByTestId("map-preset").locator("option")).toHaveCount(4);
        await page.getByTestId("map-preset").selectOption("関係を読む");
        await expect(page.getByTestId("map-color-kind")).toHaveAttribute("aria-checked", "true");
        await page.getByTestId("map-preset").selectOption("銀河");
        await expect(page.getByTestId("map-color-cluster")).toHaveAttribute("aria-checked", "true");
        await page.getByTestId("map-preset-delete").click();
        await expect(page.getByTestId("map-preset").locator("option")).toHaveCount(3);
        // 検索: 強調（一致は明るく・ほかは薄く）
        await page.getByTestId("map-search").fill("qwen");
        await expect(page.getByTestId("map-hits")).toContainText("一致 1 件・一致を強調");
        await expect(cv).toHaveAttribute("data-hits", "1");
        expect(await stat(page, "hit")).toBe(1);
        expect(await stat(page, "dim")).toBe(7);      // 一致以外 6 点＋隠れている孤立した点
        expect(await stat(page, "off")).toBe(1);      // 孤立した点だけ隠れている
        await shot(page, `map-highlight-${vp.name}-${scheme}`);
        // 絞る: 一致と近傍だけ（Qwen・Alibaba・llama.cpp・GGUF）
        await page.getByTestId("mode-filter").click();
        await expect(cv).toHaveAttribute("data-nodes", "4");
        await expect(page.getByTestId("map-hits")).toContainText("一致と近傍だけを表示");
        await shot(page, `map-filter-${vp.name}-${scheme}`);
        await page.getByTestId("mode-highlight").click();
        await page.getByTestId("map-search").fill("");
        await expect(cv).toHaveAttribute("data-hits", "0");
        await expect(cv).toHaveAttribute("data-nodes", "7");
        // 絞り込み（型・確かさ・期間）
        await page.getByTestId("legend-co_occurs").click();
        await expect(page.getByTestId("map-count")).toContainText("1 本を隠しています");
        await page.getByTestId("legend-co_occurs").click();
        await page.getByTestId("map-filters").getByLabel("確かさ").selectOption("0.7");
        await expect(page.getByTestId("map-count")).toContainText("5 本を隠しています");
        await page.getByTestId("map-filters").getByLabel("確かさ").selectOption("0");
        await page.getByTestId("map-filters").getByLabel("期間").selectOption("90");
        await expect(page.getByTestId("map-count")).toContainText("1 本を隠しています");      // 200 日前が最後の「推測」の線
        await page.getByTestId("map-filters").getByLabel("期間").selectOption("0");
        await page.getByTestId("map-filters").getByLabel("Things の種類").selectOption("org");
        await expect(cv).toHaveAttribute("data-nodes", "1");
        await page.getByTestId("map-filters").getByLabel("Things の種類").selectOption("");
        // ローカル: 点を押す → ローカルで見る → 深さ 1〜3
        await tap(page, 2);
        await expect(page.getByTestId("map-selected")).toContainText("Qwen 3.5");
        await page.getByTestId("map-local-open").click();
        await expect(cv).toHaveAttribute("data-view", "local");
        await expect(cv).toHaveAttribute("data-nodes", "4");
        await page.getByTestId("map-depth").fill("2");
        await expect(cv).toHaveAttribute("data-nodes", "7");
        const depths = state.calls.filter((c) => c.path === "/graph/neighbors").map((c) => (c.body as { depth: number }).depth);
        expect(depths).toEqual([1, 2]);
        await expect(page.getByTestId("map-depth")).toHaveAttribute("max", "5");      // ローカルの深さは 1〜5
        await expectNoHorizontalOverflow(page);
        await shot(page, `map-local-${vp.name}-${scheme}`);
        await page.getByTestId("map-back").click();
        await expect(cv).toHaveAttribute("data-view", "overview");
        await expect(cv).toHaveAttribute("data-nodes", "7");
        await tap(page, 2);
        await page.getByTestId("map-open-card").click();
        await expect(page).toHaveURL(/\/entities\/2$/);
        await expect(page.getByTestId("knowledge-card")).toBeVisible();
        expect(state.unhandled).toEqual([]);
      });

      test("名寄せ: 自動で寄せた記録（段と根拠）が出て、戻せる。確定・却下の操作は無い（管理者）", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities");
        await expect(page.getByTestId("merge-row")).toHaveCount(3);
        await expect(page.getByTestId("merge-history")).toContainText("Sony Corporation → Sony");
        await expect(page.getByTestId("merge-history")).toContainText("Wikidata Q21708200（アメリカの人工知能研究機関）");
        await expect(page.getByTestId("merge-history")).toContainText("AI の判断 確かさ 0.93");
        await expect(page.getByTestId("merge-pending")).toContainText("1 件");
        await expect(page.getByTestId("merge-confirm")).toHaveCount(0);
        await expect(page.getByTestId("merge-dismiss")).toHaveCount(0);
        await expectNoHorizontalOverflow(page);
        await shot(page, `merge-panel-${vp.name}-${scheme}`);
        await page.getByTestId("merge-undo").first().click();
        await expect(page.getByTestId("merge-row")).toHaveCount(2);
        expect(state.mergeActions).toEqual(["undo:1"]);
        expect(state.unhandled).toEqual([]);
      });

      test("まとまりを押すと中を開く（その塊へ寄り、ほかは薄く）。主題の入口から地図へ", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/map");
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-nodes", "7");
        await page.getByTestId("map-group").first().click();
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-hits", "7");
        expect(state.calls.some((c) => c.path === "/things/map/community/1")).toBe(true);
        await page.goto("/map?community=1");
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-hits", "7");
        expect(state.unhandled).toEqual([]);
      });

      test("夜の配置の計算の前（保存した座標が無い）でも、まとまりごとの塊で全体が出て、押すとカードへ行ける", async ({ page }) => {
        const state = newState({ noLayout: true });
        await mockApi(page, state);
        await page.goto("/map");
        await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-nodes", "7");
        await expect(page.getByTestId("map-count")).toContainText("点 7");
        await tap(page, 2);
        await expect(page.getByTestId("map-selected")).toContainText("Qwen 3.5");
        await shot(page, `map-nolayout-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("主題の入口（MOC）: 一覧・まとまり・チャンネル。主要な Things・最近の出来事・未解決の問い", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/entities");
        await page.getByTestId("things-moc-link").click();
        await expect(page.getByTestId("moc-index")).toBeVisible();
        await expect(page.getByTestId("moc-channel")).toHaveCount(2);
        await expect(page.getByTestId("moc-community")).toHaveCount(2);
        await expectNoHorizontalOverflow(page);
        await shot(page, `moc-index-${vp.name}-${scheme}`);
        await page.getByTestId("moc-community").first().click();
        await expect(page.getByTestId("moc-title")).toContainText("llama.cpp のまとまり");
        await expect(page.getByTestId("moc-things").locator("li")).toHaveCount(5);
        await expect(page.getByTestId("moc-things")).toContainText("ローカルで LLM を動かす");
        await expect(page.getByTestId("moc-events")).toContainText("b9000");
        await expect(page.getByTestId("moc-questions")).toContainText("長い文脈で品質が劣化するか？");
        await expectNoHorizontalOverflow(page);
        await shot(page, `moc-community-${vp.name}-${scheme}`);
        await page.getByTestId("moc-open-map").click();
        await expect(page).toHaveURL(/\/map\?community=1$/);
        await page.goto("/moc");
        await page.getByTestId("moc-channel").first().click();
        await expect(page.getByTestId("moc-title")).toContainText("チャンネル");
        await expectNoHorizontalOverflow(page);
        await shot(page, `moc-channel-${vp.name}-${scheme}`);
        expect(state.unhandled).toEqual([]);
      });

      test("Obsidian の書き出し: 押したときだけ zip を受け取る。機密の区分は既定で含めない", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/map");
        await expect(page.getByTestId("export-form")).toBeVisible();
        expect(state.calls.some((c) => c.path === "/kg/export")).toBe(false);      // 開いただけでは書き出さない
        await shot(page, `export-${vp.name}-${scheme}`);
        const dl = page.waitForEvent("download");
        await page.getByTestId("export-button").click();
        expect((await dl).suggestedFilename()).toBe("knowledge.zip");
        await expect(page.getByTestId("export-state")).toContainText("3 件");
        let body = state.calls.find((c) => c.path === "/kg/export")!.body as { confidential: boolean; min_articles: number };
        expect(body.confidential).toBe(false);
        expect(body.min_articles).toBe(3);
        await page.getByTestId("export-confidential").check();
        const dl2 = page.waitForEvent("download");
        await page.getByTestId("export-button").click();
        await dl2;
        body = state.calls.filter((c) => c.path === "/kg/export")[1].body as { confidential: boolean; min_articles: number };
        expect(body.confidential).toBe(true);
        await expectNoHorizontalOverflow(page);
        expect(state.unhandled).toEqual([]);
      });

      test("分析の画面の「知識の地図」の枠が地図へのリンクになり、メニューにも出る", async ({ page }) => {
        const state = newState();
        await mockApi(page, state);
        await page.goto("/analytics");
        await page.getByTestId("map-link").click();
        await expect(page).toHaveURL(/\/map$/);
        await expect(page.getByTestId("map-page")).toBeVisible();
        expect(state.unhandled).toEqual([]);
      });
    });
  }
}

for (const N of [1500, 5000]) {
test.describe(`全体の地図の重さ（${N.toLocaleString()} 点）`, () => {
  test.use({ viewport: { width: 1280, height: 900 } });

  test(`開いて 3 秒以内に全体が出て、拡大・縮小・移動が滑らか（1 コマの平均 ${N > 1500 ? 200 : 60}ms 以下）。まとまりを押すと中を開く`, async ({ page }, info) => {
    const state = newState({ bigMap: N });
    await mockApi(page, state);
    const t0 = Date.now();
    await page.goto("/map");
    await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-nodes", String(N), { timeout: 10_000 });
    const ready = Date.now() - t0;
    await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-edges", String(Math.round((N * 3500) / 1500)));
    expect(ready).toBeLessThan(N > 1500 ? 5000 : 3000);
    await shot(page, `map-big-overview-${N}`);
    expect(await stat(page, "big")).toBe(8);      // 全体の地図で、縮小したまま名前を出すのは重要度の大きい 8 点だけ（名前の量の既定 0.5）（残りは大きな点から順に、拡大するほど出す）
    const frame = await page.evaluate(async () => {
      const sg = (document.querySelector("[data-testid=map-canvas]") as unknown as { __sigma: { getCamera(): { getState(): { x: number; y: number; ratio: number }; setState(s: { x?: number; y?: number; ratio?: number }): void } } }).__sigma;
      const cam = sg.getCamera();
      const times: number[] = [];
      const raf = () => new Promise<number>((r) => requestAnimationFrame(r));
      let last = await raf();
      for (let i = 0; i < 40; i += 1) {
        const st = cam.getState();
        cam.setState({ ratio: st.ratio * (i < 20 ? 1 / 1.06 : 1.06), x: st.x + (i % 2 ? 0.002 : -0.002) });      // 拡大・縮小しながら左右へ動かす
        const now = await raf();
        times.push(now - last);
        last = now;
      }
      return { avg: times.reduce((a, b) => a + b, 0) / times.length, max: Math.max(...times) };
    });
    info.annotations.push({ type: "perf", description: `全体 ${N} 点: 開くまで ${ready}ms・1 コマ 平均 ${frame.avg.toFixed(1)}ms・最大 ${frame.max.toFixed(1)}ms` });
    console.log(`PERF 全体 ${N} 点: 開くまで ${ready}ms・1 コマ 平均 ${frame.avg.toFixed(1)}ms・最大 ${frame.max.toFixed(1)}ms`);
    expect(frame.avg).toBeLessThan(N > 1500 ? 200 : 60);      // 5,000 点はヘッドレス（GPU なしのソフトウェア描画）の目安。実機の GPU はこれより軽い
    await page.getByTestId("map-group").first().click();
    await expect(page.getByTestId("map-canvas")).toHaveAttribute("data-hits", String(Math.min(300, Math.ceil(N / 24))));
    await shot(page, `map-big-group-${N}`);
    expect(state.unhandled).toEqual([]);
  });
});
}

/** 丸の重なり（2026-11-25）。目的: 丸の大きさは画面のピクセル基準で、拡大しても画面の大きさはほぼ同じ（間隔だけが広がる）。全体を出したとき、近い点との画面上の距離から大きさの倍率が自動で決まり、
 *  重なる点の割合が 1% 未満（スマホ 390px でも）。2 倍・4 倍・8 倍に拡大すると 0%。拡大で丸が画面を覆わない（半径 px の最大が拡大 8 倍でも 40px 以下）。撮影は overlap を名前に含める。
 *  根拠: 利用者の iPhone の撮影（2026-11-25「丸同士がかぶっている」・拡大すると丸が巨大になって画面を覆う）・Obsidian は拡大しても丸の画面の大きさがほぼ同じ。
 *  配置は、夜の配置が守る性質（丸の縁の最小の間隔）をそのまま持つ合成のひまわり配置（実データの撮影は評価で別に行う）。 */
for (const vp of [{ name: "390", width: 390, height: 844 }, { name: "1280", width: 1280, height: 900 }] as const) {
  test(`丸の重なり（${vp.name}px）: 全体で 1% 未満・拡大 2・4・8 倍で 0・拡大しても丸は画面を覆わない`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    const state = newState();
    await mockApi(page, state);
    const N = 700, ga = 2.399963229728653;
    const nodes = Array.from({ length: N }, (_, i) => ({ id: 3000 + i, name: `Things ${i}`, type: "tech", article_count: 1, degree: 1, recent: 0, community: (i % 9) + 1,
      x: 24 * Math.sqrt(i + 0.5) * Math.cos(i * ga), y: 24 * Math.sqrt(i + 0.5) * Math.sin(i * ga), importance: Math.min(1, 1 / (1 + i / 12) ** 0.8) }));
    const body = { nodes, edges: [], isolated: [], communities: Array.from({ length: 9 }, (_, c) => ({ id: c + 1, name: `まとまり${c}`, size: 70, rep_id: 3000 + c, shown: 70, top: [] })), truncated: false, limit: 1500,
      layout: { version: 9, at: "2026-11-25T03:10:00Z", placed: N }, totals: { with_relations: N, edges: 0, shown_nodes: N, shown_edges: 0 } };
    await page.route("**/api/v1/things/map/overview**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) }));
    await page.goto("/map");
    const cv = page.getByTestId("map-canvas");
    await expect(cv).toHaveAttribute("data-ready", "true");
    await expect(cv).toHaveAttribute("data-nodes", String(N), { timeout: 10_000 });
    await cv.scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
    for (const z of [1, 2, 4, 8]) {
      await page.evaluate((zz) => (document.querySelector("[data-testid=map-canvas]") as unknown as { __sigma: { getCamera(): { setState(s: { ratio: number }): void } } }).__sigma.getCamera().setState({ ratio: 1 / zz }), z);
      await page.waitForTimeout(350);
      const st = await page.evaluate(() => (document.querySelector("[data-testid=map-canvas]") as unknown as { __stat(): Record<string, number> }).__stat());
      await shot(page, `map-overlap-${vp.name}-x${z}`);
      if (z === 1) expect(st.overlap, `全体の重なり ${st.overlap}`).toBeLessThan(0.01);
      else expect(st.overlap, `${z} 倍の重なり ${st.overlap}`).toBe(0);
      expect(st.sizeMax * Math.sqrt(z)).toBeLessThan(40);      // 拡大でゆっくり（sqrt）大きくなるだけ
    }
  });
}
