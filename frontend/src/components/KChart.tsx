import { CandlestickChart } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  createChart, createSeriesMarkers, CandlestickSeries, HistogramSeries, LineSeries, ColorType,
  type IChartApi, type Time, type MouseEventParams, type SeriesMarker,
} from "lightweight-charts";
import { api } from "../api";
import { Panel, StarButton } from "./Panel";

const TFS = [
  { v: "1", label: "1分" },
  { v: "5", label: "5分" },
  { v: "15", label: "15分" },
  { v: "60", label: "60分" },
  { v: "D", label: "日" },
  { v: "W", label: "週" },
  { v: "M", label: "月" },
];
// 分鐘時間框架 → 載入回看天數（bar 數約 300~500 根）
const INTRADAY_DAYS: Record<string, number> = { "1": 5, "5": 20, "15": 40, "60": 90 };

// 均線設定（TradingView 慣例配色）
const MAS = [
  { n: 5, color: "#f0b90b" },
  { n: 20, color: "#ff7043" },
  { n: 60, color: "#26c6da" },
];

// time：日/週/月＝"YYYY-MM-DD"；分鐘＝epoch 秒（台北牆鐘時間當作 UTC，後端同慣例）
type Bar = { time: string | number; open: number; high: number; low: number; close: number };

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
 *  游標 OHLC 資訊列（TradingView 式）；1/5/15/60 分＋日/週/月切換；
 *  盤中經 WebSocket 即時更新（分鐘 K 與今日日 K，像專業看盤軟體）。 */
export function KChart({
  stockId, name, watched, onToggleWatch,
}: {
  stockId: string; name: string; watched: boolean; onToggleWatch: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [tf, setTf] = useState("D");
  const [adjusted, setAdjusted] = useState(true);
  // 拉取/同步最新 K 線中（切換股票或時間框架時後端會先回補分鐘資料，可能耗時 1~2 秒）
  const [loading, setLoading] = useState(false);
  // 游標所在 bar 的 OHLC（無游標時顯示最新一根）
  const [info, setInfo] = useState<{ o: number; h: number; l: number; c: number; pct: number | null } | null>(null);

  const tfMin = { "1": 1, "5": 5, "15": 15, "60": 60 }[tf];
  const intraday = tfMin != null;

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
      timeScale: { borderColor: "#232838", timeVisible: intraday, secondsVisible: false },
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
    let ws: WebSocket | null = null;
    let hovering = false;
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

    // 即時更新：覆寫/追加最後一根，並同步均線、量能與資訊列
    const commitLive = (b: Bar, v: number) => {
      const last = candles[candles.length - 1];
      const prevClose = last && String(last.time) !== String(b.time)
        ? last.close
        : (candles.length > 1 ? candles[candles.length - 2].close : null);
      if (last && String(last.time) === String(b.time)) candles[candles.length - 1] = b;
      else candles.push(b);
      candle.update({ ...b, time: b.time as Time });
      vol.update({
        time: b.time as Time, value: v,
        color: b.close >= b.open ? "#26a69a" : "#ef5350",
      });
      MAS.forEach((m, i) => maSeries[i].setData(sma(candles, m.n)));
      barMap.set(String(b.time), { bar: b, prevClose });
      if (!hovering) setInfoFromBar(b);
    };

    setLoading(true);
    const load = intraday
      ? api.kbars(stockId, tfMin!, INTRADAY_DAYS[tf])
      : api.price(stockId, 250, tf, adjusted);

    load.then((d) => {
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

      // ---- 盤中即時（分鐘框架與日線都吃 WS；週/月線量級太粗，收盤更新即可）----
      if (!intraday && tf !== "D") return;
      const histLast = candles[candles.length - 1];
      const histLastVol = d.volume.length ? d.volume[d.volume.length - 1].value : 0;
      // 分鐘桶聚合狀態：桶內各 1 分 K 的量（桶量 = 加總）
      let curBucket = 0;
      let minuteVols = new Map<number, number>();
      const step = (tfMin ?? 1) * 60;
      // 桶結束時間標記；60 分尾桶跨越 13:30 收盤競價時 ceil 會落在市場不存在的
      // 14:00 — 夾回當日 13:30（時間為台北牆鐘當 UTC，當日 13:30 = 日起點 + 48600s）
      const bucketOf = (t: number) => Math.min(
        Math.ceil(t / step) * step,
        Math.floor(t / 86400) * 86400 + 13.5 * 3600,
      );

      ws = api.kbarsWs(stockId);
      ws.onmessage = (evt) => {
        if (cancelled) return;
        let m: any;
        try { m = JSON.parse(evt.data); } catch { return; }
        if (m.type === "bar1m" && intraday) {
          const bucket = bucketOf(m.t);
          if (bucket !== curBucket) { curBucket = bucket; minuteVols = new Map(); }
          minuteVols.set(m.t, m.v);
          let volSum = 0; minuteVols.forEach((v) => { volSum += v; });
          const existing = barMap.get(String(bucket))?.bar;
          const merged: Bar = existing
            ? { time: bucket, open: existing.open, high: Math.max(existing.high, m.h),
                low: Math.min(existing.low, m.l), close: m.c }
            : { time: bucket, open: m.o, high: m.h, low: m.l, close: m.c };
          // 與歷史尾根重疊時無法得知已含多少量 → 取較大者（暫態，下次載入自動校正）
          const v = existing && histLast && String(histLast.time) === String(bucket)
            ? Math.max(histLastVol, volSum) : volSum;
          commitLive(merged, v);
        } else if (m.type === "day" && tf === "D") {
          // 今日日 K：tick 自帶今日開高低與累計量，整根覆蓋（自我校正）
          commitLive({ time: m.date, open: m.o, high: m.h, low: m.l, close: m.c }, m.v);
        }
      };
    }).catch(() => { /* 載入失敗：保持空圖，不拋 unhandled rejection */ })
      .finally(() => { if (!cancelled) setLoading(false); });

    const onMove = (p: MouseEventParams) => {
      hovering = !!p.time;
      if (!p.time) { setInfoFromBar(candles[candles.length - 1]); return; }
      const hit = barMap.get(String(p.time));
      setInfoFromBar(hit?.bar);
    };
    chart.subscribeCrosshairMove(onMove);

    return () => {
      cancelled = true;
      ws?.close();
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
    };
  }, [stockId, tf, adjusted]);

  const pctCls = info?.pct == null ? "" : info.pct > 0 ? "up" : info.pct < 0 ? "down" : "flat";
  return (
    <Panel title={`K 線圖 · ${stockId}`} icon={<CandlestickChart size={13} />}
      sub={`${name} · ${intraday ? "即時" : adjusted ? "還原價" : "原始價"}`}
      right={
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <StarButton active={watched} onToggle={onToggleWatch} size={17} />
          {!intraday && (
            <button className="btn" onClick={() => setAdjusted(!adjusted)}
              title="還原價：把除權息造成的跳空調整回去，看真實報酬走勢；原始價：市場實際成交價"
              style={adjusted ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}}>
              {adjusted ? "還原" : "原始"}
            </button>
          )}
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
        {/* 拉取最新數據中的提示浮層 */}
        {loading && (
          <div className="mono" style={{
            position: "absolute", top: 6, right: 10, zIndex: 4, fontSize: 11,
            padding: "3px 10px", borderRadius: 4, pointerEvents: "none",
            background: "rgba(19,23,34,0.85)", border: "1px solid var(--accent)",
            color: "#8ab4ff",
          }}>
            ⟳ 更新數據中…
          </div>
        )}
        <div ref={ref} style={{ width: "100%", height: "100%" }} />
      </div>
    </Panel>
  );
}
