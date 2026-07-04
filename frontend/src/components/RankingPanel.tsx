import { useEffect, useState } from "react";
import { api } from "../api";
import { Panel, fmt, cls } from "./Panel";

const TABS = [
  { key: "change_pct_up", label: "漲幅" },
  { key: "change_pct_down", label: "跌幅" },
  { key: "amount", label: "額" },
  { key: "volume", label: "量" },
];

interface Row {
  code: string; name: string; close: number;
  change_price: number; change_pct: number;
  total_volume: number; total_amount: number; date: string;
}

/** 排行榜（shioaji scanners 即時排行）。點列切換主圖。 */
export function RankingPanel({ onSelect }: { onSelect: (id: string) => void }) {
  const [tab, setTab] = useState("change_pct_up");
  const [rows, setRows] = useState<Row[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    setErr("");
    api.scanner(tab, 20)
      .then((r) => { if (!cancelled) setRows(r as Row[]); })
      .catch((e) => { if (!cancelled) { setRows([]); setErr(String(e.message || e)); } });
    return () => { cancelled = true; };
  }, [tab]);

  return (
    <Panel title="排行榜" icon="🏆"
      right={
        <div style={{ display: "flex", gap: 2 }}>
          {TABS.map((t) => (
            <button key={t.key} className="btn" style={{
              padding: "2px 8px", fontSize: 11,
              ...(tab === t.key ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}),
            }} onClick={() => setTab(t.key)}>{t.label}</button>
          ))}
        </div>
      }>
      {err ? <div className="empty-hint">{err.includes("400") ? "需要 shioaji 金鑰（設定 → API 金鑰）" : err}</div> : (
        <table className="grid">
          <thead><tr><th>#</th><th>代碼</th><th>成交</th><th>幅度</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.code} onClick={() => onSelect(r.code)} style={{ cursor: "pointer" }}>
                <td style={{ color: "var(--text-dim)" }}>{i + 1}</td>
                <td>
                  <div style={{ fontWeight: 600 }}>{r.code}</div>
                  <div style={{ color: "var(--text-dim)", fontSize: 10 }}>{r.name}</div>
                </td>
                <td className={`mono ${cls(r.change_price)}`}>{fmt(r.close)}</td>
                <td className={`mono ${cls(r.change_price)}`}>
                  {r.change_pct > 0 ? "+" : ""}{fmt(r.change_pct)}%
                </td>
              </tr>
            ))}
            {rows.length === 0 && !err && <tr><td colSpan={4} className="empty-hint">載入中…</td></tr>}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
