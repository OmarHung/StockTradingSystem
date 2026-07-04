import { useEffect, useState } from "react";
import { api } from "../api";

type Tab = "capital" | "risk" | "screener" | "llm" | "keys";
const TABS: { id: Tab; label: string }[] = [
  { id: "capital", label: "💰 資金" },
  { id: "risk", label: "🛡️ 風險" },
  { id: "screener", label: "🔍 選股" },
  { id: "llm", label: "🤖 LLM" },
  { id: "keys", label: "🔑 API 金鑰" },
];

/** 設定面板：表單化讀寫 settings.yaml 各區塊 + .env 金鑰。 */
export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("capital");
  const [cfg, setCfg] = useState<Record<string, any> | null>(null);
  const [env, setEnv] = useState<{ finmind_token: boolean; anthropic_key: boolean } | null>(null);
  const [saved, setSaved] = useState("");

  useEffect(() => {
    api.getConfig().then(setCfg).catch((e) => alert(String(e)));
    api.envStatus().then(setEnv).catch(() => {});
  }, []);

  const flash = (msg: string) => { setSaved(msg); setTimeout(() => setSaved(""), 2500); };
  const setField = (section: string, key: string, val: unknown) =>
    setCfg((c) => ({ ...c, [section]: { ...c![section], [key]: val } }));

  const saveSection = async (section: string) => {
    try { await api.updateConfig(section, cfg![section]); flash("已儲存 ✓"); }
    catch (e) { alert(String(e)); }
  };

  if (!cfg) return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-body"><div className="spinner">載入設定中…</div></div>
      </div>
    </div>
  );

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>⚙️ 系統設定</span>
          <span className="close" onClick={onClose}>✕</span>
        </div>
        <div className="modal-tabs">
          {TABS.map((t) => (
            <div key={t.id} className={`modal-tab ${tab === t.id ? "active" : ""}`}
                 onClick={() => setTab(t.id)}>{t.label}</div>
          ))}
        </div>

        <div className="modal-body">
          {tab === "capital" && (
            <NumRow label="總資金 (TWD)" step={100000}
              value={cfg.capital?.total} onChange={(v) => setField("capital", "total", v)} />
          )}

          {tab === "risk" && (<>
            <NumRow label="單筆最大風險 (% 總資金)" value={cfg.risk?.per_trade_risk_pct} onChange={(v) => setField("risk", "per_trade_risk_pct", v)} />
            <NumRow label="單一持股上限 (%)" value={cfg.risk?.max_single_position_pct} onChange={(v) => setField("risk", "max_single_position_pct", v)} />
            <NumRow label="R:R 下限" step={0.1} value={cfg.risk?.min_reward_risk_ratio} onChange={(v) => setField("risk", "min_reward_risk_ratio", v)} />
            <NumRow label="停損後冷卻天數" value={cfg.risk?.cooldown_days} onChange={(v) => setField("risk", "cooldown_days", v)} />
            <NumRow label="回撤熔斷門檻 (%)" value={cfg.risk?.max_drawdown_halt_pct} onChange={(v) => setField("risk", "max_drawdown_halt_pct", v)} />
          </>)}

          {tab === "screener" && (<>
            <NumRow label="選出檔數 Top N" value={cfg.screener?.top_n} onChange={(v) => setField("screener", "top_n", v)} />
            <NumRow label="20日均成交額下限 (元)" step={1000000} value={cfg.screener?.min_avg_turnover} onChange={(v) => setField("screener", "min_avg_turnover", v)} />
            <NumRow label="籌碼回看天數" value={cfg.screener?.chips_lookback} onChange={(v) => setField("screener", "chips_lookback", v)} />
            <div className="form-hint" style={{ margin: "8px 0" }}>因子權重（越大越重要）</div>
            {Object.entries(cfg.screener?.weights ?? {}).map(([k, v]) => (
              <NumRow key={k} label={k} step={0.1} value={v as number}
                onChange={(nv) => setCfg((c) => ({ ...c, screener: { ...c!.screener, weights: { ...c!.screener.weights, [k]: nv } } }))} />
            ))}
          </>)}

          {tab === "llm" && (<>
            <TextRow label="分析師模型" value={cfg.llm?.analyst_model} onChange={(v) => setField("llm", "analyst_model", v)} />
            <TextRow label="交易員模型" value={cfg.llm?.trader_model} onChange={(v) => setField("llm", "trader_model", v)} />
            <TextRow label="反思模型" value={cfg.llm?.reflection_model} onChange={(v) => setField("llm", "reflection_model", v)} />
            <div className="form-hint" style={{ margin: 0 }}>例：claude-opus-4-8 / claude-sonnet-4-6 / claude-haiku-4-5</div>
          </>)}

          {tab === "keys" && (
            <KeysTab env={env} onSaved={(k) => { flash("金鑰已寫入 .env ✓"); api.envStatus().then(setEnv); void k; }} />
          )}
        </div>

        <div className="modal-foot">
          {saved && <span className="save-ok">{saved}</span>}
          {tab !== "keys" && <button className="btn primary" onClick={() => saveSection(tab)}>儲存此頁</button>}
          <button className="btn" onClick={onClose}>關閉</button>
        </div>
      </div>
    </div>
  );
}

function NumRow({ label, value, step = 1, onChange }: { label: string; value: number; step?: number; onChange: (v: number) => void }) {
  return (
    <div className="form-row">
      <label>{label}</label>
      <input type="number" step={step} value={value ?? 0} onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  );
}
function TextRow({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="form-row">
      <label>{label}</label>
      <input value={value ?? ""} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

function KeysTab({ env, onSaved }: { env: { finmind_token: boolean; anthropic_key: boolean } | null; onSaved: (k: string) => void }) {
  const [finmind, setFinmind] = useState("");
  const [anthropic, setAnthropic] = useState("");
  const save = async (key: string, value: string) => {
    if (!value.trim()) return;
    try { await api.setEnv(key, value); onSaved(key); }
    catch (e) { alert(String(e)); }
  };
  return (
    <>
      <div className="form-row">
        <label>FinMind Token {env?.finmind_token ? "✅" : "⚠️未設"}</label>
        <input type="password" placeholder="留空則不變更" value={finmind} onChange={(e) => setFinmind(e.target.value)} />
        <button className="btn" onClick={() => save("FINMIND_TOKEN", finmind)}>存</button>
      </div>
      <div className="form-hint">提高 FinMind API 額度，免費註冊 finmindtrade.com</div>
      <div className="form-row">
        <label>Anthropic API Key {env?.anthropic_key ? "✅" : "⚠️未設"}</label>
        <input type="password" placeholder="留空則不變更" value={anthropic} onChange={(e) => setAnthropic(e.target.value)} />
        <button className="btn" onClick={() => save("ANTHROPIC_API_KEY", anthropic)}>存</button>
      </div>
      <div className="form-hint">LLM 分析師/交易員需要，取得於 console.anthropic.com</div>
    </>
  );
}
