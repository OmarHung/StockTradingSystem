import { useEffect, useState } from "react";
import { api, type Quote } from "../api";
import { Panel, fmt, cls } from "./Panel";

const DEFAULT_LIST = ["1101", "1102", "1216", "1301", "2330", "2317", "0050"];

/** 自選清單：顯示即時報價（目前為收盤基礎，Phase 5 接 shioaji 後改即時串流）。 */
export function Watchlist({ selected, onSelect }: { selected: string; onSelect: (id: string) => void }) {
  const [quotes, setQuotes] = useState<Quote[]>([]);

  useEffect(() => {
    let cancelled = false;
    Promise.all(DEFAULT_LIST.map((id) => api.quote(id).catch(() => null))).then((qs) => {
      if (!cancelled) setQuotes(qs.filter(Boolean) as Quote[]);
    });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="自選清單" icon="⭐" sub={`${quotes.length} 檔`}>
      <table className="grid">
        <thead>
          <tr><th>代碼</th><th>成交</th><th>漲跌</th><th>幅度</th></tr>
        </thead>
        <tbody>
          {quotes.map((q) => (
            <tr key={q.stock_id} className={q.stock_id === selected ? "active" : ""}
                onClick={() => onSelect(q.stock_id)} style={{ cursor: "pointer" }}>
              <td>
                <div style={{ fontWeight: 600 }}>{q.stock_id}</div>
                <div style={{ color: "var(--text-dim)", fontSize: 11 }}>{q.name}</div>
              </td>
              <td className={`mono ${cls(q.change)}`}>{fmt(q.last)}</td>
              <td className={`mono ${cls(q.change)}`}>{q.change != null && q.change > 0 ? "+" : ""}{fmt(q.change)}</td>
              <td className={`mono ${cls(q.change)}`}>{q.change_pct != null && q.change_pct > 0 ? "+" : ""}{fmt(q.change_pct)}%</td>
            </tr>
          ))}
          {quotes.length === 0 && <tr><td colSpan={4} className="empty-hint">載入中…</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
