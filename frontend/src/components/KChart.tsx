import { useEffect, useRef, useState } from "react";
import {
  createChart, CandlestickSeries, HistogramSeries, ColorType,
  type IChartApi, type Time,
} from "lightweight-charts";
import { api } from "../api";
import { Panel, StarButton } from "./Panel";

const TFS = [
  { v: "D", label: "日" },
  { v: "W", label: "週" },
  { v: "M", label: "月" },
];

/** K 線圖（lightweight-charts v5）。台股紅漲綠跌；還原價；日/週/月切換。 */
export function KChart({
  stockId, name, watched, onToggleWatch,
}: {
  stockId: string; name: string; watched: boolean; onToggleWatch: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [tf, setTf] = useState("D");

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#131722" },
        textColor: "#787b86",
        fontFamily: "SF Mono, monospace",
      },
      grid: {
        vertLines: { color: "#1a1e2a" },
        horzLines: { color: "#1a1e2a" },
      },
      rightPriceScale: { borderColor: "#232838" },
      timeScale: { borderColor: "#232838", timeVisible: false },
      crosshair: { mode: 1 },
      autoSize: true,
    });
    chartRef.current = chart;

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#ff433d", downColor: "#0ecb81",       // 台股紅漲綠跌
      wickUpColor: "#ff433d", wickDownColor: "#0ecb81",
      borderVisible: false,
    });
    const vol = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    let cancelled = false;
    api.price(stockId, 250, tf).then((d) => {
      if (cancelled) return;
      candle.setData(d.candles.map((c) => ({ ...c, time: c.time as Time })));
      vol.setData(d.volume.map((v) => ({ time: v.time as Time, value: v.value, color: v.color })));
      chart.timeScale().fitContent();
    });

    return () => { cancelled = true; chart.remove(); chartRef.current = null; };
  }, [stockId, tf]);

  return (
    <Panel title={`K 線圖 · ${stockId}`} icon="📈" sub={`${name} · 還原價`}
      right={
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <StarButton active={watched} onToggle={onToggleWatch} size={17} />
          <div style={{ display: "flex", gap: 2 }}>
            {TFS.map((t) => (
              <button key={t.v} className="btn" onClick={() => setTf(t.v)}
                style={tf === t.v ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}}>
                {t.label}
              </button>
            ))}
          </div>
        </div>
      }>
      <div ref={ref} style={{ width: "100%", height: "100%" }} />
    </Panel>
  );
}
