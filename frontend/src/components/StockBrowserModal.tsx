import { useEffect, useMemo, useRef, useState } from "react";
import { createChart, ColorType, LineSeries, HistogramSeries, type IChartApi, type Time } from "lightweight-charts";
import { api } from "../api";
import { fmt, cls } from "./Panel";

type Pt = { time: string; value: number };

/** 詳情頁小圖表：line 或 histogram（正紅負綠，台股慣例）。 */
function MiniChart({ data, kind, color = "#2962ff", height = 160, volumeScale = false, signColor = true, title }: {
  data: Pt[]; kind: "line" | "histogram"; color?: string; height?: number;
  volumeScale?: boolean; signColor?: boolean; title: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!ref.current || data.length === 0) return;
    const chart = createChart(ref.current, {
      layout: { background: { type: ColorType.Solid, color: "#0d1119" }, textColor: "#787b86", fontFamily: "SF Mono, monospace", fontSize: 10 },
      grid: { vertLines: { color: "#1a1e2a" }, horzLines: { color: "#1a1e2a" } },
      rightPriceScale: { borderColor: "#232838" },
      timeScale: { borderColor: "#232838" },
      autoSize: true,
      height,
    });
    chartRef.current = chart;
    const priceFormat = volumeScale ? { type: "volume" as const } : { type: "price" as const, precision: 2, minMove: 0.01 };
    if (kind === "line") {
      const s = chart.addSeries(LineSeries, { color, lineWidth: 2, priceFormat });
      s.setData(data.map((p) => ({ time: p.time as Time, value: p.value })));
    } else {
      const s = chart.addSeries(HistogramSeries, { priceFormat });
      s.setData(data.map((p) => ({
        time: p.time as Time, value: p.value,
        color: signColor ? (p.value >= 0 ? "#ff433d" : "#0ecb81") : color,
      })));
    }
    chart.timeScale().fitContent();
    return () => { chart.remove(); chartRef.current = null; };
  }, [data, kind, color, height, volumeScale, signColor]);

  if (data.length === 0) return null;
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 11, color: "var(--text-dim)", padding: "4px 2px" }}>{title}</div>
      <div ref={ref} style={{ width: "100%", height }} />
    </div>
  );
}

interface Row {
  stock_id: string; name: string; industry: string; market: string;
  price_days: number; price_last: string | null;
  downloaded: boolean; disposition: boolean;
  open: number | null; high: number | null; low: number | null;
  close: number | null; change_pct: number | null;
  ex_date: string | null; ex_kind: string | null;
}

const MAX_SHOW = 400;
const TABS = [
  { key: "overview", label: "總覽" },
  { key: "chips", label: "籌碼" },
  { key: "fund", label: "基本面" },
  { key: "dividend", label: "除權息" },
  { key: "coverage", label: "資料覆蓋" },
];

/** 明細列（label 左、值右） */
function Item({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "7px 2px", fontSize: 12, borderBottom: "1px solid var(--border)" }}>
      <span style={{ color: "var(--text-dim)" }}>{label}</span>
      <span className="mono">{value}</span>
    </div>
  );
}

/** 股票總覽瀏覽器：全市場列表（開高低收/漲跌幅）；點股票切換成整頁詳情＋分頁。 */
export function StockBrowserModal({ onClose, onSelect }: {
  onClose: () => void; onSelect: (id: string) => void;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [q, setQ] = useState("");
  const [market, setMarket] = useState("all");
  const [industry, setIndustry] = useState("all");
  const [status, setStatus] = useState("all");
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [series, setSeries] = useState<Record<string, Pt[]> | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [picked, setPicked] = useState("");
  const [tab, setTab] = useState("overview");

  useEffect(() => {
    api.stocksOverview().then((r) => setRows(r as Row[])).catch((e) => alert(String(e)));
  }, []);

  const industries = useMemo(
    () => Array.from(new Set(rows.map((r) => r.industry))).sort(), [rows]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return rows.filter((r) => {
      if (market !== "all" && r.market !== market) return false;
      if (industry !== "all" && r.industry !== industry) return false;
      if (status === "downloaded" && !r.downloaded) return false;
      if (status === "missing" && r.downloaded) return false;
      if (status === "disposition" && !r.disposition) return false;
      if (status === "exmonth" && !r.ex_date) return false;
      if (kw && !r.stock_id.toLowerCase().includes(kw) && !r.name.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [rows, q, market, industry, status]);

  const pick = async (id: string) => {
    setPicked(id); setDetailLoading(true); setTab("overview"); setSeries(null);
    try {
      const [dt, sr] = await Promise.all([api.stockDetail(id), api.stockSeries(id)]);
      setDetail(dt); setSeries(sr as Record<string, Pt[]>);
    }
    catch (e) { alert(String(e)); }
    finally { setDetailLoading(false); }
  };
  const backToList = () => { setPicked(""); setDetail(null); setSeries(null); };
  const showDetail = picked !== "";
  const d = detail;
  const f = d?.fundamental ?? {};
  const c = d?.chips ?? {};

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 960, maxHeight: "90vh" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          {showDetail
            ? <>
                <button className="btn" style={{ fontSize: 11, marginRight: 8 }} onClick={backToList}>← 返回列表</button>
                <span>🗂 {picked} {d?.name ?? ""}</span>
                <span style={{ marginLeft: 8, fontSize: 11, color: "var(--text-dim)" }}>
                  {d ? `${d.market === "twse" ? "上市" : "上櫃"}・${d.industry}` : ""}
                </span>
                <div style={{ flex: 1 }} />
                {d && <button className="btn primary" style={{ fontSize: 11, marginRight: 10 }}
                  onClick={() => { onSelect(d.stock_id); onClose(); }}>📈 開啟K線</button>}
              </>
            : <>
                <span>🗂 股票總覽</span>
                <span style={{ marginLeft: 10, fontSize: 11, color: "var(--text-dim)" }}>
                  共 {rows.length} 檔・符合 {filtered.length} 檔
                </span>
              </>}
          <span className="close" onClick={onClose}>✕</span>
        </div>

        {!showDetail && (
        <>
        {/* 篩選列 */}
        <div style={{ display: "flex", gap: 6, padding: "10px 16px", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
          <input placeholder="🔍 代號 / 名稱" value={q} onChange={(e) => setQ(e.target.value)}
            style={{ width: 160 }} autoFocus />
          <select value={market} onChange={(e) => setMarket(e.target.value)}>
            <option value="all">全部市場</option>
            <option value="twse">上市</option>
            <option value="tpex">上櫃</option>
          </select>
          <select value={industry} onChange={(e) => setIndustry(e.target.value)} style={{ maxWidth: 150 }}>
            <option value="all">全部產業</option>
            {industries.map((i) => <option key={i} value={i}>{i}</option>)}
          </select>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="all">全部狀態</option>
            <option value="downloaded">已下載</option>
            <option value="missing">未下載</option>
            <option value="disposition">處置中</option>
            <option value="exmonth">當月除權息</option>
          </select>
        </div>

        {/* 列表 */}
        <div className="modal-body" style={{ padding: 12 }}>
          <div style={{ overflow: "auto", maxHeight: "66vh" }}>
            <table className="grid" style={{ whiteSpace: "nowrap" }}>
              <thead><tr>
                <th>代號</th><th>名稱</th>
                <th style={{ textAlign: "right" }}>收盤</th>
                <th style={{ textAlign: "right" }}>漲跌%</th>
                <th style={{ textAlign: "right" }}>開</th>
                <th style={{ textAlign: "right" }}>高</th>
                <th style={{ textAlign: "right" }}>低</th>
                <th>市場</th>
                <th>產業</th>
                <th>狀態</th>
              </tr></thead>
              <tbody>
                {filtered.slice(0, MAX_SHOW).map((r) => (
                  <tr key={r.stock_id} onClick={() => pick(r.stock_id)} style={{ cursor: "pointer" }}>
                    <td><b>{r.stock_id}</b></td>
                    <td>{r.name}</td>
                    <td className="mono" style={{ textAlign: "right" }}>{r.close != null ? fmt(r.close) : "—"}</td>
                    <td className={`mono ${r.change_pct != null ? cls(r.change_pct) : ""}`} style={{ textAlign: "right" }}>
                      {r.change_pct != null ? `${r.change_pct > 0 ? "+" : ""}${fmt(r.change_pct)}%` : "—"}
                    </td>
                    <td className="mono" style={{ textAlign: "right", fontSize: 11, color: "var(--text-dim)" }}>{r.open != null ? fmt(r.open) : "—"}</td>
                    <td className="mono" style={{ textAlign: "right", fontSize: 11, color: "var(--text-dim)" }}>{r.high != null ? fmt(r.high) : "—"}</td>
                    <td className="mono" style={{ textAlign: "right", fontSize: 11, color: "var(--text-dim)" }}>{r.low != null ? fmt(r.low) : "—"}</td>
                    <td style={{ fontSize: 11 }}>{r.market === "twse" ? "上市" : r.market === "tpex" ? "上櫃" : r.market}</td>
                    <td style={{ fontSize: 11, color: "var(--text-dim)" }}>{r.industry}</td>
                    <td>
                      {r.disposition && <span className="tag" style={{ background: "rgba(255,67,61,0.15)", color: "var(--up)", marginRight: 3 }}>處置</span>}
                      {r.ex_date && <span className="tag" title={`${r.ex_date} 除${r.ex_kind || "權息"}`}
                        style={{ background: "rgba(240,185,11,0.15)", color: "#f0b90b", marginRight: 3 }}>
                        除{(r.ex_kind || "權息").replace("除", "")} {r.ex_date.slice(5).replace("-", "/")}
                      </span>}
                      {r.downloaded
                        ? <span className="tag" style={{ background: "rgba(14,203,129,0.12)", color: "var(--down)" }}>已下載</span>
                        : <span className="tag" style={{ background: "#2a3040", color: "var(--text-dim)" }}>未下載</span>}
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && <tr><td colSpan={10} className="empty-hint">無符合條件的股票</td></tr>}
              </tbody>
            </table>
            {filtered.length > MAX_SHOW && (
              <div className="empty-hint">僅顯示前 {MAX_SHOW} 檔，請用篩選/搜尋縮小範圍</div>
            )}
          </div>
        </div>
        </>
        )}

        {showDetail && (
        <div className="modal-body" style={{ padding: "0 16px 16px" }}>
          {detailLoading && <div className="spinner">載入中…</div>}
          {d && !detailLoading && (
            <>
              {/* 報價帶 */}
              {d.quote && (
                <div style={{ display: "flex", gap: 6, padding: "12px 0 8px" }}>
                  {[["收盤(還原)", <b style={{ fontSize: 16 }}>{fmt(d.quote.close)}</b>],
                    ["漲跌", <span className={cls(d.quote.change_pct)} style={{ fontSize: 14 }}>{d.quote.change_pct > 0 ? "+" : ""}{fmt(d.quote.change_pct)}%</span>],
                    ["成交量(股)", Number(d.quote.volume).toLocaleString()],
                    ["資料日期", d.quote.date]].map(([l, v], i) => (
                    <div key={i} className="metric" style={{ flex: 1, padding: "6px 10px" }}>
                      <span className="m-label">{l as string}</span>
                      <span className="m-value" style={{ fontSize: 13 }}>{v as any}</span>
                    </div>
                  ))}
                </div>
              )}

              {d.disposition && (
                <div style={{ padding: "6px 10px", borderRadius: 4, background: "rgba(255,67,61,0.1)",
                  border: "1px solid var(--up)", color: "var(--up)", fontSize: 11, marginBottom: 8 }}>
                  ⚠️ 處置中（{d.disposition.period_start} ~ {d.disposition.period_end}）：
                  {String(d.disposition.reason).slice(0, 80)}
                </div>
              )}

              {/* 分頁列 */}
              <div style={{ display: "flex", gap: 2, borderBottom: "1px solid var(--border)", marginBottom: 4 }}>
                {TABS.map((t) => (
                  <div key={t.key} onClick={() => setTab(t.key)}
                    style={{
                      padding: "8px 18px", cursor: "pointer", fontSize: 12,
                      color: tab === t.key ? "var(--text)" : "var(--text-dim)",
                      borderBottom: tab === t.key ? "2px solid #2962ff" : "2px solid transparent",
                      fontWeight: tab === t.key ? 600 : 400,
                    }}>{t.label}</div>
                ))}
              </div>

              <div style={{ maxHeight: "48vh", overflow: "auto", paddingTop: 4 }}>
                {tab === "overview" && (
                  <div>
                    {series?.price && <MiniChart data={series.price} kind="line" height={200} title="還原收盤走勢（近一年）" />}
                    <Item label="外資近5日淨買(股)" value={c.foreign_net_5d != null
                      ? <span className={cls(c.foreign_net_5d)}>{Number(c.foreign_net_5d).toLocaleString()}</span> : "—"} />
                    <Item label="投信近5日淨買(股)" value={c.trust_net_5d != null
                      ? <span className={cls(c.trust_net_5d)}>{Number(c.trust_net_5d).toLocaleString()}</span> : "—"} />
                    <Item label="最新月營收 YoY" value={f.revenue_yoy != null
                      ? <span className={cls(f.revenue_yoy)}>{fmt(f.revenue_yoy * 100, 1)}%</span> : "—"} />
                    <Item label="本益比 / 淨值比 / 殖利率" value={
                      `${f.per != null ? fmt(f.per, 1) : "—"} / ${f.pbr != null ? fmt(f.pbr, 2) : "—"} / ${f.dividend_yield_pct != null ? fmt(f.dividend_yield_pct, 2) + "%" : "—"}`} />
                    {f.next_ex_date != null && (
                      <Item label={`⏰ 即將除${f.next_ex_kind ?? "權息"}`} value={
                        <span style={{ color: "#f0b90b" }}>
                          {f.next_ex_date}{f.next_ex_cash_dividend != null ? `　${fmt(f.next_ex_cash_dividend, 2)} 元` : ""}
                        </span>} />
                    )}
                  </div>
                )}

                {tab === "chips" && (
                  <div>
                    {series?.foreign_net && <MiniChart data={series.foreign_net} kind="histogram" volumeScale title="外資日買賣超（股，紅買綠賣）" />}
                    {series?.trust_net && <MiniChart data={series.trust_net} kind="histogram" volumeScale title="投信日買賣超（股）" />}
                    {series?.margin_balance && <MiniChart data={series.margin_balance} kind="line" color="#f0b90b" volumeScale title="融資餘額（張）" />}
                    <Item label="外資近5日淨買(股)" value={c.foreign_net_5d != null
                      ? <span className={cls(c.foreign_net_5d)}>{Number(c.foreign_net_5d).toLocaleString()}</span> : "—"} />
                    <Item label="投信近5日淨買(股)" value={c.trust_net_5d != null
                      ? <span className={cls(c.trust_net_5d)}>{Number(c.trust_net_5d).toLocaleString()}</span> : "—"} />
                    <Item label="自營商近5日淨買(股)" value={c.dealer_net_5d != null
                      ? <span className={cls(c.dealer_net_5d)}>{Number(c.dealer_net_5d).toLocaleString()}</span> : "—"} />
                    <Item label="融資餘額(張)" value={d.margin ? Number(d.margin.margin_purchase_balance).toLocaleString() : "—"} />
                    <Item label="融券餘額(張)" value={d.margin ? Number(d.margin.short_sale_balance).toLocaleString() : "—"} />
                    <Item label="融資券資料日期" value={d.margin?.date ?? "—"} />
                  </div>
                )}

                {tab === "fund" && (
                  <div>
                    {series?.revenue && <MiniChart data={series.revenue} kind="histogram" volumeScale signColor={false} color="#2962ff" title="月營收（近三年，元）" />}
                    {series?.per && series.per.length > 5 && <MiniChart data={series.per} kind="line" color="#26a69a" title="本益比走勢" />}
                    <Item label="最新月營收(元)" value={f.latest_revenue != null
                      ? Number(f.latest_revenue).toLocaleString() : "—"} />
                    <Item label="營收月份" value={f.latest_revenue_year != null
                      ? `${f.latest_revenue_year}-${String(f.latest_revenue_month).padStart(2, "0")}` : "—"} />
                    <Item label="月營收 YoY" value={f.revenue_yoy != null
                      ? <span className={cls(f.revenue_yoy)}>{fmt(f.revenue_yoy * 100, 1)}%</span> : "—"} />
                    <Item label="本益比 (PER)" value={f.per != null ? fmt(f.per, 2) : "—（虧損或無資料）"} />
                    <Item label="股價淨值比 (PBR)" value={f.pbr != null ? fmt(f.pbr, 2) : "—"} />
                    <Item label="殖利率" value={f.dividend_yield_pct != null ? `${fmt(f.dividend_yield_pct, 2)}%` : "—"} />
                    <Item label="本益比近一年位階" value={f.per_percentile_1y != null
                      ? `${fmt(f.per_percentile_1y * 100, 0)}%（0=最便宜）` : "—（估值歷史不足一年）"} />
                    <Item label="估值資料日期" value={f.valuation_date ?? "—"} />
                    {f.next_ex_date != null && (
                      <Item label={`⏰ 即將除${f.next_ex_kind ?? "權息"}`} value={
                        <span style={{ color: "#f0b90b" }}>
                          {f.next_ex_date}{f.next_ex_cash_dividend != null ? `　${fmt(f.next_ex_cash_dividend, 2)} 元` : ""}
                        </span>} />
                    )}
                  </div>
                )}

                {tab === "dividend" && (
                  <div>
                    {(d.dividend_forecasts ?? []).length > 0 && (
                      <div style={{ marginBottom: 12 }}>
                        <div style={{ fontWeight: 600, fontSize: 12, padding: "4px 2px" }}>⏰ 預告（未來日程）</div>
                        {(d.dividend_forecasts as any[]).map((fcr, i) => (
                          <Item key={i} label={`${fcr.date}　除${fcr.kind ?? "權息"}`} value={
                            <span style={{ color: "#f0b90b" }}>
                              {fcr.cash_dividend != null && fcr.cash_dividend > 0 ? `現金 ${fmt(fcr.cash_dividend, 2)} 元` : ""}
                              {fcr.stock_ratio != null && fcr.stock_ratio > 0 ? `　配股率 ${fmt(fcr.stock_ratio, 4)}` : ""}
                              {(!fcr.cash_dividend && !fcr.stock_ratio) ? "—" : ""}
                            </span>} />
                        ))}
                      </div>
                    )}
                    <div style={{ fontWeight: 600, fontSize: 12, padding: "4px 2px" }}>📜 歷史除權息（{(d.dividends ?? []).length} 次）</div>
                    {(d.dividends ?? []).length === 0 && <div className="empty-hint">此商品無除權息紀錄</div>}
                    {(d.dividends ?? []).length > 0 && (
                      <table className="grid" style={{ whiteSpace: "nowrap" }}>
                        <thead><tr>
                          <th>除權息日</th><th>類別</th>
                          <th style={{ textAlign: "right" }}>配發(權值+息值)</th>
                          <th style={{ textAlign: "right" }}>前收盤</th>
                          <th style={{ textAlign: "right" }}>參考價</th>
                          <th style={{ textAlign: "right" }}>調整幅度</th>
                        </tr></thead>
                        <tbody>
                          {(d.dividends as any[]).map((dv, i) => (
                            <tr key={i}>
                              <td className="mono">{dv.date}</td>
                              <td>{dv.kind || "—"}</td>
                              <td className="mono" style={{ textAlign: "right" }}>{dv.dividend != null ? fmt(dv.dividend, 2) : "—"}</td>
                              <td className="mono" style={{ textAlign: "right" }}>{fmt(dv.before_price, 2)}</td>
                              <td className="mono" style={{ textAlign: "right" }}>{fmt(dv.after_price, 2)}</td>
                              <td className="mono down" style={{ textAlign: "right" }}>
                                {dv.before_price ? `-${fmt((1 - dv.after_price / dv.before_price) * 100, 2)}%` : "—"}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                )}

                {tab === "coverage" && (
                  <div>
                    {Object.values(d.coverage ?? {}).map((cv: any) => (
                      <Item key={cv.label} label={cv.label} value={
                        cv.rows > 0 ? `${cv.rows.toLocaleString()} 列　${cv.from ?? ""}${cv.from ? "~" : ""}${cv.to ?? ""}` : "無資料"} />
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
        )}
      </div>
    </div>
  );
}
