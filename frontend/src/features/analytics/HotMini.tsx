import { Link } from "react-router-dom";
import { useHotTopics } from "../../api/analytics";

/** ニュースのホームの上に出す、ホットトピックの上位 5（押すと分析へ）。履歴が足りない・無いときは 1 行だけ。 */
export function HotMini() {
  const h = useHotTopics({ kind: "", lang: "" });
  if (h.isLoading || h.isError || !h.data) return null;
  const top = h.data.top.slice(0, 5);
  return (
    <div className="nw-note" data-testid="hot-mini" style={{ marginBottom: 8 }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
        <Link to="/analytics#sec-hot" className="nw-kicker" style={{ minHeight: 32, display: "inline-flex", alignItems: "center" }}>ホットトピック</Link>
        {top.length === 0 ? <span className="nw-muted" style={{ fontSize: ".75rem" }}>{h.data.enough_history ? "いまは目立って増えた語はありません" : "履歴をためています（3 日分で点数が出ます）"}</span>
          : top.map((t, i) => (
            <Link key={t.term} to="/analytics#sec-hot" data-testid="hot-mini-item" className="nw-btn" style={{ minHeight: 32, fontSize: ".75rem" }} title={`点数 ${t.score?.toFixed(1)} · ${t.count} 件`}>{i + 1}. {t.label}{t.kind === "entity" ? "（Things）" : ""}</Link>
          ))}
      </div>
    </div>
  );
}
