import { useEffect, useState } from "react";
import { FlaskConical, FolderOpen, Database, Newspaper, Settings, RotateCcw } from "lucide-react";
import { api, type Quote } from "../api";
import { fmt, cls } from "./Panel";

/** 頂部狀態列：logo、券商環境徽章、大盤指標、時鐘、資料連線狀態、設定。 */
export function TopBar({ hasKey, brokerEnv, onOpenSettings, onOpenData, onOpenBrowser, onOpenBacktest, onOpenNews, onResetLayout }: {
  hasKey: boolean | null;
  brokerEnv: "simulation" | "production" | null;
  onOpenSettings: () => void;
  onOpenData: () => void;
  onOpenBrowser: () => void;
  onOpenBacktest: () => void;
  onOpenNews: () => void;
  onResetLayout: () => void;
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
      {(() => {
        const day = now.getDay(), hm = now.getHours() * 60 + now.getMinutes();
        const open = day >= 1 && day <= 5 && hm >= 540 && hm <= 810;  // 平日 09:00–13:30
        return open
          ? <span className="mkt-open"><span className="dot" />盤中</span>
          : <span className="mkt-closed"><span className="dot" />已收盤</span>;
      })()}
      <span className="badge" style={hasKey ? {} : { background: "#3a2a1a", color: "#f0b90b", borderColor: "#5a3a1a" }}>
        {hasKey === null ? "…" : hasKey ? "AI 已啟用" : "AI 未設 Key"}
      </span>
      <button className="btn icon-btn" onClick={onOpenBacktest}><FlaskConical size={13} /> 回測</button>
      <button className="btn icon-btn" onClick={onOpenBrowser}><FolderOpen size={13} /> 股票</button>
      <button className="btn icon-btn" onClick={onOpenNews}><Newspaper size={13} /> 新聞</button>
      <button className="btn icon-btn" onClick={onOpenData}><Database size={13} /> 資料</button>
      <button className="btn icon-btn" onClick={onOpenSettings}><Settings size={13} /> 設定</button>
      <button className="btn icon-btn" onClick={onResetLayout} title="重置面板佈局"><RotateCcw size={13} /></button>
      <span className="clock">{now.toLocaleTimeString("zh-TW", { hour12: false })}</span>
    </div>
  );
}
