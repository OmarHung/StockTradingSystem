import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { fmt, cls } from "./Panel";

interface Row {
  stock_id: string; name: string; industry: string; market: string;
  price_days: number; price_last: string | null;
  downloaded: boolean; disposition: boolean;
}

const MAX_SHOW = 400;

/** 股票總覽瀏覽器：全市場（含未下載/處置）分類、篩選、搜尋；點列看詳細數據。 */
export function StockBrowserModal({ onClose, onSelect }: {
  onClose: () => void; onSelect: (id: string) => void;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [q, setQ] = useState("");
  const [market, setMarket] = useState("all");
  const [industry, setIndustry] = useState("all");
  const [status, setStatus] = useState("all");
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [picked, setPicked] = useState("");

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
      if (kw && !r.stock_id.toLowerCase().includes(kw) && !r.name.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [rows, q, market, industry, status]);

  const pick = async (id: string) => {
    setPicked(id); setDetailLoading(true);
    try { setDetail(await api.stockDetail(id)); }
    catch (e) { alert(String(e)); }
    finally { setDetailLoading(false); }
  };

  const d = detail;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 960, maxHeight: "90vh" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>🗂 股票總覽</span>
          <span style={{ marginLeft: 10, fontSize: 11, color: "var(--text-dim)" }}>
            共 {rows.length} 檔・符合 {filtered.length} 檔
          </span>
          <span className="close" onClick={onClose}>✕</span>
        </div>

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
          </select>
        </div>

        <div className="modal-body" style={{ display: "flex", gap: 12, padding: 12 }}>
          {/* 列表 */}
          <div style={{ flex: "1 1 55%", overflow: "auto", maxHeight: "62vh" }}>
            <table className="grid">
              <thead><tr><th>代號</th><th>名稱</th><th>市場</th><th>產業</th><th>資料</th><th>狀態</th></tr></thead>
              <tbody>
                {filtered.slice(0, MAX_SHOW).map((r) => (
                  <tr key={r.stock_id} className={r.stock_id === picked ? "active" : ""}
                      onClick={() => pick(r.stock_id)} style={{ cursor: "pointer" }}>
                    <td><b>{r.stock_id}</b></td>
                    <td>{r.name}</td>
                    <td style={{ fontSize: 11 }}>{r.market === "twse" ? "上市" : r.market === "tpex" ? "上櫃" : r.market}</td>
                    <td style={{ fontSize: 11, color: "var(--text-dim)" }}>{r.industry}</td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {r.downloaded ? `${r.price_days}天 ~${r.price_last}` : "—"}
                    </td>
                    <td>
                      {r.disposition && <span className="tag" style={{ background: "rgba(255,67,61,0.15)", color: "var(--up)", marginRight: 3 }}>處置</span>}
                      {r.downloaded
                        ? <span className="tag" style={{ background: "rgba(14,203,129,0.12)", color: "var(--down)" }}>已下載</span>
                        : <span className="tag" style={{ background: "#2a3040", color: "var(--text-dim)" }}>未下載</span>}
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && <tr><td colSpan={6} className="empty-hint">無符合條件的股票</td></tr>}
              </tbody>
            </table>
            {filtered.length > MAX_SHOW && (
              <div className="empty-hint">僅顯示前 {MAX_SHOW} 檔，請用篩選/搜尋縮小範圍</div>
            )}
          </div>

          {/* 詳細數據 */}
          <div style={{ flex: "1 1 45%", overflow: "auto", maxHeight: "62vh" }}>
            {!d && !detailLoading && <div className="empty-hint">← 點選股票查看詳細數據</div>}
            {detailLoading && <div className="spinner">載入中…</div>}
            {d && !detailLoading && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 12 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                  <b style={{ fontSize: 16 }}>{d.stock_id} {d.name}</b>
                  <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
                    {d.market === "twse" ? "上市" : "上櫃"}・{d.industry}
                  </span>
                  <div style={{ flex: 1 }} />
                  <button className="btn primary" style={{ fontSize: 11 }}
                    onClick={() => { onSelect(d.stock_id); onClose(); }}>📈 開啟K線</button>
                </div>

                {d.disposition && (
                  <div style={{ padding: "6px 10px", borderRadius: 4, background: "rgba(255,67,61,0.1)",
                    border: "1px solid var(--up)", color: "var(--up)", fontSize: 11 }}>
                    ⚠️ 處置中（{d.disposition.period_start} ~ {d.disposition.period_end}）：
                    {String(d.disposition.reason).slice(0, 60)}
                  </div>
                )}

                {d.quote && (
                  <div style={{ display: "flex", gap: 4 }}>
                    {[["收盤(還原)", fmt(d.quote.close)],
                      ["漲跌", <span className={cls(d.quote.change_pct)}>{d.quote.change_pct > 0 ? "+" : ""}{fmt(d.quote.change_pct)}%</span>],
                      ["量(股)", Number(d.quote.volume).toLocaleString()],
                      ["日期", d.quote.date]].map(([l, v], i) => (
                      <div key={i} className="metric" style={{ flex: 1, padding: "4px 8px" }}>
                        <span className="m-label">{l as string}</span>
                        <span className="m-value" style={{ fontSize: 12 }}>{v as any}</span>
                      </div>
                    ))}
                  </div>
                )}

                <div style={{ background: "#0d1119", borderRadius: 4, padding: 8 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>📦 資料覆蓋</div>
                  {Object.values(d.coverage ?? {}).map((c: any) => (
                    <div key={c.label} style={{ display: "flex", justifyContent: "space-between", padding: "2px 0", fontSize: 11 }}>
                      <span style={{ color: "var(--text-dim)" }}>{c.label}</span>
                      <span className="mono">{c.rows > 0 ? `${c.rows.toLocaleString()} 列　${c.from ?? ""}${c.from ? "~" : ""}${c.to ?? ""}` : "無資料"}</span>
                    </div>
                  ))}
                </div>

                {(d.chips || d.margin || d.fundamental) && (
                  <div style={{ background: "#0d1119", borderRadius: 4, padding: 8 }}>
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>📊 最新關鍵數據</div>
                    {d.chips?.foreign_net_5d != null && (
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, padding: "2px 0" }}>
                        <span style={{ color: "var(--text-dim)" }}>外資近5日淨買(股)</span>
                        <span className={`mono ${cls(d.chips.foreign_net_5d)}`}>{Number(d.chips.foreign_net_5d).toLocaleString()}</span>
                      </div>)}
                    {d.chips?.trust_net_5d != null && (
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, padding: "2px 0" }}>
                        <span style={{ color: "var(--text-dim)" }}>投信近5日淨買(股)</span>
                        <span className={`mono ${cls(d.chips.trust_net_5d)}`}>{Number(d.chips.trust_net_5d).toLocaleString()}</span>
                      </div>)}
                    {d.margin && (
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, padding: "2px 0" }}>
                        <span style={{ color: "var(--text-dim)" }}>融資餘額(張) / 融券(張)</span>
                        <span className="mono">{Number(d.margin.margin_purchase_balance).toLocaleString()} / {Number(d.margin.short_sale_balance).toLocaleString()}</span>
                      </div>)}
                    {d.fundamental?.revenue_yoy != null && (
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, padding: "2px 0" }}>
                        <span style={{ color: "var(--text-dim)" }}>最新月營收 YoY</span>
                        <span className={`mono ${cls(d.fundamental.revenue_yoy)}`}>{fmt(d.fundamental.revenue_yoy * 100, 1)}%</span>
                      </div>)}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
