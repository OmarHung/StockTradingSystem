import { Database } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, type DataStatus } from "../api";

const STATUS_UI: Record<string, { icon: string; color: string }> = {
  ok:      { icon: "✅", color: "var(--down)" },
  stale:   { icon: "⚠️", color: "var(--warning)" },
  partial: { icon: "⚠️", color: "var(--warning)" },
  missing: { icon: "❌", color: "var(--up)" },
};

/** 資料狀態視窗：健康報告（覆蓋率/新鮮度/結論）+ 回補（逐檔進度）+ 品質檢查。 */
export function DataModal({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState<DataStatus | null>(null);
  const [mode, setMode] = useState("all");
  // 回補起始日預設 2 年前（夠算季線/動能/回測，全市場也補得動）
  const twoYearsAgo = () => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 2);
    return d.toISOString().slice(0, 10);
  };
  const [start, setStart] = useState(twoYearsAgo());
  const [stocks, setStocks] = useState("2330 2317 0050");
  const [limit, setLimit] = useState(50);
  const [force, setForce] = useState(false);
  const [autoWait, setAutoWait] = useState(true);
  // 資料類型勾選（預設全選）
  const DATASETS = [
    { key: "price_daily", label: "股價" },
    { key: "institutional", label: "法人" },
    { key: "margin", label: "融資券" },
    { key: "month_revenue", label: "月營收" },
    { key: "dividend", label: "除權息" },
    { key: "valuation", label: "估值(本益比)" },
  ];
  const [dsSel, setDsSel] = useState<Record<string, boolean>>(
    Object.fromEntries(DATASETS.map((d) => [d.key, true])));
  const toggleDs = (k: string) => setDsSel((s) => ({ ...s, [k]: !s[k] }));
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState("");
  const [prog, setProg] = useState<{ pass: string; current: number; total: number; stock_id: string; rows: number } | null>(null);
  const [qc, setQc] = useState<Record<string, any> | null>(null);
  const [qcLoading, setQcLoading] = useState(false);
  // 中止流程狀態：idle → stopping（按下中止）→ stopped（確認 job 已死）
  const [stopState, setStopState] = useState<"idle" | "stopping" | "stopped">("idle");
  const stopRef = useRef<"idle" | "stopping" | "stopped">("idle");
  const setStop = (v: "idle" | "stopping" | "stopped") => { stopRef.current = v; setStopState(v); };
  const poll = useRef<number | null>(null);
  // log 自動捲動：使用者停在底部時，新內容進來自動跟到底；往上捲則不打擾
  const logRef = useRef<HTMLPreElement | null>(null);
  const atBottom = useRef(true);
  useEffect(() => {
    const el = logRef.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [log]);

  const loadStatus = () => api.dataStatus().then(setStatus).catch(() => {});

  const startPolling = () => {
    if (poll.current) clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      const s = await api.backfillStatus();
      setRunning(s.running); setLog(s.log); setProg(s.progress);
      if (!s.running) {
        clearInterval(poll.current!); poll.current = null;
        // 若是使用者按了中止而停，標記「已中止」；自然結束則維持 idle（由進度顯示 ✅完成）
        if (stopRef.current === "stopping") setStop("stopped");
        loadStatus();
      }
    }, 1200);
  };

  useEffect(() => {
    loadStatus();
    // 開窗時若回補已在跑（例如先前啟動的），自動接上進度輪詢
    api.backfillStatus().then((s) => {
      setRunning(s.running); setLog(s.log); setProg(s.progress);
      if (s.running) startPolling();
    }).catch(() => {});
    return () => { if (poll.current) clearInterval(poll.current); };
  }, []);

  const start_ = async () => {
    try {
      setStop("idle"); setProg(null);
      const picked = DATASETS.filter((d) => dsSel[d.key]).map((d) => d.key);
      if (picked.length === 0) { alert("請至少勾選一種資料類型"); return; }
      await api.backfillStart({ mode, start, stocks, limit, force, auto_wait: autoWait,
        datasets: picked.length === DATASETS.length ? undefined : picked });
      setRunning(true); startPolling();
    } catch (e) { alert(String(e)); }
  };
  const stop_ = async () => {
    setStop("stopping");                    // 立即回饋，不等下一輪輪詢
    try { await api.backfillStop(); }
    catch (e) { setStop("idle"); alert(String(e)); }
  };
  const runQc = async () => {
    setQcLoading(true);
    try { setQc(await api.qualityCheck()); } catch (e) { alert(String(e)); }
    finally { setQcLoading(false); }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 760 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Database size={15} /> 資料狀態</span>
          <span className="close" onClick={onClose}>✕</span>
        </div>

        <div className="modal-body">
          {/* 整體結論 */}
          {status?.summary && (
            <div style={{
              padding: "8px 12px", borderRadius: 6, marginBottom: 12, fontSize: 12,
              background: status.summary.level === "ok" ? "rgba(14,203,129,0.1)" : "rgba(240,185,11,0.1)",
              border: `1px solid ${status.summary.level === "ok" ? "var(--down)" : "var(--warning)"}`,
              color: status.summary.level === "ok" ? "var(--down)" : "var(--warning)",
            }}>
              {status.summary.level === "ok" ? "✅ " : "💡 "}{status.summary.text}
              {status.latest_trading_day && (
                <span style={{ color: "var(--text-dim)", marginLeft: 8 }}>
                  （最新交易日 {status.latest_trading_day}）
                </span>
              )}
            </div>
          )}

          {/* 各資料集健康狀態 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {status?.datasets.map((d) => {
              const ui = STATUS_UI[d.status] ?? STATUS_UI.missing;
              return (
                <div key={d.table} style={{
                  display: "flex", alignItems: "center", gap: 10, padding: "8px 10px",
                  background: "#0d1119", borderRadius: 6, fontSize: 12,
                }}>
                  <span style={{ fontSize: 14 }}>{ui.icon}</span>
                  <div style={{ flex: "0 0 190px" }}>
                    <div style={{ fontWeight: 600 }}>{d.label}</div>
                    <div style={{ color: "var(--text-dim)", fontSize: 10 }}>{d.desc}</div>
                  </div>
                  {/* 覆蓋率條 */}
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-dim)", marginBottom: 2 }}>
                      <span>覆蓋 {d.stocks}/{d.universe} 檔</span>
                      <span>{d.coverage_pct}%</span>
                    </div>
                    <div style={{ height: 5, background: "#1a1e2a", borderRadius: 3, overflow: "hidden" }}>
                      <div style={{ height: "100%", width: `${Math.min(d.coverage_pct, 100)}%`,
                        background: d.coverage_pct >= 80 ? "var(--down)" : "var(--warning)" }} />
                    </div>
                  </div>
                  <div style={{ flex: "0 0 150px", textAlign: "right" }}>
                    <div className="mono" style={{ fontSize: 11 }}>
                      {d.last_date ? `更新至 ${d.last_date}` : "無資料"}
                    </div>
                    <div style={{ fontSize: 11, color: ui.color }}>{d.hint}</div>
                  </div>
                </div>
              );
            })}
            {!status && <div className="empty-hint">載入中…</div>}
          </div>

          {/* 回補控制 */}
          <div style={{ marginTop: 14, display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="all">全市場</option>
              <option value="stocks">指定股票</option>
              <option value="limit">限制檔數</option>
            </select>
            <input value={start} onChange={(e) => setStart(e.target.value)} style={{ width: 100 }} title="起始日" />
            {mode === "stocks" && <input value={stocks} onChange={(e) => setStocks(e.target.value)} placeholder="2330 2317" style={{ width: 140 }} />}
            {mode === "limit" && <input type="number" value={limit} onChange={(e) => setLimit(+e.target.value)} style={{ width: 64 }} />}
            <span style={{ display: "flex", gap: 8, alignItems: "center", padding: "0 4px",
              borderLeft: "1px solid var(--border)", borderRight: "1px solid var(--border)" }}>
              {DATASETS.map((d) => (
                <label key={d.key} style={{ display: "flex", gap: 3, alignItems: "center",
                  fontSize: 11, color: dsSel[d.key] ? "var(--text)" : "var(--text-dim)", cursor: "pointer" }}>
                  <input type="checkbox" checked={dsSel[d.key]} onChange={() => toggleDs(d.key)} style={{ width: "auto" }} />
                  {d.label}
                </label>
              ))}
            </span>
            <label style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 11, color: "var(--text-dim)" }}>
              <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} style={{ width: "auto" }} />強制重抓
            </label>
            <label title="FinMind 額度用罄時自動等到下個整點續跑（可過夜連跑補完全市場）"
              style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 11, color: "var(--text-dim)" }}>
              <input type="checkbox" checked={autoWait} onChange={(e) => setAutoWait(e.target.checked)} style={{ width: "auto" }} />額度自動等待
            </label>
            <button className="btn primary" onClick={start_} disabled={running}>回補</button>
            <button className="btn" onClick={stop_} disabled={!running || stopState === "stopping"}>
              {stopState === "stopping" ? "中止中…" : "中止"}
            </button>
            <span style={{ fontSize: 11, color:
              stopState === "stopping" ? "var(--warning)"
              : running ? "var(--down)" : "var(--text-dim)" }}>
              {stopState === "stopping" ? "🟡 中止中，等待程序結束…"
                : running ? "🟢 執行中" : "⚪ 閒置"}
            </span>
          </div>

          {/* 中止確認橫幅 */}
          {stopState === "stopped" && (
            <div style={{ marginTop: 8, padding: "6px 10px", borderRadius: 4, fontSize: 12,
              background: "rgba(240,185,11,0.1)", border: "1px solid var(--warning)", color: "var(--warning)" }}>
              ⏹ 回補已中止（進度已保存，再按「回補」會從缺口續補，不會重來）
            </div>
          )}

          {/* 逐檔進度 */}
          {prog && (running || prog.pass === "完成") && (
            <div style={{ marginTop: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>
                <span>
                  <span className="tag" style={{ background: "var(--accent-dim)", color: "#8ab4ff", marginRight: 6 }}>
                    {prog.pass === "最新" ? "① 最新優先" : prog.pass === "歷史" ? "② 歷史回填"
                      : prog.pass === "等待額度" ? "⏸ 額度用罄・等待重置" : prog.pass}
                  </span>
                  {prog.pass === "等待額度"
                    ? <>將於 <b style={{ color: "var(--warning)" }}>{prog.stock_id}</b> 自動續跑（FinMind 每小時重置額度）</>
                    : prog.stock_id && <>回補中 <b style={{ color: "var(--text)" }}>{prog.stock_id}</b>（{prog.current}/{prog.total}）</>}
                  {prog.pass === "完成" && "✅ 全部完成"}
                </span>
                <span className="mono">{prog.total > 0 ? `${Math.round((prog.current / prog.total) * 100)}%` : ""}</span>
              </div>
              <div style={{ height: 8, background: "#0d1119", borderRadius: 4, overflow: "hidden" }}>
                <div style={{ height: "100%", width: `${prog.total > 0 ? (prog.current / prog.total) * 100 : 100}%`,
                  background: prog.pass === "最新" ? "var(--accent)" : "var(--down)", transition: "width 0.3s" }} />
              </div>
            </div>
          )}

          {log && <pre ref={logRef}
            onScroll={(e) => {
              const el = e.currentTarget;
              atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 12;
            }}
            style={{ marginTop: 8, fontSize: 10, color: "var(--text-dim)", background: "#0d1119",
            padding: 8, borderRadius: 4, maxHeight: 100, overflow: "auto", whiteSpace: "pre-wrap" }}>{log}</pre>}

          {/* 品質檢查報告 */}
          {qc && (
            <div style={{ marginTop: 12, fontSize: 11, background: "#0d1119", borderRadius: 4, padding: 10 }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>🩺 資料品質報告</div>
              {qc.error ? <div style={{ color: "var(--warning)" }}>{qc.error}</div> : (
                <>
                  <div style={{ display: "flex", gap: 14, flexWrap: "wrap", color: "var(--text-dim)" }}>
                    <span>交易日曆 <b style={{ color: "var(--text)" }}>{qc.calendar_days}</b> 天</span>
                    <span>檢查 <b style={{ color: "var(--text)" }}>{qc.checked_stocks}</b> 檔</span>
                    <span>真缺日股票 <b className={qc.stocks_with_gaps > 0 ? "up" : "down"}>{qc.stocks_with_gaps}</b> 檔（共 {qc.total_missing_days} 天）</span>
                  <span>無成交/停牌日(正常) <b style={{ color: "var(--text)" }}>{qc.no_trade_days ?? 0}</b></span>
                    <span>零價列(已自動剔除) <b style={{ color: "var(--text)" }}>{qc.zero_price_rows}</b></span>
                    <span>結構異常 <b className={qc.ohlc_anomalies > 0 ? "up" : "down"}>{qc.ohlc_anomalies}</b></span>
                  </div>
                  {qc.gap_samples?.length > 0 && (
                    <div style={{ marginTop: 6, color: "var(--warning)" }}>
                      缺日樣本：{qc.gap_samples.slice(0, 5).map((g: any) => `${g.stock_id}(缺${g.missing}天)`).join("、")}
                      <span style={{ color: "var(--text-dim)" }}>　→ 用「強制重抓」回補該檔即可修復</span>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        <div className="modal-foot">
          <button className="btn" onClick={runQc} disabled={qcLoading}>🩺 品質檢查</button>
          <button className="btn" onClick={onClose}>關閉</button>
        </div>
      </div>
    </div>
  );
}
