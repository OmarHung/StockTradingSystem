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
export interface BacktestResult {
  metrics: Record<string, number | string | null>;
  equity_curve: { time: string; value: number }[];
  trades: Record<string, unknown>[];
}

export const api = {
  health: () => get<{ status: string; has_api_key: boolean }>("/health"),
  dataStatus: () => get<Record<string, unknown>[]>("/data-status"),
  stocks: () => get<{ stock_id: string; stock_name: string; industry_category: string }[]>("/stocks"),
  quote: (id: string) => get<Quote>(`/quote/${id}`),
  price: (id: string, limit = 250) =>
    get<{ candles: Candle[]; volume: Vol[] }>(`/price/${id}?limit=${limit}`),
  screener: (asOf: string, topN = 30) =>
    get<ScreenerRow[]>(`/screener?as_of=${asOf}&top_n=${topN}`),
  backtest: (body: { strategy: string; start: string; end: string; cash: number; max_positions: number }) =>
    post<BacktestResult>("/backtest", body),
  analyze: (asOf: string, topN: number) =>
    post<Record<string, any>[]>("/analyze", { as_of: asOf, top_n: topN }),
  tradePlans: (asOf: string) => get<Record<string, any>[]>(`/trade-plans?as_of=${asOf}`),
  brainLog: (limit = 100) => get<Record<string, any>[]>(`/brain-log?limit=${limit}`),
};
