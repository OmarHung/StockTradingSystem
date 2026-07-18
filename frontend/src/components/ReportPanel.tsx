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
  const [scout, setScout] = useState<Awaited<ReturnType<typeof api.scout>>>(null);
  const [scoutOpen, setScoutOpen] = useState(false);               // 偵察區塊展開（含新聞標題清單）
  const [prog, setProg] = useState<{ stage: string; current: number; total: number } | null>(null);
  const [cancelling, setCancelling] = useState(false);

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

  // 日期切換（含初始）→ 載入該日已存計畫與偵察快照；報告不再被日期藏起來
  useEffect(() => {
    if (!asOf) return;
    api.tradePlans(asOf).then(setRecs).catch(() => {});
    api.scout(asOf).then(setScout).catch(() => setScout(null));
  }, [asOf]);

  // 背景執行 + 輪詢進度：階段文字（載入候選/題材偵察/逐檔分析子階段）＋逐檔計數實時更新，
  // 完成後載回該日已存交易計畫（重複執行不疊跑）
  const run = async () => {
    setLoading(true);
    setProg({ stage: "啟動中…", current: 0, total: 0 });
    let fails = 0;
    try {
      await api.analyzeStart(asOf, topN);   // 已在跑則直接接上輪詢
      for (;;) {
        await new Promise((r) => setTimeout(r, 800));
        let st;
        try {
          st = await api.analyzeStatus();
          fails = 0;
        } catch (e) {
          // 單次輪詢失敗（後端忙/dev reload/網路抖動）不放棄：後端分析仍在跑，
          // 連續失敗才收尾，避免誤報失敗讓使用者重按
          if (++fails >= 8) throw e;
          continue;
        }
        setProg({ stage: st.stage, current: st.current, total: st.total });
        if (!st.running) {
          if (st.error) throw new Error(st.error);
          setRecs(await api.tradePlans(asOf));
          api.scout(asOf).then(setScout).catch(() => {});
          break;
        }
      }
    }
    catch (e) { alert(String(e)); }
    finally { setLoading(false); setProg(null); setCancelling(false); }
  };

  // 中斷：設旗標，背景工作於下一檔前停止並移除本次已寫入的計畫；輪詢迴圈隨即收尾
  const cancel = async () => {
    setCancelling(true);
    try { await api.analyzeCancel(); }
    catch (e) { alert(String(e)); setCancelling(false); }
  };

  return (
    <Panel title="AI 選股報告" icon={<Bot size={13} />}
      right={
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)}
            disabled={loading} />
          <input type="number" min={1} max={10} value={topN} style={{ width: 48 }}
            onChange={(e) => setTopN(+e.target.value)} disabled={loading} />
          {loading ? (
            <button className="btn" onClick={cancel} disabled={cancelling}>
              {cancelling ? "中斷中…" : "中斷"}
            </button>
          ) : (
            <button className="btn primary" onClick={run} disabled={!hasKey || !asOf}>分析</button>
          )}
        </div>
      }>
      {!hasKey && <div className="empty-hint">未設定 ANTHROPIC_API_KEY，無法執行 LLM 分析。</div>}
      {loading && (
        <div style={{ padding: "18px 20px", textAlign: "center" }}>
          <div className="spinner" style={{ marginBottom: 10 }}>
            {prog?.stage || "分析師團隊 + 交易員決策中"}
            {prog && prog.total > 0 ? `　第 ${prog.current} / ${prog.total} 檔` : "…"}
          </div>
          {prog && prog.total > 0 && (
            <div style={{ height: 4, background: "var(--border)", borderRadius: 2, overflow: "hidden", maxWidth: 360, margin: "0 auto" }}>
              <div style={{
                height: "100%", background: "#2962ff", borderRadius: 2,
                width: `${Math.min(100, (prog.current / prog.total) * 100)}%`,
                transition: "width .4s",
              }} />
            </div>
          )}
        </div>
      )}
      <div style={{ padding: 8, display: "flex", flexDirection: "column", gap: 8 }}>
        {/* 🛰️ 政策題材偵察快照：當日掃到的新聞與候選（點標題展開新聞清單） */}
        {scout && (
          <div style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 10,
            background: "rgba(240,185,11,0.04)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}
              onClick={() => setScoutOpen((v) => !v)}>
              <span style={{ color: "var(--text-dim)", fontSize: 10, width: 12 }}>{scoutOpen ? "▼" : "▶"}</span>
              <b style={{ fontSize: 12 }}>🛰️ 政策題材偵察</b>
              <span style={{ fontSize: 11, color: "var(--text-dim)" }}>
                掃描 {scout.headlines.length} 則新聞（{scout.source === "rss" ? "RSS" : "Web搜尋"}）
                → 候選 {scout.candidates.length} 檔
              </span>
            </div>
            {scout.candidates.length > 0 && (
              <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
                {scout.candidates.map((c) => (
                  <span key={c.stock_id} className="tag" title={c.reason}
                    style={{ background: "rgba(240,185,11,0.15)", color: "var(--warning)", cursor: "pointer" }}
                    onClick={() => onSelect(c.stock_id)}>
                    📰 {c.stock_id} {c.name}・{c.theme}
                  </span>
                ))}
              </div>
            )}
            {scoutOpen && (
              <div style={{ marginTop: 8 }}>
                {scout.summary && <div style={{ fontSize: 12, lineHeight: 1.5, marginBottom: 8 }}>{scout.summary}</div>}
                <div style={{ maxHeight: 220, overflow: "auto", fontSize: 11, color: "var(--text-dim)" }}>
                  {scout.headlines.map((h, i) => (
                    <div key={i} style={{ padding: "3px 0", borderBottom: "1px solid var(--border)" }}>
                      <span className="mono" style={{ marginRight: 6 }}>{h.date.slice(5)}</span>
                      {h.url
                        ? <a href={h.url} target="_blank" rel="noreferrer" style={{ color: "var(--text)" }}>{h.title}</a>
                        : <span style={{ color: "var(--text)" }}>{h.title}</span>}
                      <span style={{ marginLeft: 6 }}>（{h.source}）</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
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
                {rec.source === "news_scout" && (
                  <span className="tag" style={{ background: "rgba(240,185,11,0.15)", color: "var(--warning)" }}
                    title="由政策題材偵察（新聞掃描）加入的候選，非量化初篩名額">📰 題材</span>
                )}
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
                    {({ technical: "技術", chips: "籌碼", fundamental: "基本", news: "新聞" } as any)[k] ?? k}:{" "}
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
