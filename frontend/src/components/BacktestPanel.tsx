import { useEffect, useRef, useState } from "react";
import { createChart, LineSeries, ColorType, type IChartApi, type Time } from "lightweight-charts";
import { api, type BacktestResult } from "../api";
import { Panel, fmt } from "./Panel";

const STRATS = [
  { v: "screener", label: "多因子選股" },
  { v: "buy_and_hold", label: "買進持有0050" },
  { v: "ma_cross", label: "0050均線" },
];

/** 回測面板：跑策略回測，畫權益曲線 + 顯示績效指標。 */
export function BacktestPanel() {
  const [strategy, setStrategy] = useState("screener");
  const [res, setRes] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  const run = async () => {
    setLoading(true);
    try {
      setRes(await api.backtest({
        strategy, start: "2022-06-01", end: "2025-06-30", cash: 1_000_000, max_positions: 10,
      }));
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
        <div style={{ display: "flex", gap: 6 }}>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {STRATS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
          </select>
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
      <div ref={ref} style={{ width: "100%", height: res ? "calc(100% - 60px)" : "100%", minHeight: 120 }}>
        {!res && !loading && <div className="empty-hint">選策略後按「回測」</div>}
      </div>
    </Panel>
  );
}
