import { Bot } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Panel, fmt } from "./Panel";

const ACTION: Record<string, { cls: string; label: string }> = {
  buy: { cls: "buy", label: "買進" },
  hold: { cls: "hold", label: "觀望" },
  avoid: { cls: "avoid", label: "避開" },
};

/** AI 選股報告：跑分析師團隊 + 驗證層 + 交易員，顯示交易計畫。 */
export function ReportPanel({ hasKey, onSelect }: { hasKey: boolean; onSelect: (id: string) => void }) {
  const [asOf, setAsOf] = useState("");
  const [topN, setTopN] = useState(3);
  const [recs, setRecs] = useState<Record<string, any>[]>([]);
  const [loading, setLoading] = useState(false);
  const [names, setNames] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});   // 卡片展開狀態（預設收闔）

  // 股名對照表（一次載入；歷史報告也吃得到）
  useEffect(() => {
    api.stocks().then((list) =>
      setNames(Object.fromEntries(list.map((s) => [s.stock_id, s.stock_name])))
    ).catch(() => {});
  }, []);

  // 預設日期＝最近一次有計畫的日子（每日流程/手動分析皆算），沒有才用最新交易日
  useEffect(() => {
    Promise.all([api.tradePlansLatestDate().catch(() => ({ as_of: null })),
                 api.dataStatus().catch(() => null)])
      .then(([latest, s]) => {
        const d = latest.as_of || s?.latest_trading_day;
        if (d) setAsOf(d);
      });
  }, []);

  // 日期切換（含初始）→ 載入該日已存計畫；報告不再被日期藏起來
  useEffect(() => {
    if (!asOf) return;
    api.tradePlans(asOf).then(setRecs).catch(() => {});
  }, [asOf]);

  const run = async () => {
    setLoading(true);
    try { setRecs(await api.analyze(asOf, topN)); }
    catch (e) { alert(String(e)); }
    finally { setLoading(false); }
  };

  return (
    <Panel title="AI 選股報告" icon={<Bot size={13} />}
      right={
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
          <input type="number" min={1} max={10} value={topN} style={{ width: 48 }}
            onChange={(e) => setTopN(+e.target.value)} />
          <button className="btn primary" onClick={run} disabled={loading || !hasKey || !asOf}>分析</button>
        </div>
      }>
      {!hasKey && <div className="empty-hint">未設定 ANTHROPIC_API_KEY，無法執行 LLM 分析。</div>}
      {loading && <div className="spinner">分析師團隊 + 交易員決策中（每檔約 4 次 LLM 呼叫）…</div>}
      <div style={{ padding: 8, display: "flex", flexDirection: "column", gap: 8 }}>
        {[...recs].sort((a, b) => b.plan.action_score - a.plan.action_score).map((rec) => {
          const p = rec.plan;
          const a = ACTION[p.action] ?? { cls: "hold", label: p.action };
          const expanded = !!open[rec.stock_id];
          return (
            <div key={rec.stock_id} style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 10 }}>
              {/* 標題列（點擊展開/收闔；點代號切主圖） */}
              <div style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}
                onClick={() => setOpen((o) => ({ ...o, [rec.stock_id]: !expanded }))}>
                <span style={{ color: "var(--text-dim)", fontSize: 10, width: 12 }}>{expanded ? "▼" : "▶"}</span>
                <span className={`tag ${a.cls}`}>{a.label}</span>
                <b onClick={(e) => { e.stopPropagation(); onSelect(rec.stock_id); }}>
                  {rec.stock_id}{names[rec.stock_id] ? ` ${names[rec.stock_id]}` : ""}
                </b>
                {rec.guard && (
                  <span style={{ fontSize: 11 }}>
                    {rec.guard.approved ? "🛡️✅" : "🛡️✗"}
                  </span>
                )}
                <span className="mono" style={{ color: "var(--text-dim)" }}>
                  動作分 {p.action_score > 0 ? "+" : ""}{fmt(p.action_score)} · 信心 {fmt(p.confidence * 100, 0)}%
                </span>
                <div style={{ flex: 1 }} />
                <span className="mono" style={{ fontSize: 11, color: "var(--text-dim)" }}>
                  進 {fmt(p.entry_low)}~{fmt(p.entry_high)} · 損 {fmt(p.stop_loss)} · 標 {fmt(p.target_price)} · R:R {fmt(p.reward_risk)}
                </span>
              </div>
              {expanded && rec.guard && (
                <div style={{
                  marginTop: 6, padding: "5px 8px", borderRadius: 4, fontSize: 11,
                  background: rec.guard.approved ? "rgba(14,203,129,0.08)" : "rgba(240,185,11,0.08)",
                  borderLeft: `2px solid ${rec.guard.approved ? "var(--down)" : "var(--warning)"}`,
                }}>
                  {rec.guard.approved ? (
                    <>🛡️ <b>風控核准</b>：買進 <b className="mono">{rec.guard.shares.toLocaleString()}</b> 股
                      · 投入 <span className="mono">{Number(rec.guard.est_cost).toLocaleString()}</span> 元
                      · 最大風險 <span className="mono">{Number(rec.guard.risk_amount).toLocaleString()}</span> 元</>
                  ) : (
                    <>🛡️ <b style={{ color: "var(--warning)" }}>風控駁回</b>
                      ［{rec.guard.reject_gate}］{rec.guard.reject_reason}</>
                  )}
                </div>
              )}
              {expanded && <div style={{ marginTop: 6, fontSize: 12, color: "var(--text)", lineHeight: 1.5 }}>{p.rationale}</div>}
              {expanded && <div style={{ marginTop: 6, display: "flex", gap: 10, flexWrap: "wrap" }}>
                {Object.entries(rec.analysts ?? {}).map(([k, e]: [string, any]) => (
                  <span key={k} style={{ fontSize: 11, color: "var(--text-dim)" }}>
                    {({ technical: "技術", chips: "籌碼", fundamental: "基本" } as any)[k] ?? k}:{" "}
                    <span className={e.report.score > 0 ? "up" : e.report.score < 0 ? "down" : "flat"}>
                      {e.report.signal} {fmt(e.report.score)}
                    </span>
                    {e.validation_flags?.length ? " ⚠️" : ""}
                  </span>
                ))}
              </div>}
            </div>
          );
        })}
        {!loading && recs.length === 0 && hasKey && <div className="empty-hint">選日期與檔數後按「分析」</div>}
      </div>
    </Panel>
  );
}
