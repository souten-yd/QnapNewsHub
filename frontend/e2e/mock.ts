/** E2E の API の模擬（page.route）。画面は本物のバックエンドが配る build をそのまま使い、/api/v1 だけを差し替える。
 *  模擬に無い API が呼ばれたら 404 を返して unhandled に記録する（試験の終わりに空であることを確かめる）。 */
import type { Page, Route } from "@playwright/test";
import type { UpdateStatus, UpdateRelease } from "../src/api/update";
import type { LinkStatus } from "../src/api/link";
import type { AnalyticsFilters, Compare, HotTopics, Overview, Series, SourcesAnalysis, TrendStats } from "../src/api/analytics";
import type { EventDetail, EventRow } from "../src/api/events";
import type { WatchChanges, WatchRow } from "../src/api/watch";
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
  translate: { engine: "quickmt", threads: 2, batch: 32, beam: 2, cpu_isa: "", body_enabled: true, body_per_day: 100, body_max_chars: 20000, ondemand: true, hq_jobs: true, llm_wait_hours: 6,
    glossary: { prefill: "プリフィル" }, keep: [], max_en_ratio: 0.5 },
  llm: { digest_model: "example/free-model:free", gist_enabled: false, gist_model: "example/free-model:free", digest_focus: "", prep_enabled: true, prep_model: "example/free-model:free", prep_max_per_run: 100, daily_limit: 800, minute_limit: 15,
    extract_enabled: true, fallback_models: [], quota: { prep: 300, gist: 150, digest: 20, card: 150, trend: 10, event: 100, resolve: 0, spare: 70 }, night_from: 21, backfill_enabled: true,
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
  lan: { lan: true, discovery_port: 8788, answering: true, urls: ["http://192.0.2.10:8787", "http://198.51.100.7:8787"] },      // 文書用のアドレス（TEST-NET）
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

const DEGREE = (id: number) => KG_EDGES.filter((e) => e.source === id || e.target === id).length;
const GROUP_OF = (type: string, dir: string) => type === "part_of" ? (dir === "out" ? "upper" : dir === "in" ? "lower" : "peers") : type === "competes" || type === "co_occurs" ? "peers"
  : ["uses", "implements", "supports"].includes(type) ? (dir === "in" ? "used_by" : "uses") : type === "replaces" ? (dir === "in" ? "replaced_by" : "replaces") : type === "contradicts" ? "contradicts" : "other";
const GROUP_LABELS: [string, string][] = [["upper", "上位"], ["lower", "下位"], ["peers", "仲間"], ["uses", "使う"], ["used_by", "使われる"], ["replaces", "置き換える"], ["replaced_by", "置き換えられる"], ["contradicts", "矛盾"], ["other", "その他"]];
const DEFS: Record<number, string> = { 1: "ローカルで LLM を動かす C/C++ の推論エンジン。", 2: "Alibaba が公開する汎用の大規模言語モデル。", 3: "中国の IT 企業。", 5: "モデルの重みを小さい数値で表して軽くする技術。" };

/** 全体の地図の見本。小さい版は KG_NODES・KG_EDGES、bigMap は 1,500 点・3,500 本・24 のまとまり（決まった乱数なので毎回同じ）。 */
export function overviewMock(big: boolean | number, noLayout = false) {
  const N = typeof big === "number" ? big : 1500;      // bigMap に数を渡すと、その点数（性能の測定用。1,500 は既定）
  if (!big) {
    // 夜に計算して保存した配置の見本（重い llama.cpp が中心・小さな点が周り）。noLayout は夜の計算の前（x・y・importance なし）
    const SAVED: Record<number, [number, number, number]> = { 1: [0, 0, 1], 2: [120, 40, 0.62], 3: [260, 70, 0.35], 5: [-110, 60, 0.25], 9: [180, -90, 0.3], 10: [-80, -120, 0.42], 11: [-30, 150, 0.15] };
    const nodes = KG_NODES.map((n) => ({ id: n.id, name: n.name, type: n.type, article_count: n.article_count, degree: DEGREE(n.id), recent: 1, community: 1, ...(noLayout ? {} : { x: SAVED[n.id][0], y: SAVED[n.id][1], importance: SAVED[n.id][2] }) }));
    return { nodes, edges: KG_EDGES.map(({ first_at, sensitivity, ...e }) => e), isolated: [{ id: 4, name: "Radeon AI PRO R9700", type: "product", article_count: 6, degree: 0, recent: 0, community: 0 }],
      communities: [{ id: 1, name: "llama.cpp", size: 7, rep_id: 1, shown: 7, top: [{ id: 1, name: "llama.cpp" }, { id: 2, name: "Qwen 3.5" }] }], truncated: false, limit: 1500, layout: noLayout ? null : { version: 3, at: "2026-11-25T03:10:00Z", placed: 7 },
      totals: { with_relations: 7, edges: 7, shown_nodes: 7, shown_edges: 7 } };
  }
  let seed = 12345;
  const rnd = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };
  const types = ["model", "software", "org", "person", "tech", "product", "game", "policy", "place", "other"];
  const nodes = Array.from({ length: N }, (_, i) => ({ id: 1000 + i, name: `Things ${i + 1}`, type: types[i % types.length], article_count: Math.max(1, Math.floor(200 / (i + 1) ** 0.7)), degree: Math.max(1, Math.floor(80 / (i + 1) ** 0.6)), recent: 0, community: (i % 24) + 1,
    ...(() => { const c = i % 24, a = c * 2.4 + (i * 0.7), r = 160 + 60 * Math.sqrt(i + 1) * (0.5 + ((i * 7919) % 100) / 100); return { x: 900 * Math.sqrt(N / 1500) * Math.cos(c * 2.4) * Math.sqrt((c + 1) / 24) + r * Math.cos(a) * 0.35, y: 900 * Math.sin(c * 2.4) * Math.sqrt((c + 1) / 24) + r * Math.sin(a) * 0.35, importance: Math.min(1, 1 / (i + 1) ** 0.3) }; })() }));
  nodes[0].name = "Qwen 3.5"; nodes[1].name = "llama.cpp";
  const rels = ["uses", "supports", "part_of", "co_occurs", "competes", "implements"];
  const E = Math.round(N * 3500 / 1500);
  const edges = Array.from({ length: E }, (_, i) => { const a = Math.floor(rnd() ** 2 * N), c = a % 24; const b = (Math.floor(rnd() * Math.ceil(N / 24)) * 24 + c) % N;
    return { id: 10000 + i, source: 1000 + a, target: 1000 + (b === a ? (b + 24) % N : b), type: rels[i % rels.length], directed: i % 2 === 0, confidence: 0.3 + (i % 7) / 10, status: i % 5 === 0 ? "inferred" : "extracted", evidence_count: 1 + (i % 9), last_at: ago(24 * (i % 90)) }; });
  const communities = Array.from({ length: 24 }, (_, c) => ({ id: c + 1, name: nodes[c].name, size: Math.floor(N / 24), rep_id: nodes[c].id, shown: Math.floor(N / 24), top: [{ id: nodes[c].id, name: nodes[c].name }] }));
  return { nodes, edges, isolated: [], communities, truncated: true, limit: N, totals: { with_relations: 4604, edges: 23821, shown_nodes: N, shown_edges: E } };
}

const MOC_COMMUNITY = { kind: "community", id: 1, title: "llama.cpp のまとまり", size: 7,
  things: KG_NODES.slice(0, 5).map((n) => ({ id: n.id, name: n.name, type: n.type, article_count: n.article_count, last_seen: ago(3), definition: DEFS[n.id] ?? "" })),
  events: [{ id: 2, title: "llama.cpp b9000 の量子化の更新", n_articles: 3, last_at: ago(9), summary: "", things: ["llama.cpp"] }],
  questions: [{ entity_id: 2, entity: "Qwen 3.5", question: "長い文脈で品質が劣化するか？" }, { entity_id: 1, entity: "llama.cpp", question: "AMD GPU での速度は十分か？" }] };

/** connector の見本（backend の connectors.describe の形）。数字は見本で、実データではない。 */
const TERMS = (status: string, over: Record<string, string> = {}) => ({ id: "x", status, license: "メタデータは CC0（本体は著作権の対象）", storage: "メタデータだけ保存（本体は保存しない）", redistribution: "allowed", rate: "3 秒に 1 回以下・同時 1 接続",
  tos_url: "https://example.test/terms/api", notes: "見本の注意。公式を装わない。", checked_at: "2026-11-25", ai_use: "allowed", ...over });
const CONN = (id: string, label: string, kind: string, kind_label: string, over: Record<string, unknown> = {}) => ({ id, kind, kind_label, label, desc: `${label}の見本の説明。新しいものを見つけて、書誌だけ保存します。`, adapted: false, scheduled: true, enabled: false, interval_minutes: 1440,
  daily_items: 300, daily_mb: 10, settings: {}, has_key_option: false, has_key: false, key_required: false, terms_ack: false, needs_ack: false, terms: TERMS("confirmed"), usage: { day: "2026-11-25", items: 0, mb: 0, requests: 0 },
  budget_stop: null, last: null, last_ok_at: "", ...over });
export const CONNECTORS = [
  CONN("github", "GitHub の Release", "feed", "フィード", { adapted: true, enabled: true, daily_items: 2000, daily_mb: 50, has_key_option: true, terms: TERMS("confirmed", { license: "各リポジトリのライセンス", redistribution: "conditional", rate: "認証なし 60 回/時・トークンあり 5,000 回/時" }), usage: { day: "2026-11-25", items: 41, mb: 3.2, requests: 12 } }),
  CONN("hn", "Hacker News", "feed", "フィード", { adapted: true, enabled: true, terms: TERMS("unconfirmed", { license: "未確認", rate: "未確認", redistribution: "unknown" }), usage: { day: "2026-11-25", items: 60, mb: 0.4, requests: 3 } }),
  CONN("github_discover", "GitHub の発見・スターの急増", "discovery", "発見", { enabled: true, daily_items: 300, daily_mb: 10, has_key_option: true, settings: { topics: ["llm", "inference", "rocm", "gguf"], window_days: 7, min_stars: 50, per_topic: 15, watch_stars: true },
    usage: { day: "2026-11-25", items: 300, mb: 4.8, requests: 5 }, budget_stop: { at: new Date(NOW - 3600_000).toISOString(), reason: "1 日の件数の予算（300 件）に達したため止めています" },
    last: { at: new Date(NOW - 3600_000).toISOString(), ok: true, count: 300, error: "", skipped: "", ms: 4200, stopped: true } }),
  CONN("openalex", "OpenAlex（論文の書誌・引用数）", "api", "API", { enabled: true, has_key_option: true, settings: { filter: "primary_topic.field.id:17", search: "", days: 7, per_page: 25 }, usage: { day: "2026-11-25", items: 120, mb: 2.1, requests: 2 },
    terms: TERMS("confirmed", { license: "すべての記録は CC0", rate: "鍵なし 10 回/秒・無料の鍵は 1 日 $1 の予算" }), last: { at: new Date(NOW - 7200_000).toISOString(), ok: true, count: 25, error: "", skipped: "", ms: 900 } }),
  CONN("semanticscholar", "Semantic Scholar（引用・関連）", "discovery", "発見", { has_key_option: true, settings: { depth: 1, seeds: 3, per_paper: 15, second_level: 3 }, terms: TERMS("partial", { license: "付随のライセンス（CC BY-NC など）", redistribution: "conditional", rate: "鍵なし: 全利用者で共有・混むと絞られる" }) }),
  CONN("sitemap", "Sitemap（記事の URL）", "historical", "過去・一覧", { settings: { sites: [{ name: "見本のサイト", url: "https://news.example.test/sitemap.xml" }], max_urls: 200 }, terms: TERMS("per_site", { license: "取得先のサイトごと", redistribution: "unknown" }) }),
  CONN("boj", "日本銀行の時系列統計", "stats", "数値（統計）", { settings: { series: ["IR01:MADR1Z@D", "FM01:STRDCLUCON"], last_points: 24 }, terms: TERMS("confirmed", { license: "日本銀行の留意点に従う（クレジット表示）", rate: "短時間の高頻度のアクセスは禁止" }) }),
  CONN("estat", "e-Stat（政府統計の統計表）", "stats", "数値（統計）", { has_key_option: true, key_required: true, needs_ack: true, settings: { tables: [], limit: 200 }, terms: TERMS("unconfirmed", { license: "未確認", rate: "未確認", redistribution: "unknown" }) }),
  CONN("edinet", "EDINET（提出書類の知らせ）", "api", "API", { has_key_option: true, key_required: true, needs_ack: true, settings: { days: 1, sec_codes: [], limit: 100 }, terms: TERMS("unconfirmed", { license: "未確認", rate: "未確認", redistribution: "unknown" }) }),
  CONN("coingecko", "CoinGecko（価格）", "market", "数値（市場）", { has_key_option: true, key_required: true, settings: { coins: ["bitcoin", "ethereum"], vs: ["usd", "jpy"] }, terms: TERMS("partial", { license: "未確認（Demo は無料でレートが限られる）", redistribution: "unknown" }) }),
  CONN("hn_search", "HN Algolia の検索（道具）", "api", "API", { scheduled: false, needs_ack: true, terms: TERMS("unconfirmed", { license: "未確認", storage: "未確認", redistribution: "unknown", rate: "未確認" }) }),
];

export interface MockState {
  /** ウォッチリスト（POST/DELETE で変わる）。見た＝seen で変化は空になる */
  watch?: WatchRow[]; watchSeen?: boolean;
  authenticated: boolean; setupNeeded: boolean; setupTokenRequired?: boolean; username: string; keySet: boolean; prefs: NewsPrefs; settings: NewsRuntimeSettings;
  trModel: boolean; hidden: Set<number>; calls: { method: string; path: string; body: unknown }[]; unhandled: string[];
  /** 画像の無い記事だけの一覧にしたいとき等、一覧を差し替える */
  items: NewsItem[];
  restActive: boolean; diagProblems: boolean; update: UpdateStatus; analyticsEmpty?: boolean; gatewayKeySet: boolean; gatewayDown: boolean;
  /** ControlDeck との連携（つなぐ画面） */
  link: LinkStatus;
  connectors: typeof CONNECTORS;
  /** 知識カードの利用者のメモ（PUT で更新される） */
  kgNote?: string;
  /** 全体の地図を 1,500 点の大きい見本にする（性能の確認） */
  bigMap?: boolean | number;
  noLayout?: boolean;      // 夜の配置の計算の前（x・y・importance なしの全体の地図）
  /** 確定・却下したつなぐ候補の記事 id */
  decidedMentions: number[];
  /** 名寄せの候補の決めた操作（confirm:id・dismiss:id・undo:id） */
  mergeActions?: string[];
}

export function newState(over: Partial<MockState> = {}): MockState {
  return { authenticated: true, setupNeeded: false, username: "admin", keySet: false, prefs: structuredClone(PREFS), settings: structuredClone(SETTINGS),
    hidden: new Set(), calls: [], unhandled: [], items: ITEMS, restActive: false, diagProblems: false, trModel: false, update: structuredClone(UPDATE_BASE), gatewayKeySet: false, gatewayDown: false, link: structuredClone(LINK_BASE), connectors: structuredClone(CONNECTORS), decidedMentions: [], ...over };
}

const WATCH_CHANGES = (rows: WatchRow[], seen: boolean): WatchChanges => ({ generated_at: ago(0), watched: true, llm: false, changed: seen ? 0 : rows.length,
  items: rows.map((w) => seen ? { thing: w.entity, since: ago(0), claims: [], metrics: [], relations: [], versions: [], events: [], change_points: [], quiet: true } : {
    thing: w.entity, since: w.seen_at, quiet: false,
    claims: [{ id: 1, kind: "benchmark", text: "R9700 で Q4_K_M のデコードが 75 tok/s に向上する。", at: "2026-11-25", value: "75", cond: { hw: "R9700", quant: "Q4_K_M" }, src: "例の出どころ 2026-11-25 d3", grounded: true }],
    metrics: [{ subject: "repo:acme/llama.cpp", metric: "stars", value: 160, unit: "", at: "2026-11-25", cond: "daily", previous: 100, pct: 60 }],
    relations: [{ id: 4, type: "supports", subject: "llama.cpp", object: "Qwen 3.5", conf: 0.8, status: "extracted", at: "2026-11-25" }],
    versions: [{ doc: 9, kind: "release", title: "llama.cpp b9000 を公開", version: "b9000", at: "2026-11-25", src: "GitHub 2026-11-25 d9", url: "" }],
    events: [{ id: 1, title: "Qwen 3.5 が公開", n_articles: 6, n_sources: 5, has_primary: true, last_at: "2026-11-25" }],
    change_points: [{ kind: "mentions", direction: "up", line: "言及が急に増えた: 直近 3 日で 12 件（1 日 4 件。前の 4 週は 1 日 0.5 件）" }] }) });

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
    knowledge: { entities: 128, claims: 340, extracted_articles: 410, cards: 36, communities: 12, mention_candidates: 58 },
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
  translate: { engine: "quickmt", installed: true, loaded: true, threads: 2, batch: 32, beam: 2, cpu_isa: "", llm_state: "ok", body_per_day: 100,
    counts: { rows: { mt: 40, ja: 10 }, titles: 40, bodies: 6, today: 12, units: 300, fails: 9, fail_pct: 3.0, engines: { "quickmt-en-ja@c09e98b8:int8:beam2": 40 }, cache: 250 },
    run: { units: 120, cache_hits: 30, cache_hit_pct: 25, checked: 120, failed: 4, fail_pct: 3.3, reasons: { numbers: 3, english: 1 }, chars: 20000, seconds: 5, chars_per_sec: 4000, items: 12, ondemand: 1, load_ms: 440, load_error: "", last_at: ago(1), recent: [] },
    download: { phase: "done", message: "", done: 0, total: 0, file: "", error: "" } },
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
    if (is("GET", "/auth/hub")) return json(route, { host: "127.0.0.1", port: 8787, cookie_secure: false, discovery_port: 8788 });
    if (is("PUT", "/auth/hub")) return json(route, { ...(body as object), restart_needed: true });

    if (path.startsWith("/watch")) {
      const rows = state.watch ?? (state.watch = []);
      if (is("GET", "/watch")) return json(route, { items: rows });
      if (is("POST", "/watch")) {
        const id = (body as { entity_id: number }).entity_id;
        const e = ENTITIES.find((x) => x.id === id) ?? ENTITIES[0];
        if (!rows.some((w) => w.entity.id === e.id)) rows.push({ id: rows.length + 1, entity: { id: e.id, name: e.name, type: e.type }, created_at: ago(24), seen_at: ago(24) });
        state.watchSeen = false;
        return json(route, rows[rows.length - 1]);
      }
      if (is("DELETE", /^\/watch\/\d+$/)) { const id = Number(path.split("/")[2]); state.watch = rows.filter((w) => w.entity.id !== id); return json(route, { removed: true }); }
      if (is("POST", "/watch/seen")) { state.watchSeen = true; return json(route, { marked: rows.length }); }
      if (is("GET", "/watch/changes")) return json(route, WATCH_CHANGES(rows, Boolean(state.watchSeen)));
    }
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
    if (is("GET", /^\/(kg\/entities|things)\/\d+\/card$/)) {
      const id = Number(path.split("/")[path.startsWith("/things") ? 2 : 3]);
      const me = KG_NODES.find((n) => n.id === id) ?? KG_NODES[1];
      const rels = KG_EDGES.filter((e) => e.source === id || e.target === id).map((e) => {
        const other = KG_NODES.find((n) => n.id === (e.source === id ? e.target : e.source))!;
        return { ...e, direction: !e.directed ? "both" : e.source === id ? "out" : "in", other: { id: other.id, name: other.name, type: other.type }, status_label: e.status };
      });
      const groups = GROUP_LABELS.map(([key, label]) => ({ key, label, items: rels.filter((r) => GROUP_OF(r.type, r.direction) === key).map((r) => ({ ...r, other: { ...r.other, definition: DEFS[r.other.id] ?? "" } })) }))
        .filter((g) => g.items.length).map((g) => ({ ...g, count: g.items.length, more: 0 }));
      const rich = id === 2;
      return json(route, { id: me.id, name: me.name, type: me.type, sensitivity: "public", ai_use: "allowed",
        brief: { definition: rich ? DEFS[2] : "", type: me.type, state: rich ? { key: "official", label: "正式公開" } : { key: "unknown", label: "不明" }, confidence: me.article_count > 10 ? 0.97 : 0.75, confidence_label: me.article_count > 10 ? "高" : "中",
          aliases: rich ? ["Qwen3.5", "通義千問 3.5"] : [], evidence_count: me.article_count, latest_evidence_at: ago(3), checked_at: rich ? ago(10) : "" },
        groups, questions: rich ? ["長い文脈で品質が劣化するか？", "AMD GPU での量子化の精度差はどれくらいか？"] : [], opposing: rich ? ["Alibaba の別モデルのほうが日本語に強いとの見方もある。"] : [],
        metrics: rich ? [{ source: "claim", kind: "benchmark", label: "R9700 で Q4_K_M の生成速度", value: "25 tok/s", unit: "", at: "", url: "https://news.example.test/a/1" }, { source: "metric", kind: "stars", label: "stars（daily）", value: "1200", unit: "count", at: ago(24), url: "" }] : [],
        mentions: rich ? [{ article_id: 3, title: "Qwen 3.5 の量子化を試した記録", url: "https://news.example.test/a/3", source: "forum-a", matched: "Qwen 3.5", published_at: ago(30) },
                          { article_id: 4, title: "Qwen 3.5 vs Gemma の比較", url: "https://news.example.test/a/4", source: "forum-a", matched: "Qwen 3.5", published_at: ago(50) }].filter((m) => !state.decidedMentions.includes(m.article_id)) : [],
        overview: rich ? { text: "Qwen 3.5 は Alibaba の公開モデルで、llama.cpp などの実行環境が対応を進めています。", model: "example/free-model:free", at: ago(10), ai: true } : null,
        elements: { aliases: ["Qwen3.5"], first_seen: ago(300), last_seen: ago(3), article_count: me.article_count, topics: ["量子化", "GGUF"] },
        timeline: [], relations: rels, history: id === 2 ? KG_HISTORY : { changes: 0, contradictions: 0, series: [] },
        claims: [{ id: 1, kind: "release", text: "Qwen 3.5 が公開された", value: "3.5", ai_extracted: true, model: "example/free-model:free", sensitivity: "public",
                   evidences: [{ document: { id: 1, kind: "news", title: "llama.cpp の新しい量子化が入りました", url: "https://news.example.test/a/1" }, quote: "Qwen 3.5 を公開", start: null, end: null, page: null, quote_hash: "x", grounded: true },
                               { document: { id: 2, kind: "paper", title: "論文の見本", url: "https://arxiv.example.test/abs/2401.00001" }, quote: "", start: 10, end: 40, page: 3, quote_hash: "", grounded: false }] }],
        note: { body: state.kgNote ?? "", updated_at: state.kgNote ? ago(0) : "" } });
    }
    if (is("POST", /^\/things\/\d+\/mentions\/\d+\/(confirm|dismiss)$/)) { state.decidedMentions.push(Number(path.split("/")[4])); return json(route, { status: path.endsWith("confirm") ? "confirmed" : "dismissed" }); }
    if (is("GET", "/things/merge-candidates")) {
      const done = state.mergeActions ?? [];
      return json(route, { total: 1, items: [{ id: 7, reason: "rule_type", score: 0.8, keep: { id: 3, name: "VRChat", type: "software", article_count: 6 }, drop: { id: 9, name: "VRChat Inc.", type: "org", article_count: 1 } }],
        merges: [{ id: 1, winner: { id: 1, name: "Sony" }, loser: { id: 5, name: "Sony Corporation" }, mode: "auto", reason: "rule", merged_at: ago(5), undone: done.includes("undo:1"), can_undo: !done.includes("undo:1") },
          { id: 2, winner: { id: 2, name: "OpenAI" }, loser: { id: 6, name: "オープンエーアイ" }, mode: "wikidata", reason: "Q21708200", basis: { stage: "wikidata", qid: "Q21708200", description: "アメリカの人工知能研究機関" }, merged_at: ago(4), undone: false, can_undo: true },
          { id: 3, winner: { id: 3, name: "Mistral AI" }, loser: { id: 8, name: "ミストラル" }, mode: "llm", reason: "llm 0.93", basis: { stage: "llm", confidence: 0.93, reason: "英語名の日本語表記" }, merged_at: ago(3), undone: false, can_undo: true }] });
    }
    if (is("POST", /^\/things\/(merge-candidates\/\d+\/(confirm|dismiss)|merges\/\d+\/undo)$/)) { const p = path.split("/"); (state.mergeActions ??= []).push(`${p[2] === "merges" ? "undo" : p[4]}:${p[2] === "merges" ? p[3] : p[3]}`); return json(route, { status: "ok" }); }
    if (is("GET", "/things/map/overview")) return json(route, overviewMock(state.bigMap ?? false, Boolean(state.noLayout)));
    if (is("GET", /^\/things\/map\/community\/\d+$/)) { const o = overviewMock(state.bigMap ?? false); const id = Number(path.split("/")[4]); const ns = o.nodes.filter((n) => n.community === id).slice(0, 300); const ids = new Set(ns.map((n) => n.id));
      return json(route, { id, name: o.communities[0].name, size: ns.length, nodes: ns, edges: o.edges.filter((e) => ids.has(e.source) && ids.has(e.target)) }); }
    if (is("GET", "/things/moc")) return json(route, { communities: [{ id: 1, name: "llama.cpp", size: 7 }, { id: 2, name: "Alibaba", size: 4 }], channels: [{ id: "local-llm", name: "ローカル LLM", color: "#f5c518" }, { id: "amd-gpu", name: "AMD・GPU", color: "#38bdf8" }] });
    if (is("GET", /^\/things\/moc\/community\/\d+$/)) return json(route, MOC_COMMUNITY);
    if (is("GET", /^\/things\/moc\/channel\/[\w-]+$/)) return json(route, { ...MOC_COMMUNITY, kind: "channel", id: path.split("/")[4], title: "チャンネル「ローカル LLM」", size: 38 });
    if (is("PUT", /^\/kg\/entities\/\d+\/note$/)) { state.kgNote = (body as { body: string }).body; return json(route, { body: state.kgNote, updated_at: ago(0) }); }
    if (is("GET", "/graph/seeds")) return json(route, { items: KG_NODES.slice(0, 3) });
    if (is("POST", "/graph/neighbors")) {
      const b = body as { ids: number[]; types?: string[]; min_confidence?: number; limit?: number; depth?: number };
      const withDeg = (n: (typeof KG_NODES)[number]) => ({ ...n, degree: DEGREE(n.id), last_edge_at: ago(24) });
      const seeds = KG_NODES.filter((n) => b.ids.includes(n.id)).map(withDeg);
      const known = new Set(b.ids);
      const levels: { depth: number; nodes: unknown[]; edges: unknown[] }[] = [];
      let frontier = [...known];
      const used = new Set<number>();
      for (let d = 1; d <= (b.depth ?? 1) && frontier.length; d += 1) {
        const es = KG_EDGES.filter((e) => !used.has(e.id) && (frontier.includes(e.source) || frontier.includes(e.target)) && (!b.types?.length || b.types.includes(e.type)) && e.confidence >= (b.min_confidence ?? 0)).slice(0, b.limit ?? 60);
        es.forEach((e) => used.add(e.id));
        const nodes = [...new Set(es.flatMap((e) => [e.source, e.target]))].filter((i) => !known.has(i)).map((i) => withDeg(KG_NODES.find((n) => n.id === i)!));
        nodes.forEach((n) => known.add(n.id));
        levels.push({ depth: d, nodes, edges: es });
        frontier = nodes.map((n) => n.id);
      }
      return json(route, { seeds, levels, truncated: false });
    }
    if (is("POST", "/kg/export")) return route.fulfill({ status: 200, contentType: "application/zip", headers: { "X-Export-Files": "3" }, body: Buffer.from("PK\x05\x06" + "\0".repeat(18), "binary") });
    if (is("GET", "/news/settings")) return json(route, {
      settings: state.settings, weekdays: ["月", "火", "水", "木", "金", "土", "日"], interval_minutes: 30, prep_calls_today: 12, prep_batch: 30,
      rest_state: { enabled: state.settings.rest.enabled, active: state.restActive, mode: state.settings.rest.mode, ends_at: state.restActive ? "2026-11-25T06:00:00" : null },
      quota_labels: { prep: "下ごしらえと取り出し", gist: "要約", digest: "まとめ", card: "知識カード", trend: "傾向の解説", event: "イベントの要約", resolve: "名寄せの判定", spare: "予備" },
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
    if (is("GET", "/connectors")) return json(route, { connectors: state.connectors, kinds: {}, promotion: { channel: true, hot: true, watch: true, surge: true, derivative: true, surge_stars: 100 },
      tiers: { news: { "1": 3200, "2": 410, "3": 880 }, paper: { "0": 40, "1": 120 }, repo: { "1": 35 }, other: { "0": 600 } }, tier_labels: { "0": "L0 発見", "1": "L1 概要", "2": "L2 本文", "3": "L3 知識", "4": "L4 分析" },
      rest: state.restActive, limits: { interval: [5, 10080], daily_items: 100000, daily_mb: 4096 } });
    if (is("PUT", "/connectors/promotion")) return json(route, { promotion: body });
    if (is("PUT", /^\/connectors\/[a-z_]+$/)) {
      const id = path.split("/").pop();
      const row = state.connectors.find((c) => c.id === id);
      if (!row) return json(route, { detail: "知らない connector です" }, 404);
      const b = body as { enabled?: boolean; terms_ack?: boolean; daily_items?: number; daily_mb?: number; interval_minutes?: number };
      if (b.enabled && row.key_required && !row.has_key) return json(route, { detail: "鍵が要ります" }, 422);
      if (b.enabled && row.needs_ack && !(b.terms_ack || row.terms_ack)) return json(route, { detail: "利用条件が未確認の connector です" }, 422);
      Object.assign(row, Object.fromEntries(Object.entries(b).filter(([, v]) => v !== undefined)));
      return json(route, row);
    }
    if (is("PUT", /^\/connectors\/[a-z_]+\/key$/)) { const row = state.connectors.find((c) => c.id === path.split("/")[2]); if (row) row.has_key = true; return json(route, { has_key: true }); }
    if (is("DELETE", /^\/connectors\/[a-z_]+\/key$/)) { const row = state.connectors.find((c) => c.id === path.split("/")[2]); if (row) row.has_key = false; return json(route, { has_key: false }); }
    if (is("POST", /^\/connectors\/[a-z_]+\/run$/)) return json(route, { ran: true, reason: "", count: 12, error: "", stopped: false, note: "" });
    if (is("GET", "/link")) return json(route, state.link);
    if (is("POST", "/link/pairing")) {
      const expires_at = new Date(Date.now() + 600_000).toISOString();
      state.link.pairing = { expires_at, wrong: 0 };
      return json(route, { code: "482913", expires_at, expires_in: 600, fingerprint: state.link.fingerprint, public_key: "AAAA" });
    }
    if (is("DELETE", "/link/pairing")) { state.link.pairing = null; return json(route, { ok: true }); }
    if (is("DELETE", /^\/link\/peers\/\d+$/)) { const id = Number(path.split("/").pop()); state.link.peers = state.link.peers.map((p) => (p.id === id ? { ...p, revoked: true } : p)); return json(route, { ok: true }); }
    if (is("GET", "/translate/status")) return json(route, {
      engine: "quickmt", untranslated_mark: "〔未訳〕", default_glossary: { quantization: "量子化", prefill: "プリフィル", inference: "推論" }, llm_state: "ok",
      model: { id: "quickmt-en-ja", title: "QuickMT 英→日", repo: "quickmt/quickmt-en-ja", rev: "c09e98b8", license: "CC-BY-4.0", license_note: "モデルは CC-BY-4.0（商用・再配布可・帰属表示）", size: 403_600_000, installed: state.trModel, loaded: state.trModel },
      download: { phase: "idle", message: "", done: 0, total: 0, file: "", error: "" }, stats: { units: 0, cache_hit_pct: null, fail_pct: null, chars_per_sec: 0, load_ms: 0, load_error: "" }, counts: { titles: 0, bodies: 0, today: 0, fail_pct: null, cache: 0 } });
    if (is("POST", "/translate/model")) { state.trModel = true; return json(route, { started: true }); }
    if (is("DELETE", "/translate/model")) { state.trModel = false; return json(route, { removed: true }); }
    if (is("POST", "/translate/try")) return json(route, { ja: "量子化はプリフィルの速度を改善します。", units: 2, failed: 0 });
    if (is("GET", "/translate/articles/1")) return json(route, { translated: true, title_ja: "新しい量子化", summary_ja: "", body_ja: "", engine: "mock@1", hq: false, partial: false, untranslated_mark: "〔未訳〕" });
    if (is("POST", "/translate/articles/1")) return json(route, { translated: true, title_ja: "新しい量子化", summary_ja: "", body_ja: "本文の訳です。\n〔未訳〕An English sentence.", engine: "mock@1", hq: false, partial: true, untranslated_mark: "〔未訳〕" });
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
