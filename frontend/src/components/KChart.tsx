import { CandlestickChart } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  createChart, createSeriesMarkers, CandlestickSeries, HistogramSeries, LineSeries, ColorType,
  type IChartApi, type Time, type MouseEventParams, type SeriesMarker,
} from "lightweight-charts";
import { api } from "../api";
import { Panel, StarButton } from "./Panel";

const TFS = [
  { v: "D", label: "日" },
  { v: "W", label: "週" },
  { v: "M", label: "月" },
];

// 均線設定（TradingView 慣例配色）
const MAS = [
  { n: 5, color: "#f0b90b" },
  { n: 20, color: "#ff7043" },
  { n: 60, color: "#26c6da" },
];

type Bar = { time: string; open: number; high: number; low: number; close: number };

function sma(candles: Bar[], n: number) {
  const out: { time: Time; value: number }[] = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].close;
    if (i >= n) sum -= candles[i - n].close;
    if (i >= n - 1) out.push({ time: candles[i].time as Time, value: sum / n });
  }
  return out;
}

/** K 線圖（lightweight-charts v5）。台股紅漲綠跌；MA5/20/60；
 *  游標 OHLC 資訊列（TradingView 式）；還原/原始價與日/週/月切換。 */
export function KChart({
  stockId, name, watched, onToggleWatch,
}: {
  stockId: string; name: string; watched: boolean; onToggleWatch: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [tf, setTf] = useState("D");
  const [adjusted, setAdjusted] = useState(true);
  // 游標所在 bar 的 OHLC（無游標時顯示最新一根）
  const [info, setInfo] = useState<{ o: number; h: number; l: number; c: number; pct: number | null } | null>(null);

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

    const maSeries = MAS.map((m) => chart.addSeries(LineSeries, {
      color: m.color, lineWidth: 1,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false,
    }));

    let cancelled = false;
    let candles: Bar[] = [];
    const barMap = new Map<string, { bar: Bar; prevClose: number | null }>();

    const setInfoFromBar = (b: Bar | undefined) => {
      if (!b) { setInfo(null); return; }
      const prev = barMap.get(String(b.time))?.prevClose ?? null;
      setInfo({
        o: b.open, h: b.high, l: b.low, c: b.close,
        pct: prev ? (b.close / prev - 1) * 100 : null,
      });
    };

    api.price(stockId, 250, tf, adjusted).then((d) => {
      if (cancelled) return;
      candles = d.candles as Bar[];
      candle.setData(candles.map((c) => ({ ...c, time: c.time as Time })));
      vol.setData(d.volume.map((v) => ({ time: v.time as Time, value: v.value, color: v.color })));
      MAS.forEach((m, i) => maSeries[i].setData(sma(candles, m.n)));
      barMap.clear();
      candles.forEach((c, i) => barMap.set(String(c.time),
        { bar: c, prevClose: i > 0 ? candles[i - 1].close : null }));
      setInfoFromBar(candles[candles.length - 1]);
      chart.timeScale().fitContent();

      // 除權息/分割減資 事件標記（僅日線：週/月K 的 bar 日期對不上事件日）
      if (tf === "D") {
        api.stockEvents(stockId).then((ev) => {
          if (cancelled) return;
          const markers: SeriesMarker<Time>[] = [];
          for (const d of ev.dividends) {
            if (!barMap.has(d.date)) continue;
            const kind = d.kind.replace("除", "");
            markers.push({
              time: d.date as Time, position: "belowBar", shape: "circle",
              color: "#f0b90b", size: 0.7,
              text: `${kind}${d.amount != null ? " " + Number(d.amount).toFixed(1) : ""}`,
            });
          }
          for (const c of ev.capital_changes) {
            if (!barMap.has(c.date)) continue;
            markers.push({
              time: c.date as Time, position: "belowBar", shape: "square",
              color: "#ab47bc", size: 0.7,
              text: c.kind === "auto_split" ? "分割" : "減資",
            });
          }
          if (markers.length) {
            markers.sort((a, b) => String(a.time) < String(b.time) ? -1 : 1);
            createSeriesMarkers(candle, markers);
          }
        }).catch(() => {});
      }
    });

    const onMove = (p: MouseEventParams) => {
      if (!p.time) { setInfoFromBar(candles[candles.length - 1]); return; }
      const hit = barMap.get(String(p.time));
      setInfoFromBar(hit?.bar);
    };
    chart.subscribeCrosshairMove(onMove);

    return () => {
      cancelled = true;
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
    };
  }, [stockId, tf, adjusted]);

  const pctCls = info?.pct == null ? "" : info.pct > 0 ? "up" : info.pct < 0 ? "down" : "flat";
  return (
    <Panel title={`K 線圖 · ${stockId}`} icon={<CandlestickChart size={13} />} sub={`${name} · ${adjusted ? "還原價" : "原始價"}`}
      right={
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <StarButton active={watched} onToggle={onToggleWatch} size={17} />
          <button className="btn" onClick={() => setAdjusted(!adjusted)}
            title="還原價：把除權息造成的跳空調整回去，看真實報酬走勢；原始價：市場實際成交價"
            style={adjusted ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}}>
            {adjusted ? "還原" : "原始"}
          </button>
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
      <div style={{ position: "relative", width: "100%", height: "100%" }}>
        {/* 游標 OHLC 資訊列（TradingView 式，左上角浮層） */}
        <div className="mono" style={{
          position: "absolute", top: 6, left: 8, zIndex: 3, fontSize: 11,
          display: "flex", gap: 10, pointerEvents: "none",
          textShadow: "0 1px 3px rgba(0,0,0,0.9)",
        }}>
          {info && (
            <>
              <span style={{ color: "var(--text-dim)" }}>開 <span className={pctCls || "flat"}>{info.o.toFixed(2)}</span></span>
              <span style={{ color: "var(--text-dim)" }}>高 <span className={pctCls || "flat"}>{info.h.toFixed(2)}</span></span>
              <span style={{ color: "var(--text-dim)" }}>低 <span className={pctCls || "flat"}>{info.l.toFixed(2)}</span></span>
              <span style={{ color: "var(--text-dim)" }}>收 <span className={pctCls || "flat"}>{info.c.toFixed(2)}</span></span>
              {info.pct != null && (
                <span className={pctCls}>{info.pct > 0 ? "+" : ""}{info.pct.toFixed(2)}%</span>
              )}
              {MAS.map((m) => (
                <span key={m.n} style={{ color: m.color }}>MA{m.n}</span>
              ))}
            </>
          )}
        </div>
        <div ref={ref} style={{ width: "100%", height: "100%" }} />
      </div>
    </Panel>
  );
}
