import { Star } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type Quote } from "../api";
import { Panel, fmt, cls, StarButton } from "./Panel";

// 台北時間是否在交易時段附近（08:55–13:35，週一～五）；盤中才輪詢即時快照
function marketOpen() {
  const tw = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Taipei" }));
  const mins = tw.getHours() * 60 + tw.getMinutes();
  return tw.getDay() >= 1 && tw.getDay() <= 5 && mins >= 8 * 60 + 55 && mins <= 13 * 60 + 35;
}

/** 自選清單：批量報價（盤中 shioaji 快照每 5s 輪詢即時價，非盤中顯示最近收盤）。
 *  清單由後端持久化（App 提供 ids），星星可移除自選。 */
export function Watchlist({
  ids, selected, onSelect, onToggleWatch,
}: {
  ids: string[];
  selected: string;
  onSelect: (id: string) => void;
  onToggleWatch: (id: string) => void;
}) {
  const [quotes, setQuotes] = useState<Quote[]>([]);

  // ids 變動（加入/移除）即重抓報價；盤中每 5s 輪詢即時快照
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      if (!ids.length) { setQuotes([]); return; }
      api.quotes(ids).then((qs) => { if (!cancelled) setQuotes(qs); }).catch(() => {});
    };
    load();
    const t = window.setInterval(() => { if (marketOpen()) load(); }, 5000);
    return () => { cancelled = true; clearInterval(t); };
  }, [ids]);

  return (
    <Panel title="自選清單" icon={<Star size={13} />} sub={`${quotes.length} 檔`}>
      <table className="grid">
        <thead>
          <tr><th></th><th>代碼</th><th>成交</th><th>漲跌</th><th>幅度</th></tr>
        </thead>
        <tbody>
          {quotes.map((q) => (
            <tr key={q.stock_id} className={q.stock_id === selected ? "active" : ""}
                onClick={() => onSelect(q.stock_id)} style={{ cursor: "pointer" }}>
              <td style={{ textAlign: "center" }}>
                <StarButton active onToggle={() => onToggleWatch(q.stock_id)} />
              </td>
              <td>
                <div style={{ fontWeight: 600 }}>{q.stock_id}</div>
                <div style={{ color: "var(--text-dim)", fontSize: 11 }}>{q.name}</div>
              </td>
              <td className={`mono ${cls(q.change)}`} style={{ fontWeight: 600 }}>{fmt(q.last)}</td>
              <td className={`mono ${cls(q.change)}`}>{q.change != null && q.change > 0 ? "+" : ""}{fmt(q.change)}</td>
              <td>
                <span className={`chg-chip ${q.change_pct == null || q.change_pct === 0 ? "flat" : q.change_pct > 0 ? "up" : "down"}`}>
                  {q.change_pct != null && q.change_pct > 0 ? "+" : ""}{fmt(q.change_pct)}%
                </span>
              </td>
            </tr>
          ))}
          {quotes.length === 0 && <tr><td colSpan={5} className="empty-hint">尚無自選，點星星加入</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
