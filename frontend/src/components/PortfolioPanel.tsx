import { useEffect, useRef, useState } from "react";
import { createChart, LineSeries, ColorType, type IChartApi, type Time } from "lightweight-charts";
import { api } from "../api";
import { Panel, fmt, cls } from "./Panel";

/** 持倉績效：權益曲線 vs 大盤、持倉損益、成交明細、每日流程觸發、緊急停止。 */
export function PortfolioPanel({ onSelect }: { onSelect: (id: string) => void }) {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [tab, setTab] = useState<"pos" | "fills">("pos");
  const [dailyRunning, setDailyRunning] = useState(false);
  const [dailyLog, setDailyLog] = useState("");
  const chartRef = useRef<HTMLDivElement>(null);
  const chartApi = useRef<IChartApi | null>(null);
  const poll = useRef<number | null>(null);

  const load = () => api.portfolio().then(setData).catch(() => {});
  useEffect(() => { load(); return () => { if (poll.current) clearInterval(poll.current); }; }, []);

  // 權益曲線 vs TAIEX
  useEffect(() => {
    const perf = data?.performance;
    if (!chartRef.current || !perf?.equity_curve?.length) return;
    chartApi.current?.remove();
    const chart = createChart(chartRef.current, {
      layout: { background: { type: ColorType.Solid, color: "#131722" }, textColor: "#787b86", fontFamily: "SF Mono, monospace" },
      grid: { vertLines: { color: "#1a1e2a" }, horzLines: { color: "#1a1e2a" } },
      rightPriceScale: { borderColor: "#232838" },
      timeScale: { borderColor: "#232838" },
      autoSize: true,
    });
    chartApi.current = chart;
    const eq = chart.addSeries(LineSeries, { color: "#2962ff", lineWidth: 2, title: "組合" });
    eq.setData(perf.equity_curve.map((p: any) => ({ time: p.time as Time, value: p.value })));
    if (perf.taiex_curve?.length) {
      const tx = chart.addSeries(LineSeries, { color: "#787b86", lineWidth: 1, title: "加權(同基期)" });
      tx.setData(perf.taiex_curve.map((p: any) => ({ time: p.time as Time, value: p.value })));
    }
    chart.timeScale().fitContent();
    return () => { chart.remove(); chartApi.current = null; };
  }, [data]);

  const runDaily = async () => {
    try {
      await api.dailyRun();
      setDailyRunning(true);
      if (poll.current) clearInterval(poll.current);
      poll.current = window.setInterval(async () => {
        const s = await api.dailyStatus();
        setDailyRunning(s.running); setDailyLog(s.log);
        if (!s.running) { clearInterval(poll.current!); poll.current = null; load(); }
      }, 1500);
    } catch (e) { alert(String(e)); }
  };

  const perf = data?.performance;
  const enabled = data?.trading_enabled;

  return (
    <Panel title="持倉績效" icon="💰"
      sub={data ? `現金 ${fmt(data.cash, 0)}` : ""}
      right={
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <button className="btn" style={{ fontSize: 11 }} onClick={async () => {
            if (!window.confirm("重置模擬帳本？將清空持倉/成交/權益紀錄，現金回到起始資金。")) return;
            await api.portfolioReset(); load();
          }}>♻️ 重置</button>
          <button className="btn primary" onClick={runDaily} disabled={dailyRunning}>
            {dailyRunning ? "每日流程執行中…" : "▶ 執行每日流程"}
          </button>
          <button className="btn"
            style={enabled
              ? { borderColor: "var(--up)", color: "var(--up)", fontWeight: 700 }
              : { background: "var(--up)", color: "#fff", fontWeight: 700 }}
            onClick={async () => {
              if (enabled && !window.confirm("確定要緊急停止交易？\n停止後每日流程只做保護性出場，不會開新倉。")) return;
              await api.tradingToggle(!enabled); load();
            }}>
            {enabled ? "🛑 緊急停止" : "⛔ 已停止（點擊恢復）"}
          </button>
        </div>
      }>
      <div style={{ padding: 8 }}>
        {perf?.has_data ? (
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 6 }}>
            {[
              ["權益", fmt(perf.current, 0)],
              ["總報酬", `${fmt(perf.total_return * 100)}%`],
              ["vs 大盤", perf.alpha != null ? `${perf.alpha > 0 ? "+" : ""}${fmt(perf.alpha * 100)}%` : "—"],
              ["Sharpe", fmt(perf.sharpe)],
              ["MDD", `${fmt(perf.max_drawdown * 100)}%`],
              ["勝率", perf.win_rate != null ? `${fmt(perf.win_rate * 100, 0)}%` : "—"],
              ["已平倉", String(perf.closed_trades ?? 0)],
            ].map(([l, v]) => (
              <div className="metric" key={l as string} style={{ flex: 1, minWidth: 70, padding: "4px 8px" }}>
                <span className="m-label">{l}</span><span className="m-value" style={{ fontSize: 13 }}>{v}</span>
              </div>
            ))}
          </div>
        ) : <div className="empty-hint">尚無交易紀錄。按「▶ 執行每日流程」開始模擬交易。</div>}

        {perf?.equity_curve?.length > 1 && (
          <div ref={chartRef} style={{ width: "100%", height: 150, marginBottom: 8 }} />
        )}

        {dailyLog && dailyRunning && (
          <pre style={{ fontSize: 10, color: "var(--text-dim)", background: "#0d1119",
            padding: 6, borderRadius: 4, maxHeight: 80, overflow: "auto" }}>{dailyLog}</pre>
        )}

        <div style={{ display: "flex", gap: 4, marginBottom: 4 }}>
          <button className="btn" style={{ padding: "2px 8px", fontSize: 11, ...(tab === "pos" ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}) }}
            onClick={() => setTab("pos")}>持倉 {data?.positions?.length ?? 0}</button>
          <button className="btn" style={{ padding: "2px 8px", fontSize: 11, ...(tab === "fills" ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}) }}
            onClick={() => setTab("fills")}>成交明細</button>
          {(data?.pending_orders?.length ?? 0) > 0 && (
            <span style={{ fontSize: 11, color: "var(--warning)", alignSelf: "center" }}>
              ⏳ 待撮合委託 {data?.pending_orders?.length} 筆
            </span>
          )}
        </div>

        {tab === "pos" && (
          <table className="grid">
            <thead><tr><th>代碼</th><th>股數</th><th>成本</th><th>現價</th><th>市值</th><th>未實現</th><th>%</th><th>停損/停利</th></tr></thead>
            <tbody>
              {(data?.positions ?? []).map((p: any) => (
                <tr key={p.stock_id} onClick={() => onSelect(p.stock_id)} style={{ cursor: "pointer" }}>
                  <td><b>{p.stock_id}</b></td>
                  <td className="mono">{Number(p.shares).toLocaleString()}</td>
                  <td className="mono">{fmt(p.avg_cost)}</td>
                  <td className="mono">{fmt(p.last)}</td>
                  <td className="mono">{Number(p.market_value).toLocaleString()}</td>
                  <td className={`mono ${cls(p.unrealized_pnl)}`}>{Number(p.unrealized_pnl).toLocaleString()}</td>
                  <td className={`mono ${cls(p.unrealized_pct)}`}>{p.unrealized_pct > 0 ? "+" : ""}{fmt(p.unrealized_pct)}%</td>
                  <td className="mono" style={{ fontSize: 10 }}>{fmt(p.stop_loss)} / {fmt(p.target)}</td>
                </tr>
              ))}
              {(data?.positions ?? []).length === 0 && <tr><td colSpan={8} className="empty-hint">空手</td></tr>}
            </tbody>
          </table>
        )}

        {tab === "fills" && (
          <table className="grid">
            <thead><tr><th>日期</th><th>代碼</th><th>方向</th><th>股數</th><th>價格</th><th>損益</th><th>原因</th></tr></thead>
            <tbody>
              {(data?.fills ?? []).map((f: any) => (
                <tr key={f.id}>
                  <td style={{ fontSize: 11 }}>{f.date}</td>
                  <td>{f.stock_id}</td>
                  <td className={f.side === "BUY" ? "up" : "down"}>{f.side === "BUY" ? "買" : "賣"}</td>
                  <td className="mono">{Number(f.shares).toLocaleString()}</td>
                  <td className="mono">{fmt(f.price)}</td>
                  <td className={`mono ${cls(f.pnl)}`}>{f.pnl != null ? Number(f.pnl).toLocaleString() : "—"}</td>
                  <td style={{ fontSize: 10 }}>{{ entry: "進場", stop: "停損", target: "停利", manual: "手動" }[f.reason as string] ?? f.reason}</td>
                </tr>
              ))}
              {(data?.fills ?? []).length === 0 && <tr><td colSpan={7} className="empty-hint">尚無成交</td></tr>}
            </tbody>
          </table>
        )}
      </div>
    </Panel>
  );
}
