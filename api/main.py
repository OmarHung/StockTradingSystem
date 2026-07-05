"""REST API 後端（FastAPI）。

啟動：
    arch -arm64 .venv/bin/uvicorn api.main:app --reload --port 8000

所有端點都薄薄地包裝現有 src/ 的函式，不重複業務邏輯。
"""
from __future__ import annotations

import json
import os

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)  # 讀 .env（含 API keys）
# 防護：空的 ANTHROPIC_AUTH_TOKEN 會讓 anthropic SDK 壞掉
if not (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip():
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from src import config_io  # noqa: E402
from src import jobs  # noqa: E402
from src.agents import pipeline  # noqa: E402
from src.backtest.runner import run_backtest  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.data import database as db  # noqa: E402
from src.data import query as q  # noqa: E402
from src.llm import client as llm  # noqa: E402
from src.llm import models as llm_models  # noqa: E402
from src.screener.screener import run_screener  # noqa: E402

app = FastAPI(title="StockTradingSystem API", version="0.1.0")

# 確保資料表齊全（新增資料集後，API 先於回補啟動也不會查表失敗）
db.init_db(get_settings().db_path)


@app.on_event("startup")
async def _start_scheduler():
    """後端內建排程器（取代 launchd）：asyncio 常駐迴圈，觸發走 jobs.py 子行程。"""
    import asyncio
    from src import scheduler
    asyncio.create_task(scheduler.run_loop())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _records(df):
    """DataFrame → list[dict]，把 NaN/inf 轉成 null（JSON 合法）。"""
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records"))


# ---------- 基礎 ----------
@app.get("/api/health")
def health():
    from src.data import shioaji_source
    return {
        "status": "ok",
        "has_api_key": llm.has_api_key(),
        "broker_env": shioaji_source.current_env(),   # simulation / production
        "broker_ready": shioaji_source.available(),
    }


@app.get("/api/models")
def models(top_n: int = 5):
    """Claude 最新前 N 個模型與能力（供設定頁下拉切換）。"""
    return llm_models.list_models(top_n=top_n)


@app.get("/api/data-status")
def data_status():
    """資料健康報告：各資料集覆蓋率/新鮮度/狀態燈 + 是否需要回補的結論。"""
    return q.data_status()  # 已是 dict（datasets + summary）


@app.get("/api/stocks")
def stocks():
    """股票清單（供自選清單/搜尋）。"""
    df = q.list_stocks()
    return _records(df)


@app.get("/api/stocks/overview")
def stocks_overview():
    """全市場股票總覽（含未下載/處置股）：市場、產業、下載狀態。"""
    return q.stocks_overview()


@app.get("/api/stocks/{stock_id}/detail")
def stock_detail(stock_id: str):
    """單一股票資料總覽：覆蓋範圍 + 最新報價/籌碼/融資/營收 + 處置狀態。"""
    return q.stock_detail(stock_id)


@app.get("/api/stocks/{stock_id}/series")
def stock_series(stock_id: str):
    """股票詳情頁圖表序列：還原價/法人淨買/融資餘額/月營收/本益比。"""
    return q.stock_series(stock_id)


@app.get("/api/stocks/{stock_id}/events")
def stock_events(stock_id: str):
    """K 線標記用事件：除權息（日期/類別/配發）+ 分割減資（公司行動）。"""
    with db.connect(get_settings().db_path) as conn:
        div = [{"date": r[0], "kind": r[1] or "權息", "amount": r[2]}
               for r in conn.execute(
                   "SELECT date, kind, dividend FROM dividend WHERE stock_id=? ORDER BY date",
                   (stock_id,))]
        cap = [{"date": r[0], "kind": r[1], "before": r[2], "after": r[3]}
               for r in conn.execute(
                   "SELECT date, kind, before_price, after_price FROM capital_change "
                   "WHERE stock_id=? ORDER BY date", (stock_id,))]
    return {"dividends": div, "capital_changes": cap}


# ---------- 行情 ----------
@app.get("/api/price/{stock_id}")
def price(stock_id: str, start: str | None = None, end: str | None = None,
          limit: int = 250, tf: str = "D", adjusted: bool = True):
    """K 線（tf=D/W/M 多時間框架；adjusted=還原價，預設開）。格式對齊 lightweight-charts。"""
    df = q.get_price(stock_id, start, end, adjusted=adjusted)
    if df.empty:
        return {"candles": [], "volume": []}
    if tf in ("W", "M"):
        df = q.resample_price(df, tf)
    df = df.tail(limit)
    candles = [
        {"time": r.date, "open": r.open, "high": r.high, "low": r.low, "close": r.close}
        for r in df.itertuples()
    ]
    volume = [
        {"time": r.date, "value": int(r.volume or 0),
         "color": "#26a69a" if r.close >= r.open else "#ef5350"}
        for r in df.itertuples()
    ]
    return {"candles": candles, "volume": volume}


@app.get("/api/quote/{stock_id}")
def quote(stock_id: str):
    """最新報價（收盤基礎；Phase 5 接 shioaji 後改即時）。供自選清單顯示。"""
    df = q.get_price(stock_id).tail(2)
    names = q.list_stocks()
    name = ""
    row = names[names["stock_id"] == stock_id]
    if not row.empty:
        name = row.iloc[0]["stock_name"]
    if df.empty:
        return {"stock_id": stock_id, "name": name, "last": None, "change": None, "change_pct": None}
    last = float(df.iloc[-1]["close"])
    prev = float(df.iloc[-2]["close"]) if len(df) > 1 else last
    return {
        "stock_id": stock_id, "name": name, "last": last,
        "change": round(last - prev, 2),
        "change_pct": round((last / prev - 1) * 100, 2) if prev else 0.0,
        "date": df.iloc[-1]["date"],
    }


# ---------- 選股 ----------
@app.get("/api/screener")
def screener(as_of: str, top_n: int | None = None):
    ranked = run_screener(as_of)
    if ranked.empty:
        return []
    if top_n:
        ranked = ranked.head(top_n)
    rows = _records(ranked)
    q.save_screener_result(as_of, rows, top_n)  # 快照：重整/重啟後可還原
    return rows


@app.get("/api/screener/saved")
def screener_saved(as_of: str):
    """讀取某基準日已保存的選股結果（無則 null）。"""
    return q.load_screener_result(as_of)


@app.get("/api/screener/history")
def screener_history():
    """已保存選股結果的日期清單（新到舊）。"""
    return q.list_screener_dates()


# ---------- 自選清單 ----------
@app.get("/api/watchlist")
def watchlist():
    """自選股代碼清單。"""
    return q.list_watchlist()


@app.post("/api/watchlist/{stock_id}")
def watchlist_add(stock_id: str):
    """加入自選，回傳更新後清單。"""
    return q.add_watchlist(stock_id)


@app.delete("/api/watchlist/{stock_id}")
def watchlist_remove(stock_id: str):
    """移除自選，回傳更新後清單。"""
    return q.remove_watchlist(stock_id)


# ---------- 回測 ----------
class BacktestReq(BaseModel):
    strategy: str = "screener"
    start: str = "2022-06-01"
    end: str = "2025-06-30"
    cash: float = 1_000_000
    max_positions: int = 10


_BT_JOB = "backtest"
_BT_RESULT = Path("logs/jobs/backtest_result.json")


@app.post("/api/backtest/start")
def backtest_start(req: BacktestReq):
    """背景執行回測（jobs.py 子行程）：可輪詢 log，結果寫檔供取回。"""
    if jobs.is_running(_BT_JOB):
        raise HTTPException(409, "回測已在執行中")
    _BT_RESULT.unlink(missing_ok=True)
    args = ["scripts.run_backtest", "--strategy", req.strategy,
            "--start", req.start, "--end", req.end,
            "--max-positions", str(req.max_positions),
            "--json-out", str(_BT_RESULT)]
    if req.cash:
        args += ["--cash", str(req.cash)]
    started = jobs.start_job(_BT_JOB, args)
    return {"started": started}


@app.get("/api/backtest/status")
def backtest_status():
    """回測進度：running + log 尾巴 + 完成後的完整結果。"""
    running = jobs.is_running(_BT_JOB)
    result = None
    if not running and _BT_RESULT.exists():
        try:
            result = json.loads(_BT_RESULT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"running": running, "log": jobs.read_log(_BT_JOB, tail=40), "result": result}


@app.post("/api/backtest")
def backtest(req: BacktestReq):
    try:
        res, m = run_backtest(
            req.strategy, req.start, req.end,
            initial_cash=req.cash, max_positions=req.max_positions,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    ec = res.equity_curve
    return {
        "metrics": m,
        "equity_curve": [{"time": d, "value": float(v)} for d, v in ec.items()],
        "trades": _records(res.trades),
    }


# ---------- LLM 選股報告 ----------
class AnalyzeReq(BaseModel):
    as_of: str
    top_n: int = 3


@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    if not llm.has_api_key():
        raise HTTPException(400, "未設定 ANTHROPIC_API_KEY")
    ranked = run_screener(req.as_of)
    if ranked.empty:
        return []
    picks = ranked["stock_id"].head(req.top_n).tolist()
    return pipeline.analyze_stocks(picks, req.as_of)


@app.get("/api/trade-plans")
def trade_plans(as_of: str):
    return pipeline.load_plans(as_of)


@app.get("/api/brain-log")
def brain_log(limit: int = 100):
    return _records(q.brain_log(limit=limit))


@app.get("/api/friction")
def friction(limit: int = 100):
    """Guard pipeline 駁回紀錄（供檢討風控鬆緊）。"""
    from src.data import database as _db
    with _db.connect(get_settings().db_path) as conn:
        df = _db.read_sql(
            conn, "SELECT id, ts, as_of, stock_id, gate, reason FROM friction_log "
                  "ORDER BY id DESC LIMIT ?", (limit,))
    return _records(df)


@app.post("/api/ai-data/clear")
def clear_ai_data():
    """清除所有 AI 產出：分析記錄、交易計畫、Guard 駁回、反思記憶。

    不動行情資料（股價/法人/營收等）、模擬交易帳本（positions/orders/fills），
    也不動智慧選股快照（screener_result 是純量化排名，非 LLM 產出，可隨時重算）。
    """
    tables = ("brain_log", "trade_plan", "friction_log")
    deleted: dict = {}
    with db.connect(get_settings().db_path) as conn:
        for t in tables:
            deleted[t] = conn.execute(f"DELETE FROM {t}").rowcount
    from src.memory import store as memory_store
    deleted["memory"] = memory_store.clear_all()
    return {"status": "cleared", "deleted": deleted}


# ---------- Phase 5：模擬交易（持倉/績效/每日流程/緊急停止） ----------
_DAILY_JOB = "daily"


@app.get("/api/portfolio")
def portfolio():
    """持倉 + 現金 + 掛單 + 績效摘要（權益曲線 vs TAIEX）。"""
    from src.broker.paper import PaperBroker
    from src.report.performance import performance_summary

    broker = PaperBroker()
    pos = broker.positions()
    rows = []
    for r in pos.to_dict(orient="records"):
        px = q.get_price(r["stock_id"])
        last = float(px["close"].iloc[-1]) if not px.empty else r["avg_cost"]
        mv = r["shares"] * last
        cost = r["shares"] * r["avg_cost"]
        rows.append({**r, "last": last, "market_value": round(mv, 0),
                     "unrealized_pnl": round(mv - cost, 0),
                     "unrealized_pct": round((last / r["avg_cost"] - 1) * 100, 2) if r["avg_cost"] else 0})
    return {
        "cash": broker.cash,
        "trading_enabled": broker.trading_enabled(),
        "positions": rows,
        "pending_orders": _records(broker.pending_orders()),
        "orders": _records(broker.orders(100)),
        "fills": _records(broker.fills(100)),
        "performance": performance_summary(),
    }


@app.post("/api/portfolio/reset")
def portfolio_reset():
    """重置模擬帳本（清空持倉/委託/成交/權益史，現金回到設定資金）。"""
    from src.data import database as _db
    with _db.connect(get_settings().db_path) as conn:
        for t in ("positions", "orders", "fills", "equity_history"):
            conn.execute(f"DELETE FROM {t}")
        cap = float(get_settings()["capital"]["total"])
        conn.execute("INSERT OR REPLACE INTO broker_state (key, value) VALUES ('cash', ?)", (str(cap),))
    return {"status": "reset", "cash": float(get_settings()["capital"]["total"])}


class TradingToggle(BaseModel):
    enabled: bool


@app.post("/api/trading/toggle")
def trading_toggle(req: TradingToggle):
    """緊急停止/恢復：停用時每日流程只做保護性出場，不開新倉。"""
    from src.broker.paper import PaperBroker
    PaperBroker().set_trading_enabled(req.enabled)
    return {"trading_enabled": req.enabled}


class DailyRunReq(BaseModel):
    as_of: str | None = None
    top_n: int | None = None   # None = 用 settings.yaml daily.top_n


@app.post("/api/daily/run")
def daily_run(req: DailyRunReq):
    """背景執行每日主流程（撮合→風控→快照→決策掛單）。"""
    if jobs.is_running(_DAILY_JOB):
        raise HTTPException(409, "每日流程已在執行中")
    args = ["scripts.run_daily"]
    if req.top_n:
        args += ["--top-n", str(req.top_n)]
    if req.as_of:
        args += ["--as-of", req.as_of]
    started = jobs.start_job(_DAILY_JOB, args)
    return {"started": started}


@app.get("/api/daily/status")
def daily_status():
    return {"running": jobs.is_running(_DAILY_JOB),
            "log": jobs.read_log(_DAILY_JOB, tail=25)}


# ---------- 內建排程器（監控/調整/手動觸發）----------
class SchedulerConfigReq(BaseModel):
    name: str          # dataupdate / daily
    enabled: bool
    time: str          # "HH:MM"


@app.get("/api/scheduler/status")
def scheduler_status():
    from src import scheduler
    return scheduler.status()


@app.post("/api/scheduler/config")
def scheduler_config(req: SchedulerConfigReq):
    from src import scheduler
    if req.name not in scheduler.JOB_DEFS:
        raise HTTPException(400, f"未知排程：{req.name}")
    try:
        hh, mm = req.time.split(":")
        assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except (ValueError, AssertionError):
        raise HTTPException(400, "時間格式須為 HH:MM（24 小時制）")
    raw = config_io.load_raw()
    raw.setdefault("scheduler", {}).setdefault(req.name, {})
    raw["scheduler"][req.name]["enabled"] = req.enabled
    raw["scheduler"][req.name]["time"] = req.time
    config_io.save_raw(raw)
    return {"saved": True}


@app.post("/api/scheduler/run/{name}")
def scheduler_run_now(name: str):
    from src import scheduler
    if name not in scheduler.JOB_DEFS:
        raise HTTPException(400, f"未知排程：{name}")
    started = scheduler.trigger(name, source="manual")
    if not started:
        raise HTTPException(409, "該任務已在執行中")
    return {"started": True}


# ---------- Phase 4：反思與向量記憶 ----------
@app.get("/api/memory/status")
def memory_status():
    from src.memory import store
    return store.count_all()


@app.get("/api/memory/rules")
def memory_rules():
    from src.memory import store
    return store.list_rules()


class RuleToggle(BaseModel):
    rule_id: str
    active: bool


@app.post("/api/memory/rules/toggle")
def memory_rule_toggle(req: RuleToggle):
    from src.memory import store
    store.set_rule_active(req.rule_id, req.active)
    return {"status": "ok"}


@app.get("/api/memory/experiences")
def memory_experiences(limit: int = 30):
    from src.memory import store
    return store.recent_experiences(limit)


@app.post("/api/reflect/run")
def reflect_run():
    """一鍵反思：評估到期計畫 → 同步 friction → LLM 歸納規則。"""
    from src.memory import outcome, reflect
    evaluated = outcome.evaluate_pending()
    outcome.sync_friction_to_blocked()
    result = reflect.run_reflection()
    return {"evaluation": evaluated, "reflection": result}


# ---------- 設定 ----------
@app.get("/api/config")
def get_config():
    return dict(config_io.load_raw())


class ConfigUpdate(BaseModel):
    section: str
    values: dict


@app.put("/api/config")
def update_config(req: ConfigUpdate):
    config_io.update_section(req.section, req.values)
    return {"status": "saved"}


# ---------- API 金鑰（寫入 .env）----------
@app.get("/api/env-status")
def env_status():
    """回報金鑰是否已設定（不回傳值）。"""
    return {
        "finmind_token": bool(os.getenv("FINMIND_TOKEN")),
        "anthropic_key": bool(os.getenv("ANTHROPIC_API_KEY")),
        "shioaji_key": bool(os.getenv("SJ_API_KEY")) and bool(os.getenv("SJ_SEC_KEY")),
    }


class EnvUpdate(BaseModel):
    key: str
    value: str


_ALLOWED_ENV = {"FINMIND_TOKEN", "ANTHROPIC_API_KEY", "SJ_API_KEY", "SJ_SEC_KEY"}


@app.post("/api/set-env")
def set_env(req: EnvUpdate):
    if req.key not in _ALLOWED_ENV:
        raise HTTPException(400, f"不允許設定 {req.key}")
    if not req.value.strip():
        raise HTTPException(400, "值不可為空")
    config_io.set_env_var(req.key, req.value.strip())
    return {"status": "saved"}


# ---------- 資料管理（背景回補；資料表建立由回補自動處理）----------
_BACKFILL_JOB = "backfill"


class BackfillReq(BaseModel):
    mode: str = "limit"          # all | stocks | limit
    start: str = "2020-01-01"
    stocks: str | None = None    # 空格分隔
    limit: int | None = 50
    force: bool = False
    datasets: list[str] | None = None   # 資料類型子集（None=全部）
    auto_wait: bool = True              # 402 額度用罄自動等下個整點續跑


@app.post("/api/backfill/start")
def backfill_start(req: BackfillReq):
    if jobs.is_running(_BACKFILL_JOB):
        raise HTTPException(409, "回補已在執行中")
    args = ["scripts.backfill", "--start", req.start]
    if req.mode == "stocks" and req.stocks and req.stocks.strip():
        args += ["--stocks", *req.stocks.split()]
    elif req.mode == "limit" and req.limit:
        args += ["--limit", str(int(req.limit))]
    if req.force:
        args += ["--force"]
    if req.datasets:
        args += ["--datasets", ",".join(req.datasets)]
    if req.auto_wait:
        args += ["--auto-wait"]
    started = jobs.start_job(_BACKFILL_JOB, args)
    return {"started": started, "running": jobs.is_running(_BACKFILL_JOB), "cmd": " ".join(args)}


@app.get("/api/backfill/status")
def backfill_status():
    log_text = jobs.read_log(_BACKFILL_JOB, tail=40)
    # 解析最後一筆 @@PROGRESS@@ 結構化進度
    progress = None
    import json as _json
    for line in reversed(log_text.splitlines()):
        if line.startswith("@@PROGRESS@@"):
            try:
                progress = _json.loads(line[len("@@PROGRESS@@"):].strip())
            except Exception:  # noqa: BLE001
                pass
            break
    # log 顯示時濾掉 PROGRESS 雜訊
    clean = "\n".join(l for l in log_text.splitlines() if not l.startswith("@@PROGRESS@@"))
    return {
        "running": jobs.is_running(_BACKFILL_JOB),
        "progress": progress,
        "log": "\n".join(clean.splitlines()[-12:]),
    }


@app.post("/api/backfill/stop")
def backfill_stop():
    return {"stopped": jobs.stop_job(_BACKFILL_JOB)}


@app.get("/api/quality-check")
def quality_check():
    """資料品質檢查：缺日偵測（對照 TAIEX 交易日曆）+ OHLC 結構異常。"""
    return q.quality_check()


@app.get("/api/indices")
def indices():
    """大盤指數報價（TAIEX 加權 / TPEx 櫃買），供頂部狀態列。"""
    out = []
    for sid, label in (("TAIEX", "加權"), ("TPEx", "櫃買")):
        df = q.get_price(sid).tail(2)
        if df.empty:
            continue
        last = float(df.iloc[-1]["close"])
        prev = float(df.iloc[-2]["close"]) if len(df) > 1 else last
        out.append({
            "stock_id": sid, "name": label, "last": last,
            "change": round(last - prev, 2),
            "change_pct": round((last / prev - 1) * 100, 2) if prev else 0.0,
            "date": df.iloc[-1]["date"],
        })
    return out


@app.get("/api/disposition")
def disposition(active_on: str | None = None):
    """處置股名單（active_on 給日期則只回傳仍在處置期間者）。"""
    return _records(q.list_disposition(active_on))


@app.get("/api/scanner")
def scanner(kind: str = "change_pct_up", count: int = 20):
    """即時排行（shioaji scanners）：change_pct_up/change_pct_down/amount/volume。"""
    from src.data import shioaji_source
    if not shioaji_source.available():
        raise HTTPException(400, "排行需要 shioaji 金鑰（設定 → API 金鑰）")
    try:
        return shioaji_source.get_scanners(kind, count)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"排行查詢失敗：{e}")
