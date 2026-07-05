import { Settings } from "lucide-react";
import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { api, type ModelInfo } from "../api";
import { MoneyInput } from "./Panel";

type Tab = "capital" | "risk" | "screener" | "llm" | "sched" | "notify" | "keys";
const TABS: { id: Tab; label: string }[] = [
  { id: "capital", label: "💰 資金" },
  { id: "risk", label: "🛡️ 風險" },
  { id: "screener", label: "🔍 選股" },
  { id: "llm", label: "🤖 LLM" },
  { id: "sched", label: "⏰ 排程" },
  { id: "notify", label: "📨 通知" },
  { id: "keys", label: "🔑 API 金鑰" },
];

/** 內建排程監控與調整（後端 asyncio 排程器，已取代 launchd）。 */
function SchedulerTab() {
  const [rows, setRows] = useState<Record<string, any>[]>([]);
  const [edit, setEdit] = useState<Record<string, { enabled: boolean; time: string }>>({});
  const [msg, setMsg] = useState("");

  const refresh = () => api.schedulerStatus().then((r) => {
    setRows(r);
    setEdit((e) => {
      const next = { ...e };
      for (const j of r) if (!(j.name in next)) next[j.name] = { enabled: j.enabled, time: j.time };
      return next;
    });
  }).catch(() => {});

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10_000);
    return () => clearInterval(t);
  }, []);

  const flash = (m: string) => { setMsg(m); setTimeout(() => setMsg(""), 2500); };
  const save = async (name: string) => {
    try { await api.schedulerConfig(name, edit[name].enabled, edit[name].time); flash("已儲存，立即生效 ✓"); refresh(); }
    catch (e) { alert(String(e)); }
  };
  const runNow = async (name: string) => {
    try { await api.schedulerRun(name); flash("已觸發 ▶"); setTimeout(refresh, 800); }
    catch (e) { alert(String(e)); }
  };

  return (
    <div>
      {rows.map((j) => (
        <div key={j.name} style={{ background: "#0d1119", borderRadius: 6, padding: 12, marginBottom: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <b style={{ fontSize: 13 }}>{j.label}</b>
            {j.running
              ? <span className="tag" style={{ background: "rgba(41,98,255,0.2)", color: "#8ab4ff" }}>🟢 執行中</span>
              : <span className="tag" style={{ background: "#2a3040", color: "var(--text-dim)" }}>閒置</span>}
            <div style={{ flex: 1 }} />
            <button className="btn" style={{ fontSize: 11 }} disabled={j.running}
              onClick={() => runNow(j.name)}>▶ 立即執行</button>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 6, fontSize: 12 }}>
            <label style={{ display: "flex", gap: 4, alignItems: "center", cursor: "pointer" }}>
              <input type="checkbox" checked={edit[j.name]?.enabled ?? j.enabled}
                onChange={(e) => setEdit((x) => ({ ...x, [j.name]: { ...x[j.name], enabled: e.target.checked } }))} />
              啟用（平日）
            </label>
            <input type="time" value={edit[j.name]?.time ?? j.time}
              onChange={(e) => setEdit((x) => ({ ...x, [j.name]: { ...x[j.name], time: e.target.value } }))}
              style={{ width: 110 }} />
            <button className="btn primary" style={{ fontSize: 11 }} onClick={() => save(j.name)}>儲存</button>
          </div>
          <div style={{ fontSize: 11, color: "var(--text-dim)", display: "flex", gap: 16 }}>
            <span>上次：{j.last_run ? `${j.last_run.started_at?.replace("T", " ")}（${j.last_run.source === "manual" ? "手動" : "排程"}）` : "從未執行"}</span>
            <span>下次：{j.next_run ? j.next_run.replace("T", " ") : "—（停用）"}</span>
          </div>
          {j.log_tail && (
            <pre style={{ fontSize: 10, color: "var(--text-dim)", background: "#0a0d14", borderRadius: 4,
              padding: 6, marginTop: 6, marginBottom: 0, whiteSpace: "pre-wrap", maxHeight: 60, overflow: "auto" }}>
              {j.log_tail}
            </pre>
          )}
        </div>
      ))}
      <div className="form-hint">
        排程器住在後端 API 行程內（asyncio），實際工作透過獨立子行程執行——API 重啟不會中斷進行中的任務。
        若 API 在排定時間之後才啟動，當天會自動補跑一次。週六日不執行。
      </div>
      {msg && <span className="save-ok">{msg}</span>}
    </div>
  );
}

/** 設定面板：表單化讀寫 settings.yaml 各區塊 + .env 金鑰。 */
export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("capital");
  const [cfg, setCfg] = useState<Record<string, any> | null>(null);
  const [env, setEnv] = useState<{ finmind_token: boolean; anthropic_key: boolean;
    shioaji_key?: boolean; telegram_token?: boolean } | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [saved, setSaved] = useState("");

  useEffect(() => {
    api.getConfig().then(setCfg).catch((e) => alert(String(e)));
    api.envStatus().then(setEnv).catch(() => {});
    api.models().then(setModels).catch(() => {});
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
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Settings size={15} /> 系統設定</span>
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
            <NumRow label="總資金 (TWD)" money
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
            <div className="form-row">
              <label>盤後 LLM 深度分析檔數</label>
              <input type="number" min={1} max={10} value={cfg.daily?.top_n ?? 3}
                onChange={async (e) => {
                  const v = Math.max(1, Math.min(10, Number(e.target.value)));
                  setField("daily", "top_n", v);
                  try { await api.updateConfig("daily", { top_n: v }); flash("已儲存 ✓"); }
                  catch (err) { alert(String(err)); }
                }} />
            </div>
            <div className="form-hint">
              每日流程從量化排行取前 N 檔進 AI 決策（排程/手動皆用此值；成本 ≈ N × 5 次 LLM 呼叫）。改動即存。
            </div>
            <NumRow label="選出檔數 Top N" value={cfg.screener?.top_n} onChange={(v) => setField("screener", "top_n", v)} />
            <NumRow label="20日均成交額下限 (元)" money value={cfg.screener?.min_avg_turnover} onChange={(v) => setField("screener", "min_avg_turnover", v)} />
            <NumRow label="籌碼回看天數" value={cfg.screener?.chips_lookback} onChange={(v) => setField("screener", "chips_lookback", v)} />
            <div className="form-hint" style={{ margin: "8px 0" }}>因子權重（越大越重要）</div>
            {Object.entries(cfg.screener?.weights ?? {}).map(([k, v]) => (
              <NumRow key={k} label={k} step={0.1} value={v as number}
                onChange={(nv) => setCfg((c) => ({ ...c, screener: { ...c!.screener, weights: { ...c!.screener.weights, [k]: nv } } }))} />
            ))}
          </>)}

          {tab === "llm" && (<>
            <ModelRow label="分析師模型" value={cfg.llm?.analyst_model} models={models} onChange={(v) => setField("llm", "analyst_model", v)} />
            <ModelRow label="交易員模型" value={cfg.llm?.trader_model} models={models} onChange={(v) => setField("llm", "trader_model", v)} />
            <ModelRow label="反思模型" value={cfg.llm?.reflection_model} models={models} onChange={(v) => setField("llm", "reflection_model", v)} />
            <div className="form-hint" style={{ margin: 0 }}>
              清單為 Claude 最新前 5 個模型（即時查詢）。⚡ 表示支援思考模式；ctx 為上下文上限、out 為輸出上限。
              交易員/反思建議選支援思考的模型；不支援思考者系統會自動關閉思考參數。
            </div>

            <div style={{ borderTop: "1px solid var(--border)", marginTop: 14, paddingTop: 14 }}>
              <NumRow label="Credit 儲值總額 (USD)" step={10}
                value={cfg.llm?.credit_total_usd ?? 0} onChange={(v) => setField("llm", "credit_total_usd", v)} />
              <div className="form-hint">
                Anthropic 官方 API 沒有查餘額的端點，系統改用本地估算：每次 LLM 呼叫記錄 token 用量並依模型價目換算。
                填入你在 Console 的儲值總額（0 = 不估算剩餘），頂部狀態列即顯示估計剩餘 credit。改動請按「儲存此頁」。
              </div>
              <LlmUsageRow />
            </div>

            <div style={{ borderTop: "1px solid var(--border)", marginTop: 14, paddingTop: 14 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <button className="btn" style={{ borderColor: "var(--up)", color: "var(--up)" }}
                  onClick={async () => {
                    if (!window.confirm(
                      "⚠️ 將清除所有 AI 產出資料：\n\n" +
                      "・大腦活動（LLM 呼叫記錄）\n・交易計畫（AI 選股報告）\n" +
                      "・Guard 風控駁回記錄\n・政策題材偵察快照\n・反思記憶庫（經驗/規則）\n\n" +
                      "行情資料、個股新聞原文、量化選股排名與模擬交易帳本不受影響。此操作無法復原，確定嗎？")) return;
                    try {
                      const r = await api.clearAiData();
                      const d = r.deleted;
                      flash(`已清除 ✓（大腦 ${d.brain_log}、計畫 ${d.trade_plan} 筆）`);
                    } catch (e) { alert(String(e)); }
                  }}>🗑️ 清除 AI 資料</button>
                <div className="form-hint" style={{ margin: 0 }}>
                  清空分析記錄、交易計畫與反思記憶，從乾淨狀態重新累積。不動行情資料、量化選股與交易帳本。
                </div>
              </div>
            </div>
          </>)}

          {tab === "sched" && <SchedulerTab />}

          {tab === "notify" && (
            <NotifyTab cfg={cfg} setCfg={setCfg} env={env} flash={flash}
              onEnvSaved={() => api.envStatus().then(setEnv)} />
          )}

          {tab === "keys" && (<>
            <KeysTab env={env} onSaved={(k) => { flash("金鑰已寫入 .env ✓"); api.envStatus().then(setEnv); void k; }} />

            <div style={{ borderTop: "1px solid var(--border)", marginTop: 14, paddingTop: 14 }}>
              <div className="form-row">
                <label>券商環境（shioaji）</label>
                <select
                  value={cfg.shioaji?.environment ?? "simulation"}
                  onChange={async (e) => {
                    const v = e.target.value;
                    if (v === "production" &&
                        !window.confirm("⚠️ 切換到「正式環境」後，Phase 5 交易功能將使用真實資金下單！\n\n確定要切換嗎？")) {
                      return; // 取消：不變更
                    }
                    setField("shioaji", "environment", v);
                    try {
                      await api.updateConfig("shioaji", { environment: v });
                      flash(v === "production" ? "已切換正式環境 ⚠️" : "已切換模擬環境 ✓");
                    } catch (err) { alert(String(err)); }
                  }}
                  style={cfg.shioaji?.environment === "production"
                    ? { borderColor: "var(--up)", color: "var(--up)", fontWeight: 700 } : {}}
                >
                  <option value="simulation">模擬環境（安全，預設）</option>
                  <option value="production">🔴 正式環境（真實資金）</option>
                </select>
              </div>
              <div className="form-hint">
                行情查詢兩者皆可；下單（Phase 5）在正式環境會動用真錢，且需 CA 憑證。
                切換立即生效，頂部徽章會同步顯示當前環境。
              </div>
            </div>
          </>)}
        </div>

        <div className="modal-foot">
          {saved && <span className="save-ok">{saved}</span>}
          {tab !== "keys" && tab !== "sched" && <button className="btn primary" onClick={() => saveSection(tab)}>儲存此頁</button>}
          <button className="btn" onClick={onClose}>關閉</button>
        </div>
      </div>
    </div>
  );
}

/** LLM 用量明細（今日/本月/累計花費，本地估算）。 */
function LlmUsageRow() {
  const [u, setU] = useState<import("../api").LlmUsage | null>(null);
  useEffect(() => { api.llmUsage().then(setU).catch(() => {}); }, []);
  if (!u) return null;
  return (
    <div style={{ display: "flex", gap: 18, fontSize: 12, color: "var(--text-dim)",
      background: "#0d1119", borderRadius: 6, padding: "8px 12px", marginTop: 8 }}>
      <span>今日 <b style={{ color: "var(--text)" }}>${u.today_usd.toFixed(2)}</b></span>
      <span>本月 <b style={{ color: "var(--text)" }}>${u.month_usd.toFixed(2)}</b></span>
      <span>累計 <b style={{ color: "var(--text)" }}>${u.total_usd.toFixed(2)}</b></span>
      <span>呼叫 {u.calls} 次</span>
      {u.remaining_usd != null && (
        <span>估計剩餘 <b style={{ color: "var(--text)" }}>${u.remaining_usd.toFixed(2)}</b></span>
      )}
    </div>
  );
}


/** 通知分頁：Telegram Bot 每日決策報告（token/chat_id 屬個人資訊存 .env，開關存 settings.yaml）。 */
function NotifyTab({ cfg, setCfg, env, flash, onEnvSaved }: {
  cfg: Record<string, any>;
  setCfg: Dispatch<SetStateAction<Record<string, any> | null>>;
  env: { telegram_token?: boolean; telegram_chat?: boolean } | null;
  flash: (m: string) => void;
  onEnvSaved: () => void;
}) {
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [testing, setTesting] = useState(false);
  const tg = cfg.notify?.telegram ?? {};

  const saveEnv = async (key: string, value: string, label: string) => {
    if (!value.trim()) return;
    try { await api.setEnv(key, value); flash(`${label} 已寫入 .env ✓`); onEnvSaved(); }
    catch (e) { alert(String(e)); }
  };
  const sendTest = async () => {
    setTesting(true);
    try {
      await api.notifyTest();
      flash("測試訊息已發送 ✓ 請查看 Telegram");
    } catch (e) { alert(String(e)); }
    finally { setTesting(false); }
  };

  return (
    <>
      <div className="form-row">
        <label>Bot Token {env?.telegram_token ? "✅" : "⚠️未設"}</label>
        <input type="password" placeholder="留空則不變更" value={token} onChange={(e) => setToken(e.target.value)} />
        <button className="btn" onClick={() => { saveEnv("TELEGRAM_BOT_TOKEN", token, "Token"); setToken(""); }}>存</button>
      </div>
      <div className="form-hint">在 Telegram 找 @BotFather → /newbot 建立 Bot 取得 token。</div>

      <div className="form-row">
        <label>Chat ID {env?.telegram_chat ? "✅" : "⚠️未設"}</label>
        <input type="text" placeholder="留空則不變更，例：123456789" value={chatId}
          onChange={(e) => setChatId(e.target.value)} />
        <button className="btn" onClick={() => { saveEnv("TELEGRAM_CHAT_ID", chatId, "Chat ID"); setChatId(""); }}>存</button>
      </div>
      <div className="form-hint">
        先傳任意訊息給你的 Bot，再開 api.telegram.org/bot&lt;token&gt;/getUpdates，
        取 message.chat.id（群組為負數）。兩者皆存 .env，不會進 git。
      </div>

      <div className="form-row">
        <label style={{ display: "flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
          <input type="checkbox" checked={!!tg.enabled}
            onChange={(e) => setCfg((c) => ({ ...c, notify: { ...c!.notify, telegram: { ...c!.notify?.telegram, enabled: e.target.checked } } }))} />
          啟用每日決策報告
        </label>
        <div style={{ flex: 1 }} />
        <button className="btn" disabled={testing} onClick={sendTest}>
          {testing ? "發送中…" : "📨 發送測試訊息"}
        </button>
      </div>
      <div className="form-hint">
        啟用後，每日流程（排程或手動）完成即推送報告：權益快照、開盤撮合、風控出場、
        AI 決策明細與明日委託。發送失敗只記日誌，不影響交易流程。
        開關改動請按下方「儲存此頁」。
      </div>
    </>
  );
}

function NumRow({ label, value, step = 1, onChange, money = false }: { label: string; value: number; step?: number; onChange: (v: number) => void; money?: boolean }) {
  return (
    <div className="form-row">
      <label>{label}</label>
      {money
        ? <MoneyInput value={value} onChange={onChange} />
        : <input type="number" step={step} value={value ?? 0} onChange={(e) => onChange(Number(e.target.value))} />}
    </div>
  );
}
const fmtTok = (n: number | null) =>
  n == null ? "?" : n >= 1_000_000 ? `${n / 1_000_000}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n);

function ModelRow({ label, value, models, onChange }: {
  label: string; value: string; models: ModelInfo[]; onChange: (v: string) => void;
}) {
  // 若目前設定值不在最新清單（如帶日期尾碼的舊 id），仍保留為可選項，避免被清掉。
  const known = models.some((m) => m.id === value);
  const sel = models.find((m) => m.id === value);
  return (
    <div className="form-row" style={{ flexWrap: "wrap" }}>
      <label>{label}</label>
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        {!known && value && <option value={value}>{value}（自訂）</option>}
        {models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.display_name} {m.supports_thinking ? "⚡" : ""} · ctx {fmtTok(m.context_window)} · out {fmtTok(m.max_output)}
          </option>
        ))}
      </select>
      {sel && (
        <div className="form-hint" style={{ flexBasis: "100%", margin: "4px 0 0" }}>
          {sel.supports_thinking ? "⚡ 支援思考模式（adaptive thinking）" : "✕ 不支援思考模式，將以一般模式呼叫"}
          {" · "}上下文上限 {fmtTok(sel.context_window)} tokens · 輸出上限 {fmtTok(sel.max_output)} tokens
        </div>
      )}
    </div>
  );
}

function KeysTab({ env, onSaved }: {
  env: { finmind_token: boolean; anthropic_key: boolean; shioaji_key?: boolean } | null;
  onSaved: (k: string) => void;
}) {
  const [finmind, setFinmind] = useState("");
  const [anthropic, setAnthropic] = useState("");
  const [sjKey, setSjKey] = useState("");
  const [sjSec, setSjSec] = useState("");
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
      <div className="form-row">
        <label>永豐 shioaji Key {env?.shioaji_key ? "✅" : "⚠️未設"}</label>
        <input type="password" placeholder="API Key（留空不變更）" value={sjKey} onChange={(e) => setSjKey(e.target.value)} />
        <button className="btn" onClick={() => save("SJ_API_KEY", sjKey)}>存</button>
      </div>
      <div className="form-row">
        <label>永豐 shioaji Secret</label>
        <input type="password" placeholder="Secret Key（留空不變更）" value={sjSec} onChange={(e) => setSjSec(e.target.value)} />
        <button className="btn" onClick={() => save("SJ_SEC_KEY", sjSec)}>存</button>
      </div>
      <div className="form-hint">
        FinMind 額度用罄時的回補備援源（也是 Phase 5 模擬/實盤交易必需）。
        取得：sinotrade.com.tw → Python API 管理頁申請 API Key/Secret
      </div>
    </>
  );
}
