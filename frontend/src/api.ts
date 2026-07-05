// FastAPI 後端呼叫封裝（開發時經 vite proxy → :8000）

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function put<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function del<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export interface Quote {
  stock_id: string; name: string; last: number | null;
  change: number | null; change_pct: number | null; date?: string;
}
export interface Candle { time: string; open: number; high: number; low: number; close: number; }
export interface Vol { time: string; value: number; color: string; }
export interface ScreenerRow {
  rank: number; stock_id: string; stock_name: string; industry_category: string;
  score: number; momentum_20: number; chips_net_buy: number; revenue_yoy: number | null;
}
export interface SavedScreener {
  as_of: string; rows: ScreenerRow[]; top_n: number | null; created_at: string;
}
export interface DatasetStatus {
  table: string; label: string; desc: string;
  stocks: number; universe: number; coverage_pct: number;
  first_date: string | null; last_date: string | null;
  lag_days: number | null; status: "ok" | "stale" | "partial" | "missing"; hint: string;
}
export interface DataStatus {
  latest_trading_day: string | null;
  universe: number;
  datasets: DatasetStatus[];
  summary: { level: "ok" | "warn"; text: string };
}
export interface BacktestResult {
  metrics: Record<string, number | string | null>;
  equity_curve: { time: string; value: number }[];
  trades: Record<string, unknown>[];
}
export interface ModelInfo {
  id: string;
  display_name: string;
  context_window: number | null;
  max_output: number | null;
  supports_thinking: boolean;
}

export const api = {
  health: () => get<{ status: string; has_api_key: boolean; broker_env: "simulation" | "production"; broker_ready: boolean }>("/health"),
  dataStatus: () => get<DataStatus>("/data-status"),
  stocks: () => get<{ stock_id: string; stock_name: string; industry_category: string }[]>("/stocks"),
  quote: (id: string) => get<Quote>(`/quote/${id}`),
  price: (id: string, limit = 250, tf = "D", adjusted = true) =>
    get<{ candles: Candle[]; volume: Vol[] }>(`/price/${id}?limit=${limit}&tf=${tf}&adjusted=${adjusted}`),
  indices: () => get<Quote[]>("/indices"),
  stocksOverview: () => get<Record<string, any>[]>("/stocks/overview"),
  stockDetail: (id: string) => get<Record<string, any>>(`/stocks/${id}/detail`),
  stockSeries: (id: string) => get<Record<string, any>>(`/stocks/${id}/series`),
  stockEvents: (id: string) => get<{
    dividends: { date: string; kind: string; amount: number | null }[];
    capital_changes: { date: string; kind: string; before: number; after: number }[];
  }>(`/stocks/${id}/events`),
  uiLayoutGet: () => get<{ layout: any[] | null }>("/ui/layout"),
  uiLayoutSave: (layout: any[]) => put<{ saved: boolean }>("/ui/layout", { layout }),
  uiLayoutReset: () => del<{ reset: boolean }>("/ui/layout"),
  schedulerStatus: () => get<Record<string, any>[]>("/scheduler/status"),
  schedulerConfig: (name: string, enabled: boolean, time: string) =>
    post<{ saved: boolean }>("/scheduler/config", { name, enabled, time }),
  schedulerRun: (name: string) => post<{ started: boolean }>(`/scheduler/run/${name}`, {}),
  qualityCheck: () => get<Record<string, any>>("/quality-check"),
  scanner: (kind: string, count = 20) =>
    get<Record<string, any>[]>(`/scanner?kind=${kind}&count=${count}`),
  disposition: (activeOn?: string) =>
    get<Record<string, any>[]>(`/disposition${activeOn ? `?active_on=${activeOn}` : ""}`),
  screener: (asOf: string, topN = 30) =>
    get<ScreenerRow[]>(`/screener?as_of=${asOf}&top_n=${topN}`),
  screenerSaved: (asOf: string) =>
    get<SavedScreener | null>(`/screener/saved?as_of=${asOf}`),
  screenerHistory: () =>
    get<{ as_of: string; created_at: string }[]>("/screener/history"),
  watchlist: () => get<string[]>("/watchlist"),
  watchlistAdd: (id: string) => post<string[]>(`/watchlist/${id}`, {}),
  watchlistRemove: (id: string) => del<string[]>(`/watchlist/${id}`),
  backtest: (body: { strategy: string; start: string; end: string; cash: number; max_positions: number }) =>
    post<BacktestResult>("/backtest", body),
  backtestStart: (body: { strategy: string; start: string; end: string; cash: number; max_positions: number }) =>
    post<{ started: boolean }>("/backtest/start", body),
  backtestStatus: () =>
    get<{ running: boolean; log: string; result: BacktestResult | null }>("/backtest/status"),
  analyze: (asOf: string, topN: number) =>
    post<Record<string, any>[]>("/analyze", { as_of: asOf, top_n: topN }),
  tradePlans: (asOf: string) => get<Record<string, any>[]>(`/trade-plans?as_of=${asOf}`),
  brainLog: (limit = 100) => get<Record<string, any>[]>(`/brain-log?limit=${limit}`),
  // 設定
  models: (topN = 5) => get<ModelInfo[]>(`/models?top_n=${topN}`),
  getConfig: () => get<Record<string, any>>("/config"),
  updateConfig: (section: string, values: Record<string, unknown>) =>
    put<{ status: string }>("/config", { section, values }),
  envStatus: () => get<{ finmind_token: boolean; anthropic_key: boolean; shioaji_key: boolean; telegram_token: boolean; telegram_chat: boolean }>("/env-status"),
  setEnv: (key: string, value: string) => post<{ status: string }>("/set-env", { key, value }),
  notifyTest: () => post<{ sent: boolean }>("/notify/test", {}),
  clearAiData: () => post<{ status: string; deleted: Record<string, any> }>("/ai-data/clear", {}),
  // Phase 5：模擬交易
  portfolio: () => get<Record<string, any>>("/portfolio"),
  portfolioReset: () => post<{ status: string; cash: number }>("/portfolio/reset", {}),
  tradingToggle: (enabled: boolean) => post<{ trading_enabled: boolean }>("/trading/toggle", { enabled }),
  dailyRun: (asOf?: string, topN?: number) =>
    post<{ started: boolean }>("/daily/run", { as_of: asOf ?? null, top_n: topN ?? null }),
  dailyStatus: () => get<{ running: boolean; log: string }>("/daily/status"),
  // Phase 4：反思與向量記憶
  memoryStatus: () => get<{ experiences: number; rules: number; blocked: number }>("/memory/status"),
  memoryRules: () => get<Record<string, any>[]>("/memory/rules"),
  memoryRuleToggle: (ruleId: string, active: boolean) =>
    post<{ status: string }>("/memory/rules/toggle", { rule_id: ruleId, active }),
  memoryExperiences: (limit = 30) => get<Record<string, any>[]>(`/memory/experiences?limit=${limit}`),
  reflectRun: () => post<Record<string, any>>("/reflect/run", {}),
  // 資料管理
  backfillStart: (body: { mode: string; start: string; stocks?: string; limit?: number; force: boolean; datasets?: string[]; auto_wait?: boolean }) =>
    post<{ started: boolean; running: boolean; cmd: string }>("/backfill/start", body),
  backfillStatus: () => get<{
    running: boolean;
    progress: { pass: string; current: number; total: number; stock_id: string; rows: number } | null;
    log: string;
  }>("/backfill/status"),
  backfillStop: () => post<{ stopped: boolean }>("/backfill/stop", {}),
};
