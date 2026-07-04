import { useEffect, useState } from "react";
import { api, type Quote } from "../api";
import { fmt, cls } from "./Panel";

/** 頂部狀態列：logo、券商環境徽章、大盤指標、時鐘、資料連線狀態、設定。 */
export function TopBar({ hasKey, brokerEnv, onOpenSettings, onOpenData }: {
  hasKey: boolean | null;
  brokerEnv: "simulation" | "production" | null;
  onOpenSettings: () => void;
  onOpenData: () => void;
}) {
  const [now, setNow] = useState(new Date());
  const [indices, setIndices] = useState<Quote[]>([]);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    api.indices().then(setIndices).catch(() => {});
    return () => clearInterval(t);
  }, []);

  return (
    <div className="topbar">
      <span className="logo">台股智慧交易終端</span>
      {brokerEnv === "production" ? (
        <span className="badge" style={{
          background: "rgba(255,67,61,0.18)", color: "var(--up)", borderColor: "var(--up)",
          fontWeight: 700,
        }}>🔴 正式環境</span>
      ) : (
        <span className="badge" style={{
          background: "rgba(240,185,11,0.12)", color: "var(--warning)", borderColor: "#5a4a1a",
        }}>模擬環境</span>
      )}
      {indices.map((mkt) => (
        <div className="ticker" key={mkt.stock_id}>
          <span className="label">{mkt.name}</span>
          <span className={`val ${cls(mkt.change)}`}>{fmt(mkt.last)}</span>
          <span className={`val ${cls(mkt.change)}`} style={{ fontSize: 12 }}>
            {mkt.change_pct != null && mkt.change_pct > 0 ? "+" : ""}{fmt(mkt.change_pct)}%
          </span>
        </div>
      ))}
      <div className="spacer" />
      <span className="live"><span className="dot" />資料連線</span>
      <span className="badge" style={hasKey ? {} : { background: "#3a2a1a", color: "#f0b90b", borderColor: "#5a3a1a" }}>
        {hasKey === null ? "…" : hasKey ? "AI 已啟用" : "AI 未設 Key"}
      </span>
      <button className="btn" onClick={onOpenData}>📦 資料</button>
      <button className="btn" onClick={onOpenSettings}>⚙️ 設定</button>
      <span className="clock">{now.toLocaleTimeString("zh-TW", { hour12: false })}</span>
    </div>
  );
}
