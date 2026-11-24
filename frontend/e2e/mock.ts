/** E2E の API の模擬（page.route）。画面は本物のバックエンドが配る build をそのまま使い、/api/v1 だけを差し替える。
 *  模擬に無い API が呼ばれたら 404 を返して unhandled に記録する（試験の終わりに空であることを確かめる）。 */
import type { Page, Route } from "@playwright/test";
import type { UpdateStatus, UpdateRelease } from "../src/api/update";
import type { LinkStatus } from "../src/api/link";
import type { AnalyticsFilters, Compare, HotTopics, Overview, Series, SourcesAnalysis, TrendStats } from "../src/api/analytics";
import type { EventDetail, EventRow } from "../src/api/events";
import type { Digest, EntityDetail, EntityRow, NewsHome, NewsItem, NewsPrefs, NewsRuntimeSettings } from "../src/api/news";

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

export const ENTITIES: EntityRow[] = [
  { id: 1, name: "llama.cpp", type: "software", first_seen: ago(900), last_seen: ago(1), article_count: 42, period_count: 9 },
  { id: 2, name: "Qwen 3.5", type: "model", first_seen: ago(300), last_seen: ago(3), article_count: 17, period_count: 6 },
  { id: 3, name: "Alibaba", type: "org", first_seen: ago(700), last_seen: ago(3), article_count: 11, period_count: 3 },
  { id: 4, name: "Radeon AI PRO R9700", type: "product", first_seen: ago(200), last_seen: ago(9), article_count: 6, period_count: 2 },
  { id: 5, name: "量子化", type: "tech", first_seen: ago(800), last_seen: ago(2), article_count: 5, period_count: 4 },
  { id: 6, name: "EU AI 規則（とても長い名前のついた制度の見本で、320px でも折り返して収まること）", type: "policy", first_seen: ago(500), last_seen: ago(30), article_count: 2, period_count: 0 },
];
export const ENTITY_DETAIL: EntityDetail = {
  id: 2, name: "Qwen 3.5", type: "model", aliases: ["Qwen3.5", "ＱＷＥＮ 3.5"], first_seen: ago(300), last_seen: ago(3), article_count: 17,
  timeline: [
    { id: 1, title: "llama.cpp の新しい量子化が入りました", url: "https://news.example.test/a/1", source: "見本ニュース", published_at: ago(3), summary: "Qwen 3.5 の軽い量子化の比較が公開されました。" },
    { id: 5, title: "新しい GGUF モデルが話題に", url: "https://hf.example.test/m/1", source: "Hugging Face", published_at: ago(80), summary: "小さくて速いモデル。" },
    { id: 6, title: "NAS で動かす小さなサーバー", url: null, source: "見本ニュース", published_at: ago(200), summary: "" },
  ],
  facts: [
    { id: 1, kind: "release", text: "Qwen 3.5 が公開された", value: "3.5", article_id: 1, article_title: "llama.cpp の新しい量子化が入りました", published_at: ago(3), model: "example/free-model:free", prompt_version: "ex1", ai_extracted: true },
    { id: 2, kind: "benchmark", text: "128K の文脈で動く", value: "128K", article_id: 5, article_title: "新しい GGUF モデルが話題に", published_at: ago(80), model: "example/free-model:free", prompt_version: "ex1", ai_extracted: true },
  ],
  related: [{ id: 3, name: "Alibaba", type: "org", count: 9 }, { id: 1, name: "llama.cpp", type: "software", count: 7 }, { id: 5, name: "量子化", type: "tech", count: 3 }],
};

export const ITEMS: NewsItem[] = [
  mk(1, { entities: [{ id: 1, name: "llama.cpp", type: "software" }, { id: 2, name: "Qwen 3.5", type: "model" }, { id: 5, name: "量子化", type: "tech" }], title: "llama.cpp の新しい量子化が入りました", summary: "GPU が無くても動く軽い量子化。速度と精度の比較が公開されました。", image_url: "https://img.example.test/1.png", topics: ["llama.cpp", "量子化"] }),
  mk(2, { has_body: true, title: "画像のない記事: ローカル LLM の運用メモ", summary: "画像の部分は出さず、細い線だけが付きます。", topics: ["運用"] }),
  mk(3, { event: { id: 1, others: 3, sources: 4, primary: true }, entities: [{ id: 1, name: "llama.cpp", type: "software" }], kind: "release", source: "llama.cpp", source_key: "llama-cpp", title: "llama.cpp b9000 を公開", summary: "新しい量子化と修正。", topics: ["llama.cpp"] }),
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
  rest: { enabled: false, start: "01:00", end: "06:00", mode: "keep_memory" }, storage: { flush_minutes: 30, max_items: 1000, max_mb: 128, log_file: false, access_log: false, compress_years: 1 },
  body: { enabled: true, per_day: 200, per_minute: 6 },
  llm: { digest_model: "example/free-model:free", gist_enabled: false, gist_model: "example/free-model:free", digest_focus: "", prep_enabled: true, prep_model: "example/free-model:free", prep_max_per_run: 100, daily_limit: 800, minute_limit: 15,
    extract_enabled: true, fallback_models: [], quota: { prep: 300, gist: 150, digest: 20, card: 150, trend: 10, event: 100, spare: 70 }, night_from: 21, backfill_enabled: true,
    gateway: { enabled: false, base_url: "", model: "auto", first: true, batch: 10, timeout_seconds: 300, minute_limit: 6, cooldown_minutes: 10 } },
};

const managed = (i: number, over: Record<string, unknown> = {}) => ({
  key: `rss:${i}`, kind: "rss", format: "rss", name: `見本の取得先 ${i}`, value: `https://feed${i}.example.test/rss`, domain: `feed${i}.example.test`, enabled: true, interval: 30, groups: ["ai"],
  last_at: ago(0.2), last_ok_at: ago(0.2), last_ok: true, last_error: "", last_count: 20, last_status: 200, last_skipped: "", week: [100, 100, 90, 100, 100, 100, 100], total: 120,
  fixed: false, origin: "press", origin_set: false, category: "",
  poll: { interval: 45, auto: true, backoff: false, fails: 0, next_at: new Date(NOW + 25 * 60_000).toISOString(), hit_rate: 80, conditional: 10, hits: 8, has_etag: true }, ...over,
});

const IMPORT_ROWS = [
  { line: 2, name: "見本のニュース", url: "https://import1.example.test/rss", category: "ニュース", origin: "press", origin_guess: "press", interval: 60, enabled: true, kind: "rss", status: "new", reason: "" },
  { line: 3, name: "見本の公式ブログ", url: "https://blog.import2.example.test/feed", category: "企業", origin: "", origin_guess: "official", interval: 0, enabled: true, kind: "rss", status: "new", reason: "http を https に直しました" },
  { line: 4, name: "見本の取得先 1", url: "https://feed1.example.test/rss", category: "", origin: "", origin_guess: "other", interval: 0, enabled: true, kind: "rss", status: "duplicate", reason: "すでに登録されています" },
  { line: 5, name: "使えない行", url: "ftp://bad.example.test/x", category: "", origin: "", origin_guess: "other", interval: 0, enabled: true, kind: "rss", status: "invalid", reason: "https の URL ではありません" },
];

export const UPDATE_NOTES = [
  "## 0.2.0", "- **更新の機能**が入りました", "- 設定の「アプリの更新」から `今すぐ確認` できます", "", "```", "./hub.sh setup", "```", "",
  "<script>window.__pwned = 1</script>", "<img src=x onerror=\"window.__pwned = 2\">", "[公式](https://github.com/example/repo) [危険](javascript:alert(1))",
].join("\n");
export const UPDATE_LATEST: UpdateRelease = { version: "0.2.0", tag: "v0.2.0", prerelease: false, notes: UPDATE_NOTES, url: "https://github.com/example/repo/releases/tag/v0.2.0", published_at: ago(5), size: 95_000_000 };
export const UPDATE_BASE: UpdateStatus = {
  version: "0.1.1", install: "qpkg", can_install: true, include_prerelease: false, checked_at: ago(2), checking: false, error: "", latest: null, update_available: false, rollback: null,
  job: { phase: "idle", kind: "", target: "", message: "", percent: 0, started_at: "" }, last: null, rest: { active: false, ends_at: null }, etag_hits: 2, checks: 3, manual_steps: [],
};

// ---- 分析（見本の数字。実データではない） ----
const DAYS7 = Array.from({ length: 7 }, (_, i) => new Date(NOW - (6 - i) * 86_400_000).toLocaleDateString("en-CA"));
const hot = (term: string, label: string, score: number | null, count: number, mean: number, sources: number, spark: number[]) =>
  ({ term, label, score, count, count_all: count + 1, baseline_mean: mean, baseline_sd: 1, growth_pct: Math.round(((count - mean) / Math.max(mean, 1)) * 100), sources, spark });
export const HOT: HotTopics = {
  window_hours: 24, baseline_days: 28, enough_history: true, spark_days: DAYS7,
  top: [hot("rocm", "ROCm", 6.1, 14, 2.1, 5, [1, 2, 1, 3, 2, 4, 14, 14]), hot("qwen", "Qwen", 4.4, 11, 3, 4, [2, 3, 2, 3, 4, 3, 5, 11]), hot("gguf", "GGUF", 3.2, 8, 2, 3, [1, 1, 2, 1, 2, 2, 3, 8]),
    hot("vulkan", "Vulkan", 2.5, 6, 1.5, 3, [1, 1, 1, 2, 1, 1, 2, 6]), hot("nas", "NAS", 2.1, 5, 1, 2, [0, 1, 1, 0, 1, 1, 2, 5]), hot("llama.cpp", "llama.cpp", 1.9, 9, 5, 4, [4, 5, 5, 6, 5, 4, 6, 9]),
    hot("ollama", "Ollama", 1.7, 5, 2, 3, [2, 2, 1, 2, 2, 3, 2, 5]), hot("radeon", "Radeon", 1.5, 4, 1.4, 2, [1, 1, 2, 1, 1, 2, 1, 4]), hot("wayland", "Wayland", 1.2, 4, 1.8, 2, [1, 2, 2, 1, 2, 1, 2, 4]), { ...hot("entity:2", "Qwen 3.5", 1.1, 6, 2, 3, [1, 0, 1, 1, 0, 1, 1, 6]), kind: "entity", entity_id: 2, entity_type: "model" }],
  new: [hot("zorblax", "Zorblax", null, 4, 0, 3, [0, 0, 0, 0, 0, 0, 0, 4])],
  fields: [{ channel: "ai", name: "AI・LLM", color: "#7c3aed", top: [hot("qwen", "Qwen", 4.4, 11, 3, 4, [2, 3, 2, 3, 4, 3, 5, 11])] }, { channel: "infra", name: "インフラ", color: "#0ea5e9", top: [hot("nas", "NAS", 2.1, 5, 1, 2, [0, 1, 1, 0, 1, 1, 2, 5])] }],
  method: "",
};
export const OVERVIEW: Overview = { today: 412, today_indep: 361, last24h: 980, last24h_indep: 870, sources_today: 57, sources_8d: 80, prev7_daily_avg: 790, prev7_days: 7, ratio_24h: 1.24, ratio_today: 0.52, day: DAYS7[6], built_at: ago(1), unsaved: 0,
  events_24h: 41, independent_events_24h: 612, event_members_24h: 140, singles_24h: 571, independent_events_prev_daily: 530.4 };
const KINDS = [["news", "ニュース"], ["release", "更新"], ["model", "モデル"], ["forum", "フォーラム"], ["reddit", "Reddit"]] as const;
export const ANA_FILTERS: AnalyticsFilters = { kinds: KINDS.map(([id, label]) => ({ id, label })), langs: [{ id: "ja", label: "日本語" }, { id: "en", label: "英語" }], channels: CHANNELS.map((c) => ({ id: c.id, name: c.name, color: c.color })) };
const mockSeries = (p: URLSearchParams): Series => {
  const hour = (p.get("grain") || (["24h", "7d"].includes(p.get("range") ?? "7d") ? "hour" : "day")) === "hour";
  const range = (p.get("range") ?? "7d") as Series["range"];
  const n = hour ? (range === "24h" ? 24 : 168) : ({ "24h": 1, "7d": 7, "30d": 30, "90d": 90 }[range]);
  const buckets = Array.from({ length: n }, (_, i) => (hour ? new Date(NOW - (n - 1 - i) * 3_600_000).toLocaleString("sv-SE").slice(0, 13) : new Date(NOW - (n - 1 - i) * 86_400_000).toLocaleDateString("en-CA")));
  const by = (p.get("by") ?? "none") as Series["by"];
  const keys: [string, string][] = by === "none" ? [["all", "記事"]] : by === "kind" ? [["news", "ニュース"], ["release", "更新"], ["model", "モデル"]] : by === "lang" ? [["ja", "日本語"], ["en", "英語"]]
    : by === "channel" ? [["ai", "AI・LLM"], ["infra", "インフラ"], ["-", "どれにも入らない"]] : by === "origin" ? [["press", "報道"], ["official", "公式"]] : [["rss:a", "見本の取得先 1"], ["rss:b", "見本の取得先 2"]];
  const series = keys.map(([key, label], k) => ({ key, label, counts: buckets.map((_, i) => ((i * 7 + k * 5) % 9) + 2), total: 0, ...(by === "channel" && key !== "-" ? { color: CHANNELS[k].color } : {}) }));
  series.forEach((s) => { s.total = s.counts.reduce((a, b) => a + b, 0); });
  return { range, grain: hour ? "hour" : "day", by, metric: (p.get("metric") ?? "all") as "all" | "indep", buckets, series, total: series.reduce((a, s) => a + s.total, 0) };
};
const SOURCE_ROWS: SourcesAnalysis["sources"] = Array.from({ length: 8 }, (_, i) => ({ key: `rss:${i}`, name: `見本の取得先 ${i + 1}`, origin: "press", articles: 200 - i * 20, independent: 180 - i * 18, runs: 96, success_pct: i === 3 ? 71.9 : 100, not_modified_pct: 62 - i * 3,
  image_pct: 80 - i * 7, dup_pct: 8 + i, new: 20, daily: DAYS7.map((_, d) => 10 + ((d * 3 + i * 5) % 13)) }));
export const COMPARE: Compare = { by: "kind", this_week: [DAYS7[0], DAYS7[6]], prev_week: ["2026-11-10", "2026-11-16"], total_this: 5400, total_prev: 4900,
  rows: [{ key: "news", label: "ニュース", this: 4000, prev: 3500, this_indep: 3600, prev_indep: 3200, delta: 500, pct: 14 }, { key: "release", label: "更新", this: 900, prev: 1000, this_indep: 880, prev_indep: 990, delta: -100, pct: -10 },
    { key: "model", label: "モデル", this: 500, prev: 400, this_indep: 480, prev_indep: 390, delta: 100, pct: 25 }] };

export const TREND: TrendStats = {
  window_hours: 24, enough_history: true, ratio: 1.24,
  cur: { n: 980, indep: 870, dup_pct: 11.2, img_pct: 63.4, lag_min: 38.5, fast_pct: 71.2, primary_n: 52, primary_pct: 5.3 },
  prev: { n: 5530, indep: 5000, dup_pct: 9.6, img_pct: 66.1, lag_min: 45.2, fast_pct: 66, primary_n: 290, primary_pct: 5.2, n_per_day: 790, days: 7 },
  channels: [{ channel: "ai", name: "AI・LLM", color: "#7c3aed", cur: 210, prev_per_day: 140, ratio: 1.5 }, { channel: "infra", name: "インフラ", color: "#0ea5e9", cur: 90, prev_per_day: 100, ratio: 0.9 }],
  new_entities: [{ id: 7, name: "Zorblax Labs", type: "org", count: 5 }, { id: 8, name: "Quasar 7", type: "product", count: 3 }],
  surging_entities: [{ entity_id: 2, label: "Qwen 3.5", entity_type: "model", score: 3.2, count: 14, baseline_mean: 2.1, sources: 5 }],
  events: { events_24h: 41, independent_events_24h: 612, event_members_24h: 140, singles_24h: 571, independent_events_prev_daily: 530.4 },
  built_at: ago(1),
  note: { day: new Date(NOW).toLocaleDateString("en-CA"), slot: 0, model: "gateway:auto", created_at: ago(1), lines: [
    { text: "直近 24 時間の記事は 980 件で、前の 7 日の 1 日平均 790 件を上回っています。", refs: [{ key: "F1", label: "直近 24 時間の記事は 980 件", kind: "stat" }] },
    { text: "Qwen 3.5 の話題が複数の出どころで大きなイベントになっています。", refs: [{ key: "V1", label: "イベント: Qwen 3.5 が公開", kind: "event", to: "/events/1" }, { key: "S1", label: "急に増えた物: Qwen 3.5", kind: "entity", to: "/entities/2" }] }] },
};
const ev = (id: number, over: Partial<EventRow>): EventRow => ({
  id, rep_id: id, title: `見本のイベント ${id}`, n_articles: 3, n_sources: 3, has_primary: false, first_at: ago(5), last_at: ago(1), summary: "", summary_model: "",
  entities: [{ id: 2, name: "Qwen 3.5", type: "model", n: 3 }, { id: 3, name: "Alibaba", type: "org", n: 2 }], ...over });
export const EVENTS: EventRow[] = [
  ev(1, { title: "Qwen 3.5 が公開された（公式・報道・日本語の記事）", n_articles: 6, n_sources: 5, has_primary: true, first_at: ago(4), summary: "Alibaba が Qwen 3.5 を公開し、複数の媒体が性能と対応環境を報じている。", summary_model: "gateway:auto" }),
  ev(2, { title: "llama.cpp b9000 の量子化の更新", n_articles: 3, n_sources: 2, first_at: ago(9) }), ev(3, { title: "NAS 向けの小さなサーバーが話題", n_articles: 2, n_sources: 2, first_at: ago(30), entities: [] }),
];
const EVENT_DETAIL: EventDetail = { ...EVENTS[0], hours_since_first: 4, origins: [{ origin: "official", label: "公式", n: 1 }, { origin: "press", label: "報道", n: 4 }, { origin: "community", label: "コミュニティ", n: 1 }],
  articles: [
    { id: 11, title: "Alibaba releases Qwen 3.5", url: "https://news.example.test/e/1", source: "Qwen Blog", source_key: "qwen", kind: "release", origin: "official", origin_label: "公式", published_at: ago(4), summary: "Qwen 3.5 is available today.", is_rep: true, is_dup: false },
    { id: 12, title: "Qwen 3.5 open weights land on Hugging Face", url: "https://news.example.test/e/2", source: "見本の報道 1", source_key: "p1", kind: "news", origin: "press", origin_label: "報道", published_at: ago(3.5), summary: "", is_rep: false, is_dup: false },
    { id: 13, title: "アリババ、Qwen 3.5 を公開", url: "https://news.example.test/e/3", source: "見本の報道 2", source_key: "p2", kind: "news", origin: "press", origin_label: "報道", published_at: ago(3), summary: "新しいモデルが公開された。", is_rep: false, is_dup: false },
    { id: 14, title: "Qwen 3.5 の量子化を試した", url: null, source: "見本のフォーラム", source_key: "f1", kind: "forum", origin: "community", origin_label: "コミュニティ", published_at: ago(1), summary: "", is_rep: false, is_dup: false },
  ] };

export const LINK_BASE: LinkStatus = {
  fingerprint: "1a2b-3c4d-5e6f-7a8b-9c0d", peers: [], secrets: [], pairing: null, allowed_secrets: ["openrouter"],
  jobs: { queued: 0, leased: 0, expired: 0, dead: 0, done_24h: 0, queued_by_kind: {} },
};
export const LINK_PEERS: LinkStatus["peers"] = [
  { id: 2, name: "見本の ControlDeck（書斎の PC。とても長い名前でも折り返して 320px に収まること）", fingerprint: "aaaa-bbbb-cccc-dddd-eeee", created_at: ago(30), last_seen_at: ago(0.1), revoked: false },
  { id: 1, name: "古い ControlDeck", fingerprint: "1111-2222-3333-4444-5555", created_at: ago(900), last_seen_at: "", revoked: false },
];

/** 知識グラフの見本（点 7・線 6。型・確かさ・状態がそろっている）。 */
const NODE = (id: number, name: string, type: string, n: number) => ({ id, name, type, article_count: n, sensitivity: "public", last_seen: ago(3) });
export const KG_NODES = [NODE(1, "llama.cpp", "software", 42), NODE(2, "Qwen 3.5", "model", 17), NODE(3, "Alibaba", "org", 11), NODE(5, "量子化", "tech", 5), NODE(9, "GGUF", "tech", 6), NODE(10, "vLLM", "software", 8),
  NODE(11, "ROCm（とても長い名前のついた技術の見本で、320px でも折り返して収まること）", "tech", 4)];
const EDGE = (id: number, source: number, target: number, type: string, confidence: number, status: string, directed: boolean, n: number, last = ago(24)) =>
  ({ id, source, target, type, directed, confidence, status, evidence_count: n, first_at: ago(900), last_at: last, sensitivity: "public" });
export const KG_EDGES = [EDGE(1, 2, 3, "part_of", 0.7, "extracted", true, 2), EDGE(2, 1, 2, "supports", 0.8, "extracted", true, 3), EDGE(3, 1, 5, "co_occurs", 0.5, "statistical", false, 12),
  EDGE(4, 2, 9, "uses", 0.3, "inferred", true, 1, ago(24 * 200)), EDGE(5, 1, 10, "competes", 0.6, "extracted", false, 2), EDGE(6, 10, 9, "supports", 0.4, "extracted", true, 1), EDGE(7, 1, 11, "uses", 0.6, "extracted", true, 1)];
const KG_HISTORY = { changes: 1, contradictions: 2, series: [{ kind: "price", entries: [
  { claim_id: 1, t: ago(24 * 40), value: "1000", text: "価格", title: "古い記事", url: "https://news.example.test/a/1", mark: "first", ai_extracted: true },
  { claim_id: 2, t: ago(24 * 20), value: "1200", text: "価格", title: "値上げの記事", url: "https://news.example.test/a/2", mark: "change", from: "1000", ai_extracted: true },
  { claim_id: 3, t: ago(24 * 2), value: "1500", text: "価格", title: "A 社の報道", url: "https://news.example.test/a/3", mark: "contradiction", ai_extracted: true },
  { claim_id: 4, t: ago(24 * 2 - 5), value: "1300", text: "価格", title: "B 社の報道", url: "https://news.example.test/a/4", mark: "contradiction", ai_extracted: true }] }] };

export interface MockState {
  authenticated: boolean; setupNeeded: boolean; setupTokenRequired?: boolean; username: string; keySet: boolean; prefs: NewsPrefs; settings: NewsRuntimeSettings;
  hidden: Set<number>; calls: { method: string; path: string; body: unknown }[]; unhandled: string[];
  /** 画像の無い記事だけの一覧にしたいとき等、一覧を差し替える */
  items: NewsItem[];
  restActive: boolean; diagProblems: boolean; update: UpdateStatus; analyticsEmpty?: boolean; gatewayKeySet: boolean; gatewayDown: boolean;
  /** ControlDeck との連携（つなぐ画面） */
  link: LinkStatus;
  /** 知識カードの利用者のメモ（PUT で更新される） */
  kgNote?: string;
}

export function newState(over: Partial<MockState> = {}): MockState {
  return { authenticated: true, setupNeeded: false, username: "admin", keySet: false, prefs: structuredClone(PREFS), settings: structuredClone(SETTINGS),
    hidden: new Set(), calls: [], unhandled: [], items: ITEMS, restActive: false, diagProblems: false, update: structuredClone(UPDATE_BASE), gatewayKeySet: false, gatewayDown: false, link: structuredClone(LINK_BASE), ...over };
}

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
// 1x1 の PNG（画像のある記事の見本）
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

const diagnostics = (state: MockState) => ({
  generated_at: ago(0),
  fetch: { total: 3, stalled: state.diagProblems ? 1 : 0, hit_rate: 80, hits_304: 8, conditional: 10, backoff: 1, sources: [
    { key: "rss:https://feed2.example.test/rss", name: "失敗中の取得先", kind: "rss", ok: false, reason: "http", error: "HTTP 500", last_ok_at: ago(30), last_at: ago(0.2), interval_min: 240, interval_auto: false, backoff: true, hit_rate: null, hits_304: 0, conditional: 0, new_7d: 0, fail_streak: 5, stalled: true, problems: ["失敗が 5 回続いています"], fix: "取得先の管理で URL を確かめるか、無効にしてください" },
    { key: "rss:https://feed1.example.test/rss", name: "見本の取得先 1", kind: "rss", ok: true, reason: "", error: "", last_ok_at: ago(0.2), last_at: ago(0.2), interval_min: 45, interval_auto: true, backoff: false, hit_rate: 80, hits_304: 8, conditional: 10, new_7d: 24, fail_streak: 0, stalled: false, problems: [], fix: "" }] },
  llm: { day: new Date(NOW).toLocaleDateString("en-CA"), calls: { prep: 12, digest: 2, gist: 0 }, total: 14, daily_limit: 800, minute_limit: 15, failed: 1, last_error: "HTTP 429", key_set: state.keySet,
    models: [{ model: "gateway:auto", ok: 5, fail: 1, success_pct: 83, rate_limited: 0, avg_ms: 38000, ja_pct: 95, quality_fail: 0, paid: false, provider: "gateway", last_ok_at: ago(0.3) },
      { model: "example/free-model:free", ok: 12, fail: 2, success_pct: 86, rate_limited: 1, avg_ms: 4200, ja_pct: 72, quality_fail: 1, paid: false, provider: "openrouter", last_ok_at: ago(0.1) },
      { model: "example/second-model:free", ok: 3, fail: 0, success_pct: 100, rate_limited: 0, avg_ms: 6100, ja_pct: 90, quality_fail: 0, paid: false, provider: "openrouter", last_ok_at: ago(2) }], rate_limited: 1, unprocessed: 7, prep_enabled: true, prep_model: "example/free-model:free",
    quota: state.settings.llm.quota, spare_used: 4, leftover: 730, night_from: 21, extract_enabled: true, extract_waiting: 33,
    chain: { gateway_down_in_seconds: 420, chain: ["example/free-model:free", "example/second-model:free"], current: "example/second-model:free", index: 1, return_in_seconds: 1500, events: [{ at: ago(0.4), kind: "switch", model: "example/second-model:free", reason: "example/free-model:free が直近 5 回で 3 回失敗（429）" }] },
    gateway: { enabled: true, base_set: true, key_set: true, calls: 6, model: "auto", batch: 10 },
    knowledge: { entities: 128, claims: 340, extracted_articles: 410 },
    events: { events: 41, summarized: 9, summary_waiting: 2, summary_gave_up: 1, trend_note_day: new Date(NOW).toLocaleDateString("en-CA"), trend_note_slot: 0 } },
  storage: {
    io: { rchar: 9_000_000, wchar: 350_000, read_bytes: 4_000_000, write_bytes: 1_048_576, syscw: 900 },
    series: Array.from({ length: 96 }, (_, i) => ({ t: new Date(NOW - (96 - i) * 900_000).toISOString(), write_bytes: i % 24 === 5 ? 524_288 : 0, read_bytes: 0, wchar: 0, cpu_pct: 0.1, rss: 120_000_000 })),
    last_flush: null, last_flush_at: ago(0.4), next_flush_at: new Date(NOW + 1_200_000).toISOString(), flush_minutes: 30, limits: { items: 1000, mb: 128 },
    unsaved: { items: 12, updates: 3, patches: 5, fetchlog: 4, states: 1, total: 25, bytes: 20_000, inflight: 0 }, flush_failures: 0, last_error: "",
    files: { db: 8_388_608, wal: 262_144, auth_db: 20_480, auth_wal: 0 }, disk: { free: 200_000_000_000, total: 500_000_000_000 },
    rest: { enabled: state.settings.rest.enabled, active: state.restActive, mode: state.settings.rest.mode, start: "01:00", end: "06:00", ends_at: state.restActive ? "2026-11-25T06:00:00" : null },
    rest_writes: state.restActive ? [{ at: ago(0.1), name: "prefs.save", user: true }] : [], rest_app_writes: 0, writes_by_name: { "prefs.save": 1 }, flush_history: [],
  },
  machine: { cpu_pct: 0.2, load: [0.1, 0.2, 0.2], rss: 126_000_000, uptime_seconds: 93_000, version: "0.1.1", python: "3.12", cpus: 2 },
  update: { version: state.update.version, install: state.update.install, can_install: state.update.can_install, checked_at: state.update.checked_at, error: state.update.error, latest: state.update.latest?.version ?? "",
    include_prerelease: state.update.include_prerelease, etag_hits: state.update.etag_hits, checks: state.update.checks, job: state.update.job.phase, last: state.update.last, rollback: state.update.rollback },
  search: { index: { available: true, building: false, cursor: 0, hi: 0, articles: 4210, docs: { tri: 4210, word: 4210 } },
    body: { enabled: true, per_day: 200, per_minute: 6, today: 38, ok: 120, failed: 31, reasons: { robots: 12, paywall: 8, short: 7, blocked: 4 },
      reason_labels: { robots: "robots.txt が許していません", paywall: "有料の記事です", short: "本文を取り出せませんでした（短い・空）", blocked: "アクセスが制限されています（ログイン・有料・拒否）" },
      raw_bytes: 1_800_000, packed_bytes: 520_000, cold: { items: 900, raw_bytes: 400_000, packed_bytes: 230_000 } }, compress_years: 1 },
  link: { peers: state.link.peers.filter((p) => !p.revoked).map((p) => ({ id: p.id, name: p.name, last_seen_at: p.last_seen_at })), last_seen_at: state.link.peers[0]?.last_seen_at ?? "", jobs: state.link.jobs,
    secrets: state.link.secrets.map((k) => ({ name: k.name, received_at: k.received_at })), change_seq: 120, artifacts: 3, research: 1 },
  problems: state.diagProblems ? [{ area: "fetch", level: "warn", text: "取得が止まった可能性のある取得先が 1 件あります" }] : [],
});

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

    if (is("GET", "/auth/status")) return json(route, { setup_needed: state.setupNeeded, setup_token_required: Boolean(state.setupNeeded && state.setupTokenRequired), authenticated: state.authenticated, username: state.authenticated ? state.username : null });
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

    if (is("GET", "/analytics/filters")) return json(route, ANA_FILTERS);
    if (is("GET", "/analytics/trend")) return json(route, state.analyticsEmpty ? { ...TREND, note: null, new_entities: [], surging_entities: [], channels: [] } : TREND);
    if (is("GET", "/events")) {
      const primary = url.searchParams.get("primary") === "true";
      const items = EVENTS.filter((e) => !primary || e.has_primary);
      return json(route, { items, total: items.length });
    }
    if (is("GET", /^\/events\/\d+$/)) return path.endsWith("/99") ? json(route, { detail: "そのイベントは見つかりません" }, 404) : json(route, { ...EVENT_DETAIL, id: Number(path.split("/")[2]) });
    if (is("GET", "/analytics/overview")) return json(route, state.analyticsEmpty ? { ...OVERVIEW, today: 0, today_indep: 0, last24h: 0, sources_today: 0, prev7_daily_avg: null, ratio_24h: null } : OVERVIEW);
    if (is("GET", "/analytics/series")) return json(route, state.analyticsEmpty ? { ...mockSeries(url.searchParams), series: [], total: 0 } : mockSeries(url.searchParams));
    if (is("GET", "/analytics/hot-topics")) return json(route, state.analyticsEmpty ? { ...HOT, enough_history: false, baseline_days: 1, top: [], new: [], fields: [] } : HOT);
    if (is("GET", "/analytics/sources")) return json(route, { range: url.searchParams.get("range") ?? "7d", days: DAYS7, sources: SOURCE_ROWS, total_sources: SOURCE_ROWS.length });
    if (is("GET", "/analytics/compare")) return json(route, COMPARE);
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
      groups: [{ id: "ai", label: "AI・LLM", hint: "", count: 3 }], default_interval: 30, reddit_key: false, limit: 200, interval_range: [15, 1440], formats: { rss: "RSS／Atom" }, origins: { official: "公式", press: "報道", blog: "ブログ", community: "コミュニティ", paper: "論文", other: "その他" }, running: false,
    });
    if (is("POST", "/news/sources/import/preview")) return json(route, { format: "csv", rows: IMPORT_ROWS, counts: { new: 2, duplicate: 1, invalid: 1 }, room: 400, total: 4 });
    if (is("POST", "/news/sources/import/commit")) return json(route, { added: (body as { rows: unknown[] }).rows.length, duplicate: 0, invalid: 0, failed: [], mode: (body as { mode: string }).mode });
    if (is("GET", "/news/sources/manage/detail")) return json(route, { item: managed(1), history: [{ at: ago(0.2), ok: true, status: 200, count: 20, new: 2, ms: 420, error: "", skipped: "" }], articles: [{ id: 1, title: ITEMS[0].title, url: ITEMS[0].url!, published_at: ITEMS[0].published_at }] });
    if (is("GET", "/news/presets")) return json(route, { groups: [{ id: "ai", label: "AI・LLM", hint: "", count: 1 }], items: [{ id: "p1", group: "ai", kind: "rss", name: "見本の目録", value: "https://preset.example.test/rss", lang: "ja", note: "", key: false, verified: "2026-11-24", recommended: true }], verified: "2026-11-24" });
    if (is("GET", "/news/gateway-key")) return json(route, { set: state.gatewayKeySet });
    if (is("PUT", "/news/gateway-key")) { state.gatewayKeySet = true; return json(route, { set: true }); }
    if (is("DELETE", "/news/gateway-key")) { state.gatewayKeySet = false; return json(route, { set: false }); }
    if (is("POST", "/news/gateway/test")) return json(route, state.gatewayDown ? { ok: false, models: [], ms: 12, error: "ゲートウェイに接続できません: ConnectError" } : { ok: true, models: ["auto", "local/model-a", "local/model-b"], ms: 85, error: "" });
    if (is("GET", "/entities")) {
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      const type = url.searchParams.get("type") ?? "";
      const days = Number(url.searchParams.get("days") ?? 0);
      const items = ENTITIES.filter((e) => (!q || e.name.toLowerCase().includes(q)) && (!type || e.type === type) && (!days || e.period_count > 0));
      return json(route, { items, total: items.length, types: ["model", "software", "org", "person", "tech", "product", "game", "policy", "place", "other"] });
    }
    if (is("GET", /^\/entities\/\d+$/)) return json(route, { ...ENTITY_DETAIL, id: Number(path.split("/")[2]) });
    if (is("GET", /^\/kg\/entities\/\d+\/card$/)) {
      const id = Number(path.split("/")[3]);
      const me = KG_NODES.find((n) => n.id === id) ?? KG_NODES[1];
      const rels = KG_EDGES.filter((e) => e.source === id || e.target === id).map((e) => {
        const other = KG_NODES.find((n) => n.id === (e.source === id ? e.target : e.source))!;
        return { ...e, direction: !e.directed ? "both" : e.source === id ? "out" : "in", other: { id: other.id, name: other.name, type: other.type }, status_label: e.status };
      });
      return json(route, { id: me.id, name: me.name, type: me.type, sensitivity: "public", ai_use: "allowed",
        overview: id === 2 ? { text: "Qwen 3.5 は Alibaba の公開モデルで、llama.cpp などの実行環境が対応を進めています。", model: "example/free-model:free", at: ago(10), ai: true } : null,
        elements: { aliases: ["Qwen3.5"], first_seen: ago(300), last_seen: ago(3), article_count: me.article_count, topics: ["量子化", "GGUF"] },
        timeline: [], relations: rels, history: id === 2 ? KG_HISTORY : { changes: 0, contradictions: 0, series: [] },
        claims: [{ id: 1, kind: "release", text: "Qwen 3.5 が公開された", value: "3.5", ai_extracted: true, model: "example/free-model:free", sensitivity: "public",
                   evidences: [{ document: { id: 1, kind: "news", title: "llama.cpp の新しい量子化が入りました", url: "https://news.example.test/a/1" }, quote: "Qwen 3.5 を公開", start: null, end: null, page: null, quote_hash: "x", grounded: true },
                               { document: { id: 2, kind: "paper", title: "論文の見本", url: "https://arxiv.example.test/abs/2401.00001" }, quote: "", start: 10, end: 40, page: 3, quote_hash: "", grounded: false }] }],
        note: { body: state.kgNote ?? "", updated_at: state.kgNote ? ago(0) : "" } });
    }
    if (is("PUT", /^\/kg\/entities\/\d+\/note$/)) { state.kgNote = (body as { body: string }).body; return json(route, { body: state.kgNote, updated_at: ago(0) }); }
    if (is("GET", "/graph/seeds")) return json(route, { items: KG_NODES.slice(0, 3) });
    if (is("POST", "/graph/neighbors")) {
      const b = body as { ids: number[]; types?: string[]; min_confidence?: number; limit?: number };
      const seeds = KG_NODES.filter((n) => b.ids.includes(n.id));
      const es = KG_EDGES.filter((e) => (b.ids.includes(e.source) || b.ids.includes(e.target)) && (!b.types?.length || b.types.includes(e.type)) && e.confidence >= (b.min_confidence ?? 0)).slice(0, b.limit ?? 60);
      const known = new Set(b.ids);
      const nodes = [...new Set(es.flatMap((e) => [e.source, e.target]))].filter((i) => !known.has(i)).map((i) => KG_NODES.find((n) => n.id === i)!);
      return json(route, { seeds, levels: [{ depth: 1, nodes, edges: es }], truncated: false });
    }
    if (is("POST", "/kg/export")) return route.fulfill({ status: 200, contentType: "application/zip", headers: { "X-Export-Files": "3" }, body: Buffer.from("PK\x05\x06" + "\0".repeat(18), "binary") });
    if (is("GET", "/news/settings")) return json(route, {
      settings: state.settings, weekdays: ["月", "火", "水", "木", "金", "土", "日"], interval_minutes: 30, prep_calls_today: 12, prep_batch: 30,
      rest_state: { enabled: state.settings.rest.enabled, active: state.restActive, mode: state.settings.rest.mode, ends_at: state.restActive ? "2026-11-25T06:00:00" : null },
      quota_labels: { prep: "下ごしらえと取り出し", gist: "要約", digest: "まとめ", card: "知識カード", trend: "傾向の解説", event: "イベントの要約", spare: "予備" },
      model_chain: { gateway_down_in_seconds: null, chain: ["example/free-model:free"], current: "example/free-model:free", index: 0, return_in_seconds: null, paid: [], events: [] },
      gateway_key_set: state.gatewayKeySet,
      rest_modes: { keep_memory: "集め続けてメモリに保持", stop_all: "集める・分析・保存をすべて休む", flush_at_limit: "集め続けて上限のときだけ一括で保存" },
      llm_stats: { day: new Date(NOW).toLocaleDateString("en-CA"), calls: { prep: 12, digest: 2, gist: 0 }, total: 14, daily_limit: state.settings.llm.daily_limit, minute_limit: state.settings.llm.minute_limit, failed: 1, last_error: "混み合っています（429）", key_set: state.keySet,
        quota: state.settings.llm.quota, spare_used: 4, night_from: 21, leftover: 730 },
    });
    if (is("PUT", "/news/settings")) { state.settings = body as NewsRuntimeSettings; return json(route, { settings: state.settings }); }
    if (is("GET", "/news/openrouter-key")) return json(route, { set: state.keySet });
    if (is("PUT", "/news/openrouter-key")) { state.keySet = true; return json(route, { set: true }); }
    if (is("DELETE", "/news/openrouter-key")) { state.keySet = false; return json(route, { set: false }); }
    if (is("POST", "/news/refresh")) return json(route, { started: true, reason: "", new: 2, failed: 0 });
    if (is("PUT", "/news/channels")) return json(route, body);

    if (is("GET", "/update")) return json(route, state.update);
    if (is("GET", "/update/notice")) return json(route, state.update.update_available && state.update.latest ? { available: true, version: state.update.latest.version, message: `新しい版 ${state.update.latest.version} があります（いまは ${state.update.version}）` } : { available: false, version: "", message: null });
    if (is("POST", "/update/check")) { state.update.checked_at = new Date().toISOString(); return json(route, state.update); }
    if (is("PUT", "/update/settings")) { state.update.include_prerelease = (body as { include_prerelease: boolean }).include_prerelease; return json(route, state.update); }
    if (is("POST", "/update/install") || is("POST", "/update/rollback")) {
      const kind = path.endsWith("rollback") ? "rollback" : "update";
      state.update.job = { phase: "downloading", kind, target: (body as { version: string }).version, message: "取得しています", percent: 40, started_at: new Date().toISOString() };
      return json(route, state.update);
    }
    if (is("GET", "/link")) return json(route, state.link);
    if (is("POST", "/link/pairing")) {
      const expires_at = new Date(Date.now() + 600_000).toISOString();
      state.link.pairing = { expires_at, wrong: 0 };
      return json(route, { code: "482913", expires_at, expires_in: 600, fingerprint: state.link.fingerprint, public_key: "AAAA" });
    }
    if (is("DELETE", "/link/pairing")) { state.link.pairing = null; return json(route, { ok: true }); }
    if (is("DELETE", /^\/link\/peers\/\d+$/)) { const id = Number(path.split("/").pop()); state.link.peers = state.link.peers.map((p) => (p.id === id ? { ...p, revoked: true } : p)); return json(route, { ok: true }); }
    if (is("GET", "/diagnostics")) return json(route, diagnostics(state));
    if (is("GET", "/diagnostics/notice")) return json(route, state.diagProblems ? { message: "取得が止まった可能性のある取得先が 1 件あります", level: "warn", count: 1 } : { message: null, level: null, count: 0 });
    if (is("POST", "/diagnostics/selfcheck")) return json(route, { at: ago(0), ok: false, checks: [
      { name: "DB の整合（PRAGMA quick_check）", ok: true, detail: "ok", fix: "" }, { name: "全文検索（FTS）の整合", ok: true, detail: "記事 6 件・索引 6 件", fix: "" },
      { name: "LLM の鍵", ok: state.keySet, detail: state.keySet ? "入っています" : "未設定です", fix: "設定の AI の欄で OpenRouter の鍵を入れてください" }, { name: "時計", ok: true, detail: "ずれなし", fix: "" }] });
    if (is("POST", "/diagnostics/search/rebuild")) return json(route, { queued: true, articles: 4210 });
    if (is("POST", "/diagnostics/flush")) return json(route, { ok: true, reason: "manual", items: 0 });
    state.unhandled.push(`${method} ${path}`);
    return json(route, { detail: "模擬にない API です" }, 404);
  });
}
