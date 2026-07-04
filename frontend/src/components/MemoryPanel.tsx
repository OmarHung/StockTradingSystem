import { useEffect, useState } from "react";
import { api } from "../api";
import { Panel, fmt } from "./Panel";

/** 反思規則庫：經驗/規則/被擋交易統計 + 一鍵反思 + 規則啟用切換。 */
export function MemoryPanel() {
  const [counts, setCounts] = useState<{ experiences: number; rules: number; blocked: number } | null>(null);
  const [rules, setRules] = useState<Record<string, any>[]>([]);
  const [exps, setExps] = useState<Record<string, any>[]>([]);
  const [tab, setTab] = useState<"rules" | "exps">("rules");
  const [running, setRunning] = useState(false);
  const [lastResult, setLastResult] = useState("");

  const load = () => {
    api.memoryStatus().then(setCounts).catch(() => {});
    api.memoryRules().then(setRules).catch(() => {});
    api.memoryExperiences(30).then(setExps).catch(() => {});
  };
  useEffect(load, []);

  const runReflect = async () => {
    setRunning(true); setLastResult("");
    try {
      const r = await api.reflectRun();
      const ev = r.evaluation;
      const rf = r.reflection;
      setLastResult(
        `評估 ${ev.evaluated} 筆（未到期 ${ev.pending}）` +
        (rf ? `｜新增 ${rf.rules_added} 條規則｜風格建議：${rf.style_advice}` : "｜素材不足未反思（需 ≥3 筆已評估決策）"));
      load();
    } catch (e) { alert(String(e)); }
    finally { setRunning(false); }
  };

  return (
    <Panel title="反思規則庫" icon="📚"
      sub={counts ? `經驗 ${counts.experiences}・規則 ${counts.rules}・被擋 ${counts.blocked}` : ""}
      right={
        <div style={{ display: "flex", gap: 4 }}>
          <button className="btn" style={{ padding: "2px 8px", fontSize: 11, ...(tab === "rules" ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}) }}
            onClick={() => setTab("rules")}>規則</button>
          <button className="btn" style={{ padding: "2px 8px", fontSize: 11, ...(tab === "exps" ? { borderColor: "var(--accent)", color: "#8ab4ff" } : {}) }}
            onClick={() => setTab("exps")}>經驗</button>
          <button className="btn primary" onClick={runReflect} disabled={running}>
            {running ? "反思中…" : "🧠 執行反思"}
          </button>
        </div>
      }>
      <div style={{ padding: 8 }}>
        {lastResult && (
          <div style={{ fontSize: 11, color: "var(--down)", marginBottom: 8,
            padding: "5px 8px", background: "rgba(14,203,129,0.08)", borderRadius: 4 }}>
            {lastResult}
          </div>
        )}

        {tab === "rules" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {rules.map((r) => (
              <div key={r.id} style={{ display: "flex", gap: 8, alignItems: "flex-start",
                fontSize: 12, padding: "6px 8px", background: "#0d1119", borderRadius: 4,
                opacity: r.active ? 1 : 0.45 }}>
                <span className="tag" style={r.kind === "anti_pattern"
                  ? { background: "rgba(255,67,61,0.15)", color: "var(--up)" }
                  : { background: "rgba(14,203,129,0.15)", color: "var(--down)" }}>
                  {r.kind === "anti_pattern" ? "反模式" : "有效"}
                </span>
                <div style={{ flex: 1 }}>
                  <div>{r.text}</div>
                  {r.evidence && <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 2 }}>證據：{r.evidence}</div>}
                </div>
                <button className="btn" style={{ padding: "1px 8px", fontSize: 10 }}
                  onClick={async () => { await api.memoryRuleToggle(r.id, !r.active); load(); }}>
                  {r.active ? "停用" : "啟用"}
                </button>
              </div>
            ))}
            {rules.length === 0 && <div className="empty-hint">尚無規則。累積 ≥3 筆已評估決策後按「執行反思」。</div>}
          </div>
        )}

        {tab === "exps" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {exps.map((e) => (
              <div key={e.id} style={{ fontSize: 11, padding: "5px 8px", background: "#0d1119", borderRadius: 4 }}>
                <span className={Number(e.meta?.ret) > 0 ? "up" : Number(e.meta?.ret) < 0 ? "down" : "flat"}>
                  [{e.meta?.outcome}] {e.meta?.ret != null ? `${fmt(Number(e.meta.ret) * 100, 1)}%` : ""}
                </span>
                <span style={{ color: "var(--text-dim)", marginLeft: 6 }}>{String(e.text).slice(0, 90)}</span>
              </div>
            ))}
            {exps.length === 0 && <div className="empty-hint">尚無經驗。決策滿 20 個交易日後按「執行反思」開始評估。</div>}
          </div>
        )}
      </div>
    </Panel>
  );
}
