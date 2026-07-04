import { useEffect, useState } from "react";
import { api, type ScreenerRow } from "../api";
import { Panel, fmt, cls } from "./Panel";

/** 智慧選股面板：跑量化多因子初篩，點列可切換主圖。 */
export function ScreenerPanel({ onSelect }: { onSelect: (id: string) => void }) {
  const [asOf, setAsOf] = useState("");
  const [rows, setRows] = useState<ScreenerRow[]>([]);
  const [loading, setLoading] = useState(false);

  // 預設帶入最新交易日
  useEffect(() => {
    api.dataStatus()
      .then((s) => { if (s.latest_trading_day) setAsOf(s.latest_trading_day); })
      .catch(() => {});
  }, []);

  const run = async () => {
    setLoading(true);
    try { setRows(await api.screener(asOf, 30)); }
    catch (e) { alert(String(e)); }
    finally { setLoading(false); }
  };

  return (
    <Panel title="智慧選股" icon="🔍"
      right={
        <div style={{ display: "flex", gap: 6 }}>
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
          <button className="btn primary" onClick={run} disabled={loading || !asOf}>執行</button>
        </div>
      }>
      {loading ? <div className="spinner">量化初篩中…</div> : (
        <table className="grid">
          <thead>
            <tr><th>#</th><th>代碼</th><th>股名</th><th>綜合分</th><th>動能20</th><th>法人淨買</th><th>營收YoY</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.stock_id} onClick={() => onSelect(r.stock_id)} style={{ cursor: "pointer" }}>
                <td>{r.rank}</td>
                <td><b>{r.stock_id}</b></td>
                <td>{r.stock_name}</td>
                <td className="mono">{fmt(r.score, 3)}</td>
                <td className={`mono ${cls(r.momentum_20)}`}>{fmt(r.momentum_20 * 100, 1)}%</td>
                <td className={`mono ${cls(r.chips_net_buy)}`}>{fmt(r.chips_net_buy, 0)}</td>
                <td className={`mono ${cls(r.revenue_yoy)}`}>{r.revenue_yoy != null ? fmt(r.revenue_yoy * 100, 1) + "%" : "—"}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={7} className="empty-hint">選日期後按「執行」</td></tr>}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
