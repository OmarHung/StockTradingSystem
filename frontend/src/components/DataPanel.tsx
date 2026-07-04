import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Panel } from "./Panel";

/** 資料狀態：覆蓋概況 + 初始化資料庫 + 背景回補（即時進度）。 */
export function DataPanel() {
  const [status, setStatus] = useState<Record<string, any>[]>([]);
  const [mode, setMode] = useState("limit");
  const [start, setStart] = useState("2020-01-01");
  const [stocks, setStocks] = useState("2330 2317 0050");
  const [limit, setLimit] = useState(50);
  const [force, setForce] = useState(false);
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState("");
  const poll = useRef<number | null>(null);

  const loadStatus = () => api.dataStatus().then(setStatus).catch(() => {});
  useEffect(() => { loadStatus(); return () => { if (poll.current) clearInterval(poll.current); }; }, []);

  const startPolling = () => {
    if (poll.current) clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      const s = await api.backfillStatus();
      setRunning(s.running); setLog(s.log);
      if (!s.running) { clearInterval(poll.current!); poll.current = null; loadStatus(); }
    }, 1500);
  };

  const start_ = async () => {
    try {
      await api.backfillStart({ mode, start, stocks, limit, force });
      setRunning(true); startPolling();
    } catch (e) { alert(String(e)); }
  };
  const stop_ = async () => { await api.backfillStop(); };
  const init_ = async () => { await api.initDb(); loadStatus(); alert("資料庫已初始化"); };

  return (
    <Panel title="資料狀態" icon="📦"
      right={<button className="btn" onClick={init_}>初始化資料庫</button>}>
      <div style={{ padding: 8 }}>
        <table className="grid">
          <thead><tr><th>資料表</th><th>列數</th><th>股票數</th><th>起</th><th>迄</th></tr></thead>
          <tbody>
            {status.map((r) => (
              <tr key={r.table as string}>
                <td>{r.table}</td>
                <td className="mono">{Number(r.rows).toLocaleString()}</td>
                <td className="mono">{r.stocks as number}</td>
                <td className="mono" style={{ fontSize: 11 }}>{r.min_date as string}</td>
                <td className="mono" style={{ fontSize: 11 }}>{r.max_date as string}</td>
              </tr>
            ))}
            {status.length === 0 && <tr><td colSpan={5} className="empty-hint">尚無資料，請先初始化並回補</td></tr>}
          </tbody>
        </table>

        <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="all">全市場</option>
            <option value="stocks">指定股票</option>
            <option value="limit">限制檔數</option>
          </select>
          <input value={start} onChange={(e) => setStart(e.target.value)} style={{ width: 100 }} title="起始日" />
          {mode === "stocks" && <input value={stocks} onChange={(e) => setStocks(e.target.value)} placeholder="2330 2317" style={{ width: 130 }} />}
          {mode === "limit" && <input type="number" value={limit} onChange={(e) => setLimit(+e.target.value)} style={{ width: 60 }} />}
          <label style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 11, color: "var(--text-dim)" }}>
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} style={{ width: "auto" }} />強制重抓
          </label>
          <button className="btn primary" onClick={start_} disabled={running}>回補</button>
          <button className="btn" onClick={stop_} disabled={!running}>中止</button>
          <span style={{ fontSize: 11, color: running ? "var(--down)" : "var(--text-dim)" }}>
            {running ? "🟢 執行中" : "⚪ 閒置"}
          </span>
        </div>
        {log && <pre style={{ marginTop: 8, fontSize: 10, color: "var(--text-dim)", background: "#0d1119",
          padding: 8, borderRadius: 4, maxHeight: 120, overflow: "auto", whiteSpace: "pre-wrap" }}>{log}</pre>}
      </div>
    </Panel>
  );
}
