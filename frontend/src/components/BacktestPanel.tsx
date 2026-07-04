import { useEffect, useRef, useState } from "react";
import { createChart, LineSeries, ColorType, type IChartApi, type Time } from "lightweight-charts";
import { api, type BacktestResult } from "../api";
import { Panel, fmt } from "./Panel";

const STRATS = [
  { v: "screener", label: "多因子選股" },
  { v: "screener_risk", label: "多因子選股+風控(停損/大盤濾網)" },
  { v: "buy_and_hold", label: "買進持有0050" },
  { v: "ma_cross", label: "0050均線" },
];

/** 回測面板：跑策略回測（區間/資金/持倉數可調），畫權益曲線 + 顯示績效指標。 */
export function BacktestPanel() {
  const [strategy, setStrategy] = useState("screener");
  const twoYearsAgo = () => {
    const d = new Date(); d.setFullYear(d.getFullYear() - 2);
    return d.toISOString().slice(0, 10);
  };
  const [start, setStart] = useState(twoYearsAgo());
  const [end, setEnd] = useState(new Date().toISOString().slice(0, 10));
  const [cash, setCash] = useState(1_000_000);
  const [maxPos, setMaxPos] = useState(10);
  const [res, setRes] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  const run = async () => {
    setLoading(true);
    try {
      setRes(await api.backtest({ strategy, start, end, cash, max_positions: maxPos }));
    } catch (e) { alert(String(e)); } finally { setLoading(false); }
  };

  useEffect(() => {
    if (!ref.current || !res) return;
    chartRef.current?.remove();
    const chart = createChart(ref.current, {
      layout: { background: { type: ColorType.Solid, color: "#131722" }, textColor: "#787b86", fontFamily: "SF Mono, monospace" },
      grid: { vertLines: { color: "#1a1e2a" }, horzLines: { color: "#1a1e2a" } },
      rightPriceScale: { borderColor: "#232838" },
      timeScale: { borderColor: "#232838" },
      autoSize: true,
    });
    chartRef.current = chart;
    const line = chart.addSeries(LineSeries, { color: "#2962ff", lineWidth: 2 });
    line.setData(res.equity_curve.map((p) => ({ time: p.time as Time, value: p.value })));
    chart.timeScale().fitContent();
    return () => { chart.remove(); chartRef.current = null; };
  }, [res]);

  const m = res?.metrics;
  return (
    <Panel title="策略回測" icon="🧪"
      right={
        <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {STRATS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
          </select>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)}
            title="回測起日" style={{ width: 118, fontSize: 11 }} />
          <span style={{ color: "var(--text-dim)", fontSize: 11 }}>~</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)}
            title="回測迄日" style={{ width: 118, fontSize: 11 }} />
          <input type="number" value={cash} step={100_000} min={100_000}
            onChange={(e) => setCash(Number(e.target.value))}
            title="初始資金（元）" style={{ width: 92, fontSize: 11 }} />
          <input type="number" value={maxPos} min={1} max={30}
            onChange={(e) => setMaxPos(Number(e.target.value))}
            title="最大持倉檔數" style={{ width: 46, fontSize: 11 }} />
          <button className="btn primary" onClick={run} disabled={loading}>回測</button>
        </div>
      }>
      {loading && <div className="spinner">回測中…</div>}
      {m && (
        <div style={{ display: "flex", gap: 4, padding: "4px 6px", flexWrap: "wrap" }}>
          {[
            ["總報酬", `${fmt((m.total_return as number) * 100)}%`],
            ["年化", `${fmt((m.cagr as number) * 100)}%`],
            ["Sharpe", fmt(m.sharpe as number)],
            ["最大回撤", `${fmt((m.max_drawdown as number) * 100)}%`],
            ["交易數", String(m.n_trades)],
          ].map(([l, v]) => (
            <div className="metric" key={l as string} style={{ flex: 1, minWidth: 70 }}>
              <span className="m-label">{l}</span><span className="m-value">{v}</span>
            </div>
          ))}
        </div>
      )}
      <div ref={ref} style={{ width: "100%", height: res ? 200 : "100%", minHeight: 120 }}>
        {!res && !loading && <div className="empty-hint">選策略後按「回測」</div>}
      </div>
      {res && res.trades.length > 0 && (
        <details style={{ padding: 8 }}>
          <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--text-dim)" }}>
            逐筆成交明細（{res.trades.length} 筆）
          </summary>
          <table className="grid" style={{ marginTop: 6 }}>
            <thead><tr><th>日期</th><th>代碼</th><th>方向</th><th>股數</th><th>價格</th><th>金額</th></tr></thead>
            <tbody>
              {res.trades.slice(0, 200).map((t, i) => (
                <tr key={i}>
                  <td style={{ fontSize: 11 }}>{String(t.date)}</td>
                  <td>{String(t.stock_id)}</td>
                  <td className={t.side === "BUY" ? "up" : "down"}>{t.side === "BUY" ? "買" : "賣"}</td>
                  <td className="mono">{Number(t.shares).toLocaleString()}</td>
                  <td className="mono">{fmt(Number(t.price))}</td>
                  <td className="mono">{Number(t.amount).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </Panel>
  );
}
