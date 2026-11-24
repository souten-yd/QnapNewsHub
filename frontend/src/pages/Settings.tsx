import { NewsSettings } from "../features/news/NewsSettings";

/** 設定。チャンネル・ソース・更新・OpenRouter の鍵・LLM・表示・好み・アーカイブ・待ち受け。 */
export default function SettingsPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-3 md:p-6">
      <h1 className="text-xl font-semibold tracking-tight">設定</h1>
      <NewsSettings />
    </div>
  );
}
