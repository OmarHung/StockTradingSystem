import { useEffect, useState } from "react";
import { api, type Quote } from "../api";
import { Panel, fmt, cls, StarButton } from "./Panel";

/** 自選清單：顯示即時報價（目前為收盤基礎，Phase 5 接 shioaji 後改即時串流）。
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

  // ids 變動（加入/移除）即重抓報價
  useEffect(() => {
    let cancelled = false;
    Promise.all(ids.map((id) => api.quote(id).catch(() => null))).then((qs) => {
      if (!cancelled) setQuotes(qs.filter(Boolean) as Quote[]);
    });
    return () => { cancelled = true; };
  }, [ids]);

  return (
    <Panel title="自選清單" icon="⭐" sub={`${quotes.length} 檔`}>
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
              <td className={`mono ${cls(q.change)}`}>{fmt(q.last)}</td>
              <td className={`mono ${cls(q.change)}`}>{q.change != null && q.change > 0 ? "+" : ""}{fmt(q.change)}</td>
              <td className={`mono ${cls(q.change)}`}>{q.change_pct != null && q.change_pct > 0 ? "+" : ""}{fmt(q.change_pct)}%</td>
            </tr>
          ))}
          {quotes.length === 0 && <tr><td colSpan={5} className="empty-hint">尚無自選，點星星加入</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
