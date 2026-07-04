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
  const [prog, setProg] = useState<{ pass: string; current: number; total: number; stock_id: string; rows: number } | null>(null);
  const [qc, setQc] = useState<Record<string, any> | null>(null);
  const [qcLoading, setQcLoading] = useState(false);
  const poll = useRef<number | null>(null);

  const loadStatus = () => api.dataStatus().then(setStatus).catch(() => {});
  useEffect(() => { loadStatus(); return () => { if (poll.current) clearInterval(poll.current); }; }, []);

  const startPolling = () => {
    if (poll.current) clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      const s = await api.backfillStatus();
      setRunning(s.running); setLog(s.log); setProg(s.progress);
      if (!s.running) { clearInterval(poll.current!); poll.current = null; loadStatus(); }
    }, 1200);
  };

  const start_ = async () => {
    try {
      await api.backfillStart({ mode, start, stocks, limit, force });
      setRunning(true); startPolling();
    } catch (e) { alert(String(e)); }
  };
  const stop_ = async () => { await api.backfillStop(); };
  const init_ = async () => { await api.initDb(); loadStatus(); alert("資料庫已初始化"); };
  const runQc = async () => {
    setQcLoading(true);
    try { setQc(await api.qualityCheck()); } catch (e) { alert(String(e)); }
    finally { setQcLoading(false); }
  };

  return (
    <Panel title="資料狀態" icon="📦"
      right={
        <div style={{ display: "flex", gap: 6 }}>
          <button className="btn" onClick={runQc} disabled={qcLoading}>🩺 品質檢查</button>
          <button className="btn" onClick={init_}>初始化資料庫</button>
        </div>
      }>
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

        {prog && (running || prog.pass === "完成") && (
          <div style={{ marginTop: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>
              <span>
                <span className="tag" style={{ background: "var(--accent-dim)", color: "#8ab4ff", marginRight: 6 }}>
                  {prog.pass === "最新" ? "① 最新優先" : prog.pass === "歷史" ? "② 歷史回填" : prog.pass}
                </span>
                {prog.stock_id && <>回補中 <b style={{ color: "var(--text)" }}>{prog.stock_id}</b>（{prog.current}/{prog.total}）</>}
                {prog.pass === "完成" && "✅ 全部完成"}
              </span>
              <span className="mono">{Math.round((prog.current / prog.total) * 100)}%</span>
            </div>
            <div style={{ height: 8, background: "#0d1119", borderRadius: 4, overflow: "hidden" }}>
              <div style={{ height: "100%", width: `${(prog.current / prog.total) * 100}%`,
                background: prog.pass === "最新" ? "var(--accent)" : "var(--down)", transition: "width 0.3s" }} />
            </div>
          </div>
        )}

        {log && <pre style={{ marginTop: 8, fontSize: 10, color: "var(--text-dim)", background: "#0d1119",
          padding: 8, borderRadius: 4, maxHeight: 100, overflow: "auto", whiteSpace: "pre-wrap" }}>{log}</pre>}

        {qc && (
          <div style={{ marginTop: 10, fontSize: 11, background: "#0d1119", borderRadius: 4, padding: 10 }}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>🩺 資料品質報告</div>
            {qc.error ? <div style={{ color: "var(--warning)" }}>{qc.error}</div> : (
              <>
                <div style={{ display: "flex", gap: 14, flexWrap: "wrap", color: "var(--text-dim)" }}>
                  <span>交易日曆 <b style={{ color: "var(--text)" }}>{qc.calendar_days}</b> 天</span>
                  <span>檢查 <b style={{ color: "var(--text)" }}>{qc.checked_stocks}</b> 檔</span>
                  <span>缺日股票 <b className={qc.stocks_with_gaps > 0 ? "up" : "down"}>{qc.stocks_with_gaps}</b> 檔（共 {qc.total_missing_days} 天）</span>
                  <span>零價列(已自動剔除) <b style={{ color: "var(--text)" }}>{qc.zero_price_rows}</b></span>
                  <span>結構異常 <b className={qc.ohlc_anomalies > 0 ? "up" : "down"}>{qc.ohlc_anomalies}</b></span>
                </div>
                {qc.gap_samples?.length > 0 && (
                  <div style={{ marginTop: 6, color: "var(--warning)" }}>
                    缺日樣本：{qc.gap_samples.slice(0, 5).map((g: any) =>
                      `${g.stock_id}(缺${g.missing}天)`).join("、")}
                    <span style={{ color: "var(--text-dim)" }}>　→ 用「強制重抓」回補該檔即可修復</span>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}
