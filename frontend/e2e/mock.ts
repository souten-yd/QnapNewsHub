/** E2E の API の模擬（page.route）。画面は本物のバックエンドが配る build をそのまま使い、/api/v1 だけを差し替える。
 *  模擬に無い API が呼ばれたら 404 を返して unhandled に記録する（試験の終わりに空であることを確かめる）。 */
import type { Page, Route } from "@playwright/test";
import type { Digest, NewsHome, NewsItem, NewsPrefs, NewsRuntimeSettings } from "../src/api/news";

const NOW = Date.now();
const ago = (hours: number) => new Date(NOW - hours * 3_600_000).toISOString();
const noState = { open: false, like: false, dislike: false, hide: false, star: false };

export const CHANNELS = [
  { id: "ai", name: "AI・LLM", color: "#7c3aed", number: 1 },
  { id: "infra", name: "インフラ", color: "#0ea5e9", number: 2 },
];

const mk = (id: number, over: Partial<NewsItem>): NewsItem => ({
  id, kind: "news", source: "見本ニュース", source_key: "sample", title: `記事 ${id}`, url: `https://news.example.test/a/${id}`, summary: "記事の概要です。",
  image_url: null, published_at: ago(id), topics: ["llama.cpp"], state: { ...noState }, channel: "ai", ...over,
});

export const ITEMS: NewsItem[] = [
  mk(1, { title: "llama.cpp の新しい量子化が入りました", summary: "GPU が無くても動く軽い量子化。速度と精度の比較が公開されました。", image_url: "https://img.example.test/1.png", topics: ["llama.cpp", "量子化"] }),
  mk(2, { title: "画像のない記事: ローカル LLM の運用メモ", summary: "画像の部分は出さず、細い線だけが付きます。", topics: ["運用"] }),
  mk(3, { kind: "release", source: "llama.cpp", source_key: "llama-cpp", title: "llama.cpp b9000 を公開", summary: "新しい量子化と修正。", topics: ["llama.cpp"] }),
  mk(4, { kind: "forum", source: "フォーラム", source_key: "forum", title: "24GB でどこまで動くか", summary: "実測の報告が集まっています。", channel: "infra", topics: ["VRAM"] }),
  mk(5, { kind: "model", source: "Hugging Face", source_key: "hf", title: "新しい GGUF モデルが話題に", summary: "小さくて速いモデル。", image_url: "https://img.example.test/2.png", channel: "ai", topics: ["GGUF"] }),
  mk(6, { title: "NAS で動かす小さなサーバー", summary: "非力な機器でニュースを集める話。", channel: "infra", topics: ["NAS"] }),
];

const section = (c: (typeof CHANNELS)[number]) => {
  const items = ITEMS.filter((i) => i.channel === c.id);
  return { ...c, total: items.length, week: items.length, items };
};

export const HOME: NewsHome = {
  channels: CHANNELS,
  onair: ITEMS[0],
  ticker: ITEMS.map((i) => ({ id: i.id, title: i.title, source: i.source, url: i.url, published_at: i.published_at })),
  now: ITEMS.slice(1, 3), today: ITEMS.slice(3, 5), week: ITEMS.slice(5),
  sections: CHANNELS.map(section),
  interest: { topics: [{ topic: "llama.cpp", weight: 3 }, { topic: "量子化", weight: 1.5 }], sources: [], avoided_sources: [], muted: [], chat_terms: 0 },
  trends: { days: ["11-22", "11-23", "11-24"], topics: [{ topic: "llama.cpp", counts: [1, 2, 3], total: 6 }], sources: [{ source: "見本ニュース", count: 4 }] },
  numbers: { today: 6, read_today: 1, read_30d: 4, top_channel: { id: "ai", name: "AI・LLM", color: "#7c3aed", reads: 3 }, top_topic: "llama.cpp", top_topic_count: 6 },
  generated_at: ago(0),
};

export const DIGEST_BODY = [
  "## 今日の 3 行",
  "- 新しい量子化が公開され、軽いモデルが動かしやすくなった [1]",
  "- 小さな機器でニュースを集める取り組みが話題 [2]",
  "- フォーラムでは 24GB での実測が集まっている",
  "## AI・LLM",
  "- 傾向: 量子化と小型モデルの話題が多い [1]",
  "- 主な技術・製品: llama.cpp、GGUF",
  "- 注目点: 速度と精度のバランス",
].join("\n");

export const DIGEST_DAILY: Digest = {
  id: 1, period: "daily", label: "今朝のまとめ", day: new Date(NOW).toLocaleDateString("en-CA"), body: DIGEST_BODY, model: "example/free-model:free", created_at: ago(1),
  error: "", fallback: false, item_ids: [1, 2],
  sources: [{ id: 1, n: 1, title: ITEMS[0].title, source: "見本ニュース", url: ITEMS[0].url, topics: ["llama.cpp"] }, { id: 6, n: 2, title: ITEMS[5].title, source: "見本ニュース", url: ITEMS[5].url, topics: ["NAS"] }],
};
export const DIGEST_WEEKLY: Digest = { ...DIGEST_DAILY, id: 2, period: "weekly", label: "今週のまとめ", body: DIGEST_BODY.replace("今日の 3 行", "今週の 3 行") };

export const PREFS: NewsPrefs = {
  display: { design: "broadcast", accent: "", font_size: "m", density: "normal", images: true, columns: "auto", ticker: true, onair: true, numbers: true, reduce_motion: true, home_items: 5 },
  taste: { strength: 0.5, use_chat: false, half_life_days: 30, mute_terms: [], mute_sources: [], dim_read: true },
};

export const SETTINGS: NewsRuntimeSettings = {
  feed_refresh_minutes: 15,
  digest_daily: { enabled: true, time: "06:30" }, digest_weekly: { enabled: true, weekday: 0, time: "07:00" }, quiet: { enabled: false, start: "01:00", end: "05:00" },
  llm: { digest_model: "example/free-model:free", gist_enabled: false, gist_model: "example/free-model:free", digest_focus: "", prep_enabled: true, prep_model: "example/free-model:free", prep_max_per_run: 100, daily_limit: 300, minute_limit: 10 },
};

const managed = (i: number, over: Record<string, unknown> = {}) => ({
  key: `rss:${i}`, kind: "rss", format: "rss", name: `見本の取得先 ${i}`, value: `https://feed${i}.example.test/rss`, domain: `feed${i}.example.test`, enabled: true, interval: 30, groups: ["ai"],
  last_at: ago(0.2), last_ok_at: ago(0.2), last_ok: true, last_error: "", last_count: 20, last_status: 200, last_skipped: "", week: [100, 100, 90, 100, 100, 100, 100], total: 120, ...over,
});

export interface MockState {
  authenticated: boolean; setupNeeded: boolean; username: string; keySet: boolean; prefs: NewsPrefs; settings: NewsRuntimeSettings;
  hidden: Set<number>; calls: { method: string; path: string; body: unknown }[]; unhandled: string[];
  /** 画像の無い記事だけの一覧にしたいとき等、一覧を差し替える */
  items: NewsItem[];
}

export function newState(over: Partial<MockState> = {}): MockState {
  return { authenticated: true, setupNeeded: false, username: "admin", keySet: false, prefs: structuredClone(PREFS), settings: structuredClone(SETTINGS),
    hidden: new Set(), calls: [], unhandled: [], items: ITEMS, ...over };
}

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
// 1x1 の PNG（画像のある記事の見本）
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

export async function mockApi(page: Page, state: MockState): Promise<void> {
  await page.route("https://img.example.test/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: PNG }));
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = req.method();
    let body: unknown = null;
    try { body = req.postDataJSON(); } catch { /* 本文なし */ }
    state.calls.push({ method, path, body });
    const is = (m: string, p: string | RegExp) => method === m && (typeof p === "string" ? path === p : p.test(path));

    if (is("GET", "/auth/status")) return json(route, { setup_needed: state.setupNeeded, authenticated: state.authenticated, username: state.authenticated ? state.username : null });
    if (is("POST", "/auth/login")) {
      const b = body as { username: string; password: string };
      if (b.password !== "correct-horse-battery") return json(route, { detail: "利用者名かパスワードが違います" }, 401);
      state.authenticated = true; state.username = b.username;
      return json(route, { username: b.username });
    }
    if (is("POST", "/auth/setup")) {
      const b = body as { username: string; password: string };
      state.setupNeeded = false; state.authenticated = true; state.username = b.username;
      return json(route, { username: b.username });
    }
    if (is("POST", "/auth/logout")) { state.authenticated = false; return json(route, { ok: true }); }
    if (!state.authenticated) return json(route, { detail: "ログインしてください" }, 401);
    if (is("GET", "/auth/hub")) return json(route, { host: "127.0.0.1", port: 8787, cookie_secure: false });
    if (is("PUT", "/auth/hub")) return json(route, { ...(body as object), restart_needed: true });

    if (is("GET", "/news/prefs")) return json(route, state.prefs);
    if (is("PUT", "/news/prefs")) { state.prefs = body as NewsPrefs; return json(route, state.prefs); }
    const pick = (list: NewsItem[]) => list.map((i) => state.items.find((x) => x.id === i.id)).filter((i): i is NewsItem => Boolean(i));
    if (is("GET", "/news/home")) return json(route, { ...HOME, onair: state.items[0] ?? null, now: pick(HOME.now), today: pick(HOME.today), week: pick(HOME.week), sections: HOME.sections.map((s) => ({ ...s, items: state.items.filter((i) => i.channel === s.id) })) });
    if (is("GET", "/news/channels")) return json(route, {
      channels: CHANNELS.map((c) => ({ id: c.id, name: c.name, color: c.color, include: ["llama"], exclude: [], kinds: [], lang: "both", limit: 6, search: true })),
      archive_keep: 20000, interval_minutes: 30, kinds: { release: "更新", news: "ニュース", model: "モデル", forum: "フォーラム", reddit: "Reddit" }, reddit_key: false,
      limits: { channels: 8, terms: 20, palette: ["#7c3aed", "#0ea5e9", "#10b981"] },
    });
    if (is("GET", "/news/items")) {
      const q = url.searchParams.get("q") ?? "";
      const list = state.items.filter((i) => !state.hidden.has(i.id) && (!q || i.title.includes(q) || i.summary.includes(q)));
      return json(route, { items: list.map((i) => ({ ...i, state: state.hidden.has(i.id) ? { ...i.state, hide: true } : i.state })), next: null });
    }
    if (is("POST", /^\/news\/items\/\d+\/action$/)) {
      const id = Number(path.split("/")[3]);
      const b = body as { action: string; on: boolean };
      if (b.action === "hide") b.on ? state.hidden.add(id) : state.hidden.delete(id);
      return json(route, { state: { ...noState, [b.action]: b.on } });
    }
    if (is("GET", "/news/archive")) return json(route, { days: [{ date: new Date(NOW).toLocaleDateString("en-CA"), count: 6 }], total: 6, starred: 0 });
    if (is("GET", "/news/trends")) return json(route, HOME.trends);
    if (is("GET", "/news/profile")) return json(route, HOME.interest);
    if (is("GET", "/news/digests")) return json(route, { latest: { daily: DIGEST_DAILY, weekly: DIGEST_WEEKLY }, digests: [DIGEST_DAILY, DIGEST_WEEKLY] });
    if (is("GET", /^\/news\/digests\/\d+$/)) return json(route, DIGEST_DAILY);
    if (is("GET", "/news/sources")) return json(route, {
      config: { enabled: {}, github: [], codeberg: [], rss: [], pages: [], hackernews: true, huggingface: true, discourse: [], reddit: [] },
      kinds: { release: "更新", news: "ニュース" }, reddit_key: false, limit: 200,
      sources: [{ source_key: "reddit", label: "r/LocalLLaMA", kind: "reddit", ok: false, skipped: "Reddit は API キーが無いので取得しません", error: "", count: 0, at: ago(1) }],
      last: { finished: ago(0.5), new: 3 }, running: false,
    });
    if (is("GET", "/news/sources/manage")) return json(route, {
      items: [managed(1), managed(2, { name: "失敗中の取得先", last_ok: false, last_error: "接続できませんでした", last_status: null, week: [100, 80, 0, 0, null, 0, 0] }), managed(3, { enabled: false, name: "止めている取得先" })],
      groups: [{ id: "ai", label: "AI・LLM", hint: "", count: 3 }], default_interval: 30, reddit_key: false, limit: 200, interval_range: [15, 1440], formats: { rss: "RSS／Atom" }, running: false,
    });
    if (is("GET", "/news/sources/manage/detail")) return json(route, { item: managed(1), history: [{ at: ago(0.2), ok: true, status: 200, count: 20, new: 2, ms: 420, error: "", skipped: "" }], articles: [{ id: 1, title: ITEMS[0].title, url: ITEMS[0].url!, published_at: ITEMS[0].published_at }] });
    if (is("GET", "/news/presets")) return json(route, { groups: [{ id: "ai", label: "AI・LLM", hint: "", count: 1 }], items: [{ id: "p1", group: "ai", kind: "rss", name: "見本の目録", value: "https://preset.example.test/rss", lang: "ja", note: "", key: false, verified: "2026-11-24", recommended: true }], verified: "2026-11-24" });
    if (is("GET", "/news/settings")) return json(route, {
      settings: state.settings, weekdays: ["月", "火", "水", "木", "金", "土", "日"], interval_minutes: 30, prep_calls_today: 12, prep_batch: 30,
      llm_stats: { day: new Date(NOW).toLocaleDateString("en-CA"), calls: { prep: 12, digest: 2, gist: 0 }, total: 14, daily_limit: state.settings.llm.daily_limit, minute_limit: state.settings.llm.minute_limit, failed: 1, last_error: "混み合っています（429）", key_set: state.keySet },
    });
    if (is("PUT", "/news/settings")) { state.settings = body as NewsRuntimeSettings; return json(route, { settings: state.settings }); }
    if (is("GET", "/news/openrouter-key")) return json(route, { set: state.keySet });
    if (is("PUT", "/news/openrouter-key")) { state.keySet = true; return json(route, { set: true }); }
    if (is("DELETE", "/news/openrouter-key")) { state.keySet = false; return json(route, { set: false }); }
    if (is("POST", "/news/refresh")) return json(route, { started: true, reason: "", new: 2, failed: 0 });
    if (is("PUT", "/news/channels")) return json(route, body);

    state.unhandled.push(`${method} ${path}`);
    return json(route, { detail: "模擬にない API です" }, 404);
  });
}
