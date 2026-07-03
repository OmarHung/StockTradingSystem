import { useEffect, useRef } from "react";
import {
  createChart, CandlestickSeries, HistogramSeries, ColorType,
  type IChartApi, type Time,
} from "lightweight-charts";
import { api } from "../api";
import { Panel } from "./Panel";

/** K 線圖（TradingView lightweight-charts v5）。台股慣例：紅漲綠跌。 */
export function KChart({ stockId, name }: { stockId: string; name: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

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
    api.price(stockId, 250).then((d) => {
      if (cancelled) return;
      candle.setData(d.candles.map((c) => ({ ...c, time: c.time as Time })));
      vol.setData(d.volume.map((v) => ({ time: v.time as Time, value: v.value, color: v.color })));
      chart.timeScale().fitContent();
    });

    return () => { cancelled = true; chart.remove(); chartRef.current = null; };
  }, [stockId]);

  return (
    <Panel title={`K 線圖 · ${stockId}`} icon="📈" sub={name}>
      <div ref={ref} style={{ width: "100%", height: "100%" }} />
    </Panel>
  );
}
