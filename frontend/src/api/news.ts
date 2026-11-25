/** ニュースの専門ページ API の型と取得。backend/qnapnewshub/router.py と対になる。 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

export type NewsKind = "release" | "news" | "model" | "forum" | "reddit" | "event";
export type NewsAction = "open" | "like" | "dislike" | "hide" | "star";
export type NewsState = Record<NewsAction, boolean>;
export type NewsView = "recommended" | "latest" | "starred";

export interface NewsItem {
  id: number; kind: NewsKind; source: string; source_key: string; title: string; url: string | null;
  summary: string; image_url: string | null; image_ai?: boolean; published_at: string; topics: string[]; state: NewsState; score?: number;
  /** チャンネルの id（どのチャンネルに属するか。無ければ null） */
  channel?: string | null;
  /** 英語の抜粋を日本語に訳したもの（summary がその訳になり、元の抜粋は summary_original） */
  translated?: boolean; summary_original?: string; has_body?: boolean;
  /** 機械翻訳（QuickMT など。translate.py）。題・概要の訳と、エンジン・高品質の訳か・訳せなかった文があるか */
  mt?: MachineTranslation; mt_summary?: boolean;
  /** 同じ記事の別の出どころの数（代表の記事だけ。一覧では 1 件にまとめて出す）と、その出どころの名前 */
  also?: number; also_sources?: string[];
  /** 同じ出来事（イベント）の記事を 1 つにまとめたとき: イベントの id・ほかの記事の数・出どころの数・一次情報の有無 */
  event?: { id: number; others: number; sources: number; primary: boolean };
  /** 要素の取り出し（AI）で取れたThingsの札（記事の数が多い順に最大 4）。押すとThingsのページ */
  entities?: EntityTag[];
}
export interface MachineTranslation { title: string; summary: string; engine: string; hq: boolean; at: string; body: boolean; partial: boolean }
export interface EntityTag { id: number; name: string; type: EntityType }
export interface NewsPage { items: NewsItem[]; next: string | null }
export interface NewsQuery { view: NewsView; kinds: string; q: string; date: string; topic: string; source: string; channel?: string }

/** チャンネル＝検索テーマ（backend/qnapnewshub/channels.py）。 */
export interface NewsChannel {
  id: string; name: string; color: string; include: string[]; exclude: string[]; kinds: string[];
  lang: "ja" | "en" | "both"; limit: number; search: boolean;
}
export interface ChannelsConfig { channels: NewsChannel[]; archive_keep: number; interval_minutes: number }
export interface ChannelsResponse extends ChannelsConfig {
  kinds: Record<string, string>; reddit_key: boolean; limits: { channels: number; terms: number; palette: string[] };
}

/** 表示のデザインと好み（利用者ごと）。 */
export type NewsDesign = "broadcast" | "magazine" | "newspaper" | "finance" | "text";
/** 旧い名前の移し替え: 保存済みの「紙面」は経済紙、「コンパクト」はテキスト一覧として扱う。 */
export const LEGACY_DESIGNS: Record<string, NewsDesign> = { paper: "finance", compact: "text" };
export interface NewsDisplay {
  design: NewsDesign; accent: string; font_size: "s" | "m" | "l"; density: "relaxed" | "normal" | "tight"; images: boolean;
  columns: "auto" | "1" | "2" | "3"; ticker: boolean; onair: boolean; numbers: boolean; reduce_motion: boolean;
  /** ホームのニュースのカードに出す記事の数（1〜10） */
  home_items: number;
}
export interface NewsTaste {
  strength: number; use_chat: boolean; half_life_days: number; mute_terms: string[]; mute_sources: string[]; dim_read: boolean;
}
export interface NewsPrefs { display: NewsDisplay; taste: NewsTaste }
export const DEFAULT_PREFS: NewsPrefs = {
  display: { design: "broadcast", accent: "", font_size: "m", density: "normal", images: true, columns: "auto", ticker: true, onair: true, numbers: true, reduce_motion: false, home_items: 5 },
  taste: { strength: 0.5, use_chat: true, half_life_days: 30, mute_terms: [], mute_sources: [], dim_read: true },
};

/** GET /news/home: 放送局の画面が 1 回で読み込む内容。 */
export interface HomeSection { id: string; name: string; color: string; number: number; total: number; week: number; items: NewsItem[] }
export interface NewsHome {
  channels: { id: string; name: string; color: string; number: number }[];
  onair: NewsItem | null;
  ticker: { id: number; title: string; source: string; url: string | null; published_at: string }[];
  now: NewsItem[]; today: NewsItem[]; week: NewsItem[];
  sections: HomeSection[];
  interest: NewsProfile; trends: NewsTrends;
  numbers: { today: number; read_today: number; read_30d: number; top_channel: { id: string; name: string; color: string; reads: number } | null; top_topic: string | null; top_topic_count: number };
  generated_at: string;
}
export interface ArchiveDays { days: { date: string; count: number }[]; total: number; starred: number }
export interface NewsTrends {
  days: string[]; topics: { topic: string; counts: number[]; total: number }[]; sources: { source: string; count: number }[];
}
export interface NewsProfile {
  topics: { topic: string; weight: number }[];
  sources: { source_key: string; source: string; weight: number }[];
  avoided_sources: { source_key: string; source: string; weight: number }[];
  muted: string[]; chat_terms: number;
}
export interface NewsConfig {
  enabled: Record<string, boolean>; github: string[]; codeberg: string[]; rss: { name: string; url: string }[];
  /** RSS が無いサイト: 記事の一覧のページを巡回する（robots.txt を守る・1 時間に 1 回・30 件まで） */
  pages: { name: string; url: string }[];
  hackernews: boolean; huggingface: boolean; discourse: string[]; reddit: string[];
  /** ソースごとの取得の間隔（分。{source_key: 分}）と、止めているソース。無いソースは設定の既定の間隔 */
  intervals?: Record<string, number>; disabled?: string[];
}
/** 設定 → ニュース → ソース →「プリセット」（backend/qnapnewshub/presets.py）。 */
export interface NewsPreset {
  id: string; group: string; also?: string[]; kind: "rss" | "github" | "codeberg" | "discourse" | "reddit"; name: string; value: string;
  lang: "ja" | "en"; note: string; key: boolean; verified: string;
  /** 取得の間隔の既定（分。0 は設定の既定）・おすすめ・確かめたときの最新の記事の日と件数・365 日基準で入れた公式（stale） */
  interval?: number; recommended?: boolean; latest?: string; count?: number; stale?: boolean;
}
export interface NewsExcluded { group: string; name: string; value: string; reason: string }
export interface NewsPresets { groups: { id: string; label: string; hint: string; count: number }[]; items: NewsPreset[]; verified: string; excluded?: NewsExcluded[] }
export interface SourceStatus {
  source_key: string; label: string; kind: string; ok: boolean; skipped: string; error: string; count: number; at: string;
  /** 最後に取れた時刻と、最後の失敗の理由（今は取れていても残る） */
  ok_at?: string; last_error?: string;
}
/** 通信確認・サイトの発見（backend/qnapnewshub/probe.py）。 */
export interface CheckResult {
  ok: boolean; count: number; titles: string[]; ms: number; status: number | null; latest: string;
  error: { code: string; message: string; status: number | null; location: string } | null;
}
export interface DiscoverFeed { url: string; count: number; latest: string; titles: string[] }
export interface DiscoverResult {
  url: string; title: string; mode: "feed" | "feeds" | "page" | "none"; feeds: DiscoverFeed[];
  page: { count: number; titles: string[]; robots: boolean } | null; error: { code: string; message: string } | null;
}
export interface NewsSources {
  config: NewsConfig; kinds: Record<string, string>; reddit_key: boolean; limit: number;
  sources: SourceStatus[]; last: { finished: string | null; new: number }; running: boolean;
}
/** 軽い翻訳の設定（backend/qnapnewshub/settings.py の translate）。glossary は対訳表（英語→日本語）、keep はそのまま残す語 */
export interface TranslateSettings {
  engine: "quickmt" | "off"; threads: number; batch: number; beam: number; cpu_isa: string; body_enabled: boolean; body_per_day: number; body_max_chars: number;
  ondemand: boolean; hq_jobs: boolean; llm_wait_hours: number; glossary: Record<string, string>; keep: string[]; max_en_ratio: number;
}
/** 更新・LLM の設定（backend/qnapnewshub/settings.py）。 */
export interface NewsRuntimeSettings {
  feed_refresh_minutes: number;
  digest_daily: { enabled: boolean; time: string };
  digest_weekly: { enabled: boolean; weekday: number; time: string };
  quiet: { enabled: boolean; start: string; end: string };
  rest: { enabled: boolean; start: string; end: string; mode: "keep_memory" | "stop_all" | "flush_at_limit" };
  storage: { flush_minutes: number; max_items: number; max_mb: number; log_file: boolean; access_log: boolean; compress_years: number };
  body: { enabled: boolean; per_day: number; per_minute: number };
  translate: TranslateSettings;
  llm: { digest_model: string; gist_enabled: boolean; gist_model: string; digest_focus: string; prep_enabled: boolean; prep_model: string; prep_max_per_run: number; daily_limit: number; minute_limit: number;
    extract_enabled: boolean; fallback_models: string[]; quota: Record<string, number>; night_from: number; backfill_enabled: boolean; gateway: GatewaySettings };
}
/** ControlDeck のゲートウェイ（OpenAI 互換）。鍵は別（暗号化して保存。画面・応答に出ない） */
export interface GatewaySettings { enabled: boolean; base_url: string; model: string; first: boolean; batch: number; timeout_seconds: number; minute_limit: number; cooldown_minutes: number }
/** 今日の LLM の呼び出し（backend の llm.stats）。鍵の本文は含まない（key_set は入っているかだけ）。 */
export interface LlmStats {
  day: string; calls: Record<string, number>; total: number; daily_limit: number; minute_limit: number; failed: number; last_error: string; key_set: boolean;
  quota: Record<string, number>; spare_used: number; night_from: number; leftover: number;
}
export interface ModelChainStatus {
  gateway_down_in_seconds?: number | null;
  chain: string[]; current: string; index: number; return_in_seconds: number | null; paid: string[];
  events: { at: string; kind: "switch" | "return" | "down"; model: string; reason: string }[];
}
export interface NewsSettingsResponse { settings: NewsRuntimeSettings; weekdays: string[]; interval_minutes: number; prep_calls_today?: number; llm_stats: LlmStats; prep_batch: number; rest_state?: { enabled: boolean; active: boolean; mode: string; ends_at: string | null }; rest_modes?: Record<string, string>; quota_labels?: Record<string, string>; model_chain?: ModelChainStatus; gateway_key_set?: boolean }
/** 日刊・週刊のまとめ。body が空で error があるのは「作らなかった」回（外部への送信が許可されていない等）。fallback は LLM なしの見出しの一覧。 */
export interface Digest {
  id: number; period: "daily" | "weekly"; label: string; day: string; body: string; model: string; created_at: string;
  error: string; fallback: boolean; item_ids: number[];
  sources?: { id: number; n: number; title: string; source: string; url: string | null; topics?: string[] }[];
}
export interface DigestsResponse { latest: { daily: Digest | null; weekly: Digest | null }; digests: Digest[] }
export interface RefreshResult { started: boolean; reason: string; new?: number; failed?: number }

const key = ["news"] as const;

export function useNewsItems(query: NewsQuery, enabled = true, refetchMinutes = 0) {
  return useInfiniteQuery({
    queryKey: [...key, "items", query],
    enabled,
    refetchInterval: refetchMinutes > 0 ? refetchMinutes * 60_000 : false,
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams({ view: query.view, limit: "24" });
      if (query.kinds) p.set("kind", query.kinds);
      if (query.q.trim()) p.set("q", query.q.trim());
      if (query.date) p.set("date", query.date);
      if (query.topic) p.set("topic", query.topic);
      if (query.source) p.set("source", query.source);
      if (query.channel) p.set("channel", query.channel);
      if (pageParam) p.set("before", pageParam);
      return api<NewsPage>(`/news/items?${p}`);
    },
    getNextPageParam: (last) => last.next ?? undefined,
    staleTime: 30_000,
  });
}

export const useNewsArchive = (enabled = true) =>
  useQuery({ queryKey: [...key, "archive"], queryFn: () => api<ArchiveDays>("/news/archive"), enabled, staleTime: 60_000 });
export const useNewsTrends = () =>
  useQuery({ queryKey: [...key, "trends"], queryFn: () => api<NewsTrends>("/news/trends?days=14"), staleTime: 60_000 });
export const useNewsProfile = () =>
  useQuery({ queryKey: [...key, "profile"], queryFn: () => api<NewsProfile>("/news/profile"), staleTime: 30_000 });
export const useNewsHome = () =>
  useQuery({ queryKey: [...key, "home"], queryFn: () => api<NewsHome>("/news/home"), staleTime: 30_000 });
export const useNewsChannels = () =>
  useQuery({ queryKey: [...key, "channels"], queryFn: () => api<ChannelsResponse>("/news/channels"), staleTime: 15_000 });
export const useNewsPrefs = () =>
  useQuery({ queryKey: [...key, "prefs"], queryFn: () => api<NewsPrefs>("/news/prefs"), staleTime: 60_000 });
export const useNewsSources = () =>
  useQuery({ queryKey: [...key, "sources"], queryFn: () => api<NewsSources>("/news/sources"), staleTime: 15_000 });

export const useNewsPresets = (enabled = true) =>
  useQuery({ queryKey: [...key, "presets"], queryFn: () => api<NewsPresets>("/news/presets"), staleTime: 3_600_000, enabled });

export const useNewsSettings = () =>
  useQuery({ queryKey: [...key, "settings"], queryFn: () => api<NewsSettingsResponse>("/news/settings"), staleTime: 15_000 });
export const useDigests = () =>
  useQuery({ queryKey: [...key, "digests"], queryFn: () => api<DigestsResponse>("/news/digests"), staleTime: 30_000 });
export const useDigest = (id: number | null) =>
  useQuery({ queryKey: [...key, "digest", id], queryFn: () => api<Digest>(`/news/digests/${id}`), enabled: id !== null, staleTime: 60_000 });
export function useSaveNewsSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (settings: NewsRuntimeSettings) => api<{ settings: NewsRuntimeSettings }>("/news/settings", { method: "PUT", json: settings }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [...key, "settings"] }),
  });
}

export function useRunDigest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (period: "daily" | "weekly") => api<Digest>("/news/digests/run", { method: "POST", json: { period } }),
    onSettled: () => { qc.invalidateQueries({ queryKey: [...key, "digests"] }); qc.invalidateQueries({ queryKey: [...key, "digest"] }); },
  });
}

/** 1 件の操作を送る。返ってきた状態を、開いているどの一覧にも反映する（隠した記事を一覧から外すのは画面側）。 */
export function useNewsAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action, on = true }: { id: number; action: NewsAction; on?: boolean }) =>
      api<{ state: NewsState }>(`/news/items/${id}/action`, { method: "POST", json: { action, on } }),
    onSuccess: ({ state }, { id }) => {
      qc.setQueriesData<{ pages: NewsPage[]; pageParams: unknown[] }>({ queryKey: [...key, "items"] }, (old) => old && {
        ...old,
        pages: old.pages.map((page) => ({ ...page, items: page.items.map((item) => (item.id === id ? { ...item, state } : item)) })),
      });
      qc.setQueryData<NewsHome>([...key, "home"], (old) => old && patchHome(old, id, state));
      qc.invalidateQueries({ queryKey: [...key, "profile"] });
      qc.invalidateQueries({ queryKey: [...key, "archive"] });
    },
  });
}

/** 画面の 1 件の状態だけ差し替える（区画を取り直さない）。 */
function patchHome(home: NewsHome, id: number, state: NewsState): NewsHome {
  const fix = (items: NewsItem[]) => items.map((item) => (item.id === id ? { ...item, state } : item));
  return { ...home, onair: home.onair && home.onair.id === id ? { ...home.onair, state } : home.onair, now: fix(home.now), today: fix(home.today), week: fix(home.week),
    sections: home.sections.map((section) => ({ ...section, items: fix(section.items) })) };
}

export function useRefreshNews() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<RefreshResult>("/news/refresh", { method: "POST" }),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });
}

export function useMuteTopic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ topic, muted }: { topic: string; muted: boolean }) =>
      api<NewsProfile>("/news/profile/mute", { method: "PUT", json: { topic, muted } }),
    onSuccess: (profile) => {
      // 「あなたの興味」の画面はホームの 1 回の読み込み（home.interest）から描く。profile だけ差し替えても画面は変わらない
      // （2026-11-24 利用者「× をしても消えない」の原因）ので、ホームの興味も同じ結果へ差し替える。
      qc.setQueryData([...key, "profile"], profile);
      qc.setQueryData<NewsHome>([...key, "home"], (old) => old && { ...old, interest: profile });
      qc.invalidateQueries({ queryKey: [...key, "home"] });
      qc.invalidateQueries({ queryKey: [...key, "items"] });
    },
  });
}

export function useSaveChannels() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (config: ChannelsConfig) => api<ChannelsConfig>("/news/channels", { method: "PUT", json: config }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [...key, "channels"] }); qc.invalidateQueries({ queryKey: [...key, "home"] }); qc.invalidateQueries({ queryKey: [...key, "items"] }); },
  });
}

/** 表示・好みの保存。保存前に画面へ反映する（見た目はすぐ変わる）。 */
export function useSavePrefs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (prefs: NewsPrefs) => api<NewsPrefs>("/news/prefs", { method: "PUT", json: prefs }),
    onMutate: (prefs) => { qc.setQueryData([...key, "prefs"], prefs); },
    onSuccess: (saved, prefs) => {
      qc.setQueryData([...key, "prefs"], saved);
      if (prefs.taste !== undefined) { qc.invalidateQueries({ queryKey: [...key, "home"] }); qc.invalidateQueries({ queryKey: [...key, "items"] }); }
    },
    onError: () => qc.invalidateQueries({ queryKey: [...key, "prefs"] }),
  });
}

export function useSaveNewsSources() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (config: NewsConfig) => api<{ config: NewsConfig }>("/news/sources", { method: "PUT", json: config }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [...key, "sources"] }); qc.invalidateQueries({ queryKey: [...key, "manage"] }); },
  });
}

export const checkNewsSource = (body: { kind: string; value: string; name?: string }) =>
  api<CheckResult>("/news/sources/check", { method: "POST", json: body });
export const discoverNewsSite = (url: string) => api<DiscoverResult>("/news/sources/discover", { method: "POST", json: { url } });

/** 「3 分前」「2 日前」。1 週間を過ぎたら日付。 */
export function relativeTime(iso: string, now = Date.now()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const minutes = Math.max(0, Math.round((now - then) / 60_000));
  if (minutes < 1) return "たった今";
  if (minutes < 60) return `${minutes}分前`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)}時間前`;
  const days = Math.floor(minutes / 1440);
  return days < 7 ? `${days}日前` : new Date(iso).toLocaleDateString("ja-JP", { year: "numeric", month: "numeric", day: "numeric" });
}

export const KIND_STYLE: Record<NewsKind, { label: string; icon: string; band: string }> = {
  release: { label: "更新", icon: "📦", band: "from-emerald-500/80 to-teal-600/80" },
  news: { label: "ニュース", icon: "📰", band: "from-sky-500/80 to-blue-600/80" },
  model: { label: "モデル", icon: "🤖", band: "from-violet-500/80 to-purple-600/80" },
  forum: { label: "フォーラム", icon: "💬", band: "from-amber-500/80 to-orange-600/80" },
  reddit: { label: "Reddit", icon: "🗣️", band: "from-orange-500/80 to-red-500/80" },
  event: { label: "出来事", icon: "🔔", band: "from-zinc-500/80 to-slate-600/80" },
};


/** 取得先の管理（backend/qnapnewshub/sourcemgr.py・fetchlog.py）。 */
export interface ManagedSource {
  key: string; kind: string; format: string; name: string; value: string; domain: string; enabled: boolean; interval: number; groups: string[];
  last_at: string; last_ok_at: string; last_ok: boolean | null; last_error: string; last_count: number; last_status: number | null; last_skipped: string;
  /** 直近 7 日（古い日→今日）の成功率 0〜100・その日に取っていなければ null */
  week: (number | null)[]; total: number;
  /** 間隔を自動で変えない（固定）。出どころの種類（origin_set が偽なら、ドメインからの推定）・取り込んだ分類 */
  fixed: boolean; origin: string; origin_set: boolean; category: string;
  /** 取得の状態: interval = いまの間隔（分）・auto = 自動で決めている・backoff = 失敗で延ばしている・next_at = 次の取得の予定（ISO・無効なら空）・hit_rate = 304 の当たり率（%） */
  poll: { interval: number; auto: boolean; backoff: boolean; fails: number; next_at: string; hit_rate: number | null; conditional: number; hits: number; has_etag: boolean };
}
export interface ManageList {
  items: ManagedSource[]; groups: { id: string; label: string; hint: string; count: number }[]; default_interval: number; reddit_key: boolean; limit: number;
  interval_range: [number, number]; formats: Record<string, string>; origins: Record<string, string>; running: boolean;
}
export interface FetchHistoryRow { at: string; ok: boolean; status: number | null; count: number; new: number; ms: number; error: string; skipped: string }
export interface ManageDetail { item: ManagedSource; history: FetchHistoryRow[]; articles: { id: number; title: string; url: string; published_at: string }[] }
export interface GeneratedFeed { kind: "rss" | "github"; value: string; name: string; lang: "ja" | "en" }
export type GenerateRequest =
  | { type: "keyword"; keyword: string; lang: "ja" | "en" }
  | { type: "github"; target: string; what: "releases" | "commits" | "tags"; branch?: string }
  | { type: "steam"; target: string }
  | { type: "arxiv"; cats: string[] };
export type BulkAction = "enable" | "disable" | "delete" | "interval" | "fixed" | "auto" | "origin";

export interface ImportRow {
  line: number; name: string; url: string; category: string; origin: string; origin_guess: string; interval: number; enabled: boolean; kind: "rss" | "page";
  status: "new" | "duplicate" | "invalid"; reason: string;
}
export interface ImportPreview { format: "csv" | "opml"; rows: ImportRow[]; counts: { new: number; duplicate: number; invalid: number }; room: number; total: number }
export interface ImportResult { added: number; duplicate: number; invalid: number; failed: { url: string; name: string; reason: string }[]; mode: "check" | "direct" }
export const previewImport = (data: string, format: "auto" | "csv" | "opml" = "auto") =>
  api<ImportPreview>("/news/sources/import/preview", { method: "POST", json: { data, format } });
export const commitImport = (rows: ImportRow[], mode: "check" | "direct") =>
  api<ImportResult>("/news/sources/import/commit", { method: "POST", json: { mode, rows: rows.map((r) => ({ url: r.url, name: r.name, category: r.category, origin: r.origin, kind: r.kind, interval: r.interval, enabled: r.enabled })) } });

export const useManageList = () =>
  useQuery({ queryKey: [...key, "manage"], queryFn: () => api<ManageList>("/news/sources/manage"), staleTime: 10_000 });
export const useManageDetail = (k: string | null) =>
  useQuery({ queryKey: [...key, "manage", "detail", k], enabled: k !== null, staleTime: 5_000,
    queryFn: () => api<ManageDetail>(`/news/sources/manage/detail?${new URLSearchParams({ key: k ?? "", limit: "50" })}`) });
export const generateFeed = (body: GenerateRequest) => api<GeneratedFeed>("/news/sources/generate", { method: "POST", json: body });
export const bulkCheck = (keys: string[]) => api<{ results: (CheckResult & { key: string })[] }>("/news/sources/manage/bulk", { method: "POST", json: { action: "check", keys } });

export function useManageMutations() {
  const qc = useQueryClient();
  const done = () => { qc.invalidateQueries({ queryKey: key }); };
  return {
    bulk: useMutation({
      mutationFn: (b: { action: BulkAction; keys: string[]; interval?: number; origin?: string }) => api<{ count: number }>("/news/sources/manage/bulk", { method: "POST", json: b }),
      onSuccess: done,
    }),
    add: useMutation({
      mutationFn: (items: { kind: string; value: string; name?: string; interval?: number }[]) => api<{ added: number }>("/news/sources/manage/add", { method: "POST", json: { items } }),
      onSuccess: done,
    }),
  };
}

/** OpenRouter の鍵。本文は返らない（入っているかだけ）。送るときだけ本文を渡し、画面には残さない。 */
export const useOpenRouterKey = () =>
  useQuery({ queryKey: [...key, "openrouter-key"], queryFn: () => api<{ set: boolean }>("/news/openrouter-key"), staleTime: 15_000 });
export function useSaveOpenRouterKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (value: string) => api<{ set: boolean }>("/news/openrouter-key", { method: "PUT", json: { key: value } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [...key, "openrouter-key"] }); qc.invalidateQueries({ queryKey: [...key, "settings"] }); },
  });
}
export function useDeleteOpenRouterKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<{ set: boolean }>("/news/openrouter-key", { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [...key, "openrouter-key"] }); qc.invalidateQueries({ queryKey: [...key, "settings"] }); },
  });
}
export const useGatewayKey = () =>
  useQuery({ queryKey: [...key, "gateway-key"], queryFn: () => api<{ set: boolean }>("/news/gateway-key"), staleTime: 15_000 });
export function useSaveGatewayKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (value: string) => api<{ set: boolean }>("/news/gateway-key", { method: "PUT", json: { key: value } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [...key, "gateway-key"] }); },      // 設定の下書きを巻き戻さないよう、settings は読み直さない
  });
}
export function useDeleteGatewayKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<{ set: boolean }>("/news/gateway-key", { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [...key, "gateway-key"] }); },      // 設定の下書きを巻き戻さないよう、settings は読み直さない
  });
}
/** 「接続を試す」: {base}/models を読む。入力中の URL を渡す（空なら保存済みの URL）。 */
export interface GatewayTest { ok: boolean; models: string[]; ms: number; error: string }
export const useTestGateway = () =>
  useMutation({ mutationFn: (baseUrl: string) => api<GatewayTest>("/news/gateway/test", { method: "POST", json: { base_url: baseUrl } }) });

/** Things（要素の取り出し）。backend/qnapnewshub/entities.py・entities_router.py と対になる。 */
export type EntityType = "model" | "software" | "org" | "person" | "tech" | "product" | "game" | "policy" | "place" | "other";
export const ENTITY_TYPE_LABEL: Record<EntityType, string> = {
  model: "モデル", software: "ソフト", org: "組織", person: "人", tech: "技術", product: "製品", game: "ゲーム", policy: "制度", place: "場所", other: "その他",
};
export interface EntityRow { id: number; name: string; type: EntityType; first_seen: string; last_seen: string; article_count: number; period_count: number }
export interface EntityList { items: EntityRow[]; total: number; types: EntityType[] }
export interface EntityFact {
  id: number; kind: string; text: string; value: string; article_id: number; article_title: string; published_at: string;
  model: string; prompt_version: string; ai_extracted: boolean;
}
export interface EntityDetail {
  id: number; name: string; type: EntityType; aliases: string[]; first_seen: string; last_seen: string; article_count: number;
  timeline: { id: number; title: string; url: string | null; source: string; published_at: string; summary: string }[];
  facts: EntityFact[]; related: { id: number; name: string; type: EntityType; count: number }[];
}
export interface EntityQuery { q: string; type: string; days: number; sort: "count" | "recent" }
export const useEntities = (query: EntityQuery) =>
  useQuery({
    queryKey: ["entities", query],
    queryFn: () => {
      const p = new URLSearchParams({ sort: query.sort, limit: "100" });
      if (query.q.trim()) p.set("q", query.q.trim());
      if (query.type) p.set("type", query.type);
      if (query.days) p.set("days", String(query.days));
      return api<EntityList>(`/entities?${p}`);
    },
    staleTime: 15_000,
  });
export const useEntity = (id: number) => useQuery({ queryKey: ["entities", "one", id], queryFn: () => api<EntityDetail>(`/entities/${id}`), enabled: Number.isFinite(id) });
