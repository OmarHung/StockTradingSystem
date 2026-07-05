import { BrainCircuit } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { Panel } from "./Panel";

const ICON: Record<string, string> = {
  technical: "📊", chips: "💰", fundamental: "📈", trader: "🧑‍💼",
};
const AGENT_LABEL: Record<string, string> = {
  technical: "技術面", chips: "籌碼面", fundamental: "基本面", trader: "交易員",
};

/** 從 "YYYY-MM-DD HH:MM:SS" 取出 HH:MM，取不到就原樣返回。 */
function timeOf(ts: string): string {
  const m = /(\d{2}:\d{2})(:\d{2})?$/.exec(ts || "");
  return m ? m[1] : ts || "";
}
function dateOf(ts: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(ts || "");
  return m ? m[1] : "";
}
function tsMs(ts: string): number {
  const t = Date.parse((ts || "").replace(" ", "T"));
  return Number.isNaN(t) ? 0 : t;
}

/** 把同一檔的記錄切成「分析批次」：優先用後端寫入的 run_id 分組；
 *  舊資料沒有 run_id 時退回「相鄰兩筆間隔超過 10 分鐘視為另一批」。 */
function splitRuns(rows: Record<string, any>[]): Record<string, any>[][] {
  const GAP_MS = 10 * 60 * 1000;
  const runs: Record<string, any>[][] = [];
  for (const r of rows) {
    const last = runs[runs.length - 1];
    const lastRow = last?.[last.length - 1];
    const sameRun = lastRow && (
      r.run_id && lastRow.run_id
        ? r.run_id === lastRow.run_id
        : !r.run_id && !lastRow.run_id && tsMs(r.ts) - tsMs(lastRow.ts) <= GAP_MS
    );
    if (sameRun) last.push(r);
    else runs.push([r]);
  }
  return runs;
}

interface Group {
  key: string;
  stockId: string;
  asOf: string;
  rows: Record<string, any>[];   // 依時間升冪
  agents: string[];
  flags: number;
  start: string;
  end: string;
}

/** 單檔股票的分析細節視窗：時間軸列出每次 LLM 呼叫與攔截，可展開 Prompt/回應。 */
function BrainDetailModal({ group, title, onClose }: {
  group: Group; title: string; onClose: () => void;
}) {
  const span = timeOf(group.start) === timeOf(group.end)
    ? timeOf(group.start)
    : `${timeOf(group.start)} – ${timeOf(group.end)}`;
  const runs = splitRuns(group.rows);
  // 用 portal 掛到 body：面板在 react-grid-layout 的 transform 容器內，
  // 直接渲染會讓 fixed 定位相對於面板而非視窗，位置會跑掉
  return createPortal(
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 720 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>🧠 {title}</span>
          <span className="close" onClick={onClose}>✕</span>
        </div>
        <div className="modal-body" style={{ maxHeight: "70vh", overflow: "auto" }}>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 12,
            color: "var(--text-dim)", marginBottom: 10 }}>
            <span>分析日 <b style={{ color: "var(--text)" }}>{group.asOf}</b></span>
            <span>時間 <b style={{ color: "var(--text)" }}>{span}</b></span>
            <span>{group.rows.length} 次呼叫</span>
            {group.flags > 0 && <span style={{ color: "var(--warning)" }}>🛡️ 攔截 {group.flags}</span>}
            <span>
              {group.agents.map((a) => `${ICON[a] || "🤖"} ${AGENT_LABEL[a] || a}`).join("　")}
            </span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {runs.map((run, i) => {
              const rSpan = timeOf(run[0].ts) === timeOf(run[run.length - 1].ts)
                ? timeOf(run[0].ts)
                : `${timeOf(run[0].ts)} – ${timeOf(run[run.length - 1].ts)}`;
              return (
                <div key={run[0].id}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6,
                    fontSize: 11, color: "var(--text-dim)" }}>
                    <span className="tag" style={{ background: "var(--accent-dim)", color: "#8ab4ff" }}>
                      第 {i + 1} 次分析
                    </span>
                    <span>{dateOf(run[0].ts)}　{rSpan}</span>
                    <span>{run.length} 次呼叫</span>
                    <span style={{ flex: 1, height: 1, background: "var(--border)" }} />
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {run.map((r) => r.note ? (
                      <div key={r.id} style={{ fontSize: 11, color: "var(--warning)", background: "rgba(240,185,11,0.08)",
                        padding: "6px 8px", borderRadius: 4, borderLeft: "2px solid var(--warning)" }}>
                        🛡️ {timeOf(r.ts)}　{AGENT_LABEL[r.agent] || r.agent}　{r.note}
                      </div>
                    ) : (
                      <details key={r.id} style={{ fontSize: 11, background: "#0d1119", borderRadius: 4, padding: "5px 8px" }}>
                        <summary style={{ cursor: "pointer", color: "var(--text-dim)" }}>
                          {ICON[r.agent] || "🤖"} {timeOf(r.ts)}　<b style={{ color: "var(--text)" }}>{AGENT_LABEL[r.agent] || r.agent}</b>
                          {r.model ? `｜${r.model}` : ""}
                        </summary>
                        {r.prompt && <><div style={{ marginTop: 4, color: "var(--text-dim)" }}>Prompt:</div>
                          <pre style={{ whiteSpace: "pre-wrap", color: "var(--text)", maxHeight: 160, overflow: "auto" }}>{r.prompt}</pre></>}
                        {r.response && <><div style={{ color: "var(--text-dim)" }}>回應:</div>
                          <pre style={{ whiteSpace: "pre-wrap", color: "#8ab4ff", maxHeight: 200, overflow: "auto" }}>{r.response}</pre></>}
                      </details>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose}>關閉</button>
        </div>
      </div>
    </div>,
    document.body
  );
}

/** 大腦活動：LLM 呼叫與驗證層攔截記錄，依「股票 × 分析日」列摘要，點一檔開視窗看完整過程。 */
export function BrainPanel() {
  const [rows, setRows] = useState<Record<string, any>[]>([]);
  const [names, setNames] = useState<Record<string, string>>({});
  const [onlyFlags, setOnlyFlags] = useState(false);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const load = () => api.brainLog(200).then(setRows).catch(() => {});
  useEffect(() => {
    load();
    api.stocks()
      .then((list) => setNames(Object.fromEntries(list.map((s) => [s.stock_id, s.stock_name]))))
      .catch(() => {});
  }, []);

  const groups = useMemo<Group[]>(() => {
    const map = new Map<string, Group>();
    // rows 是新到舊；反轉成舊到新，讓群組內時間軸由上而下遞增
    for (const r of [...rows].reverse()) {
      const stockId = r.stock_id || "";
      const asOf = r.as_of || dateOf(r.ts);
      const key = `${stockId}|${asOf}`;
      let g = map.get(key);
      if (!g) {
        g = { key, stockId, asOf, rows: [], agents: [], flags: 0, start: r.ts, end: r.ts };
        map.set(key, g);
      }
      g.rows.push(r);
      g.end = r.ts;
      if (r.agent && !g.agents.includes(r.agent)) g.agents.push(r.agent);
      if (r.note) g.flags++;
    }
    // 依最後活動時間新到舊排序，最近分析的股票排最上面
    return [...map.values()].sort((a, b) => (a.end < b.end ? 1 : -1));
  }, [rows]);

  const shown = onlyFlags ? groups.filter((g) => g.flags > 0) : groups;
  const total = shown.reduce((n, g) => n + g.rows.length, 0);
  const titleOf = (g: Group) => g.stockId
    ? `${g.stockId} ${names[g.stockId] || ""}`.trim()
    : "系統／全市場";
  const opened = openKey ? groups.find((g) => g.key === openKey) : undefined;

  return (
    <Panel title="大腦活動" icon={<BrainCircuit size={13} />} sub={`${shown.length} 檔股票／${total} 筆`}
      right={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label style={{ fontSize: 11, color: "var(--text-dim)", display: "flex", gap: 4, alignItems: "center" }}>
            <input type="checkbox" checked={onlyFlags} onChange={(e) => setOnlyFlags(e.target.checked)} style={{ width: "auto" }} />只看攔截
          </label>
          <button className="btn" onClick={load}>🔄</button>
        </div>
      }>
      <div style={{ padding: 8, display: "flex", flexDirection: "column", gap: 6 }}>
        {shown.map((g) => {
          const span = timeOf(g.start) === timeOf(g.end)
            ? timeOf(g.start)
            : `${timeOf(g.start)} – ${timeOf(g.end)}`;
          return (
            <div key={g.key} onClick={() => setOpenKey(g.key)}
              style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap",
                padding: "8px 10px", background: "#0d1119", borderRadius: 6,
                border: "1px solid rgba(255,255,255,0.06)", cursor: "pointer" }}>
              <b style={{ fontSize: 12, color: "var(--text)" }}>{titleOf(g)}</b>
              <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{g.asOf}　{span}</span>
              <span style={{ fontSize: 12 }} title={g.agents.map((a) => AGENT_LABEL[a] || a).join("、")}>
                {g.agents.map((a) => ICON[a] || "🤖").join(" ")}
              </span>
              <span style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
                {g.flags > 0 && (
                  <span style={{ fontSize: 10, color: "var(--warning)", background: "rgba(240,185,11,0.12)",
                    padding: "1px 6px", borderRadius: 8 }}>🛡️ 攔截 {g.flags}</span>
                )}
                <span style={{ fontSize: 10, color: "var(--text-dim)" }}>{g.rows.length} 次呼叫</span>
                <span style={{ fontSize: 10, color: "var(--text-dim)" }}>›</span>
              </span>
            </div>
          );
        })}
        {shown.length === 0 && <div className="empty-hint">尚無記錄。到「AI 選股報告」跑一次分析後即可檢視。</div>}
      </div>
      {opened && (
        <BrainDetailModal group={opened} title={titleOf(opened)} onClose={() => setOpenKey(null)} />
      )}
    </Panel>
  );
}
