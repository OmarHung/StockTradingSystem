import { useEffect, useState } from "react";
import { api } from "../api";
import { Panel } from "./Panel";

const ICON: Record<string, string> = {
  technical: "📊", chips: "💰", fundamental: "📈", trader: "🧑‍💼",
};

/** 大腦活動：每次 LLM 呼叫與驗證層攔截記錄，決策可追溯。 */
export function BrainPanel() {
  const [rows, setRows] = useState<Record<string, any>[]>([]);
  const [onlyFlags, setOnlyFlags] = useState(false);

  const load = () => api.brainLog(100).then(setRows).catch(() => {});
  useEffect(() => { load(); }, []);

  const shown = onlyFlags ? rows.filter((r) => r.note) : rows;

  return (
    <Panel title="大腦活動" icon="🧠" sub={`${shown.length} 筆`}
      right={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label style={{ fontSize: 11, color: "var(--text-dim)", display: "flex", gap: 4, alignItems: "center" }}>
            <input type="checkbox" checked={onlyFlags} onChange={(e) => setOnlyFlags(e.target.checked)} style={{ width: "auto" }} />只看攔截
          </label>
          <button className="btn" onClick={load}>🔄</button>
        </div>
      }>
      <div style={{ padding: 8, display: "flex", flexDirection: "column", gap: 6 }}>
        {shown.map((r) => r.note ? (
          <div key={r.id} style={{ fontSize: 11, color: "var(--warning)", background: "rgba(240,185,11,0.08)",
            padding: "6px 8px", borderRadius: 4, borderLeft: "2px solid var(--warning)" }}>
            🛡️ [{r.ts}] {r.agent}｜{r.stock_id || ""}　{r.note}
          </div>
        ) : (
          <details key={r.id} style={{ fontSize: 11, background: "#0d1119", borderRadius: 4, padding: "4px 8px" }}>
            <summary style={{ cursor: "pointer", color: "var(--text-dim)" }}>
              {ICON[r.agent] || "🤖"} [{r.ts}] <b style={{ color: "var(--text)" }}>{r.agent}</b>｜{r.stock_id || ""}｜{r.model || ""}
            </summary>
            {r.prompt && <><div style={{ marginTop: 4, color: "var(--text-dim)" }}>Prompt:</div>
              <pre style={{ whiteSpace: "pre-wrap", color: "var(--text)", maxHeight: 100, overflow: "auto" }}>{r.prompt}</pre></>}
            {r.response && <><div style={{ color: "var(--text-dim)" }}>回應:</div>
              <pre style={{ whiteSpace: "pre-wrap", color: "#8ab4ff", maxHeight: 120, overflow: "auto" }}>{r.response}</pre></>}
          </details>
        ))}
        {shown.length === 0 && <div className="empty-hint">尚無記錄。到「AI 選股報告」跑一次分析後即可檢視。</div>}
      </div>
    </Panel>
  );
}
