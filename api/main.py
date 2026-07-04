"""REST API 後端（FastAPI）。

啟動：
    arch -arm64 .venv/bin/uvicorn api.main:app --reload --port 8000

所有端點都薄薄地包裝現有 src/ 的函式，不重複業務邏輯。
"""
from __future__ import annotations

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
from src.screener.screener import run_screener  # noqa: E402

app = FastAPI(title="StockTradingSystem API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 基礎 ----------
@app.get("/api/health")
def health():
    return {"status": "ok", "has_api_key": llm.has_api_key()}


@app.get("/api/data-status")
def data_status():
    return q.data_status().to_dict(orient="records")


@app.get("/api/stocks")
def stocks():
    """股票清單（供自選清單/搜尋）。"""
    df = q.list_stocks()
    return df.to_dict(orient="records")


# ---------- 行情 ----------
@app.get("/api/price/{stock_id}")
def price(stock_id: str, start: str | None = None, end: str | None = None, limit: int = 250):
    """日 K，格式對齊 TradingView lightweight-charts。"""
    df = q.get_price(stock_id, start, end)
    if df.empty:
        return {"candles": [], "volume": []}
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
    return ranked.to_dict(orient="records")


# ---------- 回測 ----------
class BacktestReq(BaseModel):
    strategy: str = "screener"
    start: str = "2022-06-01"
    end: str = "2025-06-30"
    cash: float = 1_000_000
    max_positions: int = 10


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
        "trades": res.trades.to_dict(orient="records") if not res.trades.empty else [],
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
    return q.brain_log(limit=limit).to_dict(orient="records")


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
    }


class EnvUpdate(BaseModel):
    key: str
    value: str


_ALLOWED_ENV = {"FINMIND_TOKEN", "ANTHROPIC_API_KEY"}


@app.post("/api/set-env")
def set_env(req: EnvUpdate):
    if req.key not in _ALLOWED_ENV:
        raise HTTPException(400, f"不允許設定 {req.key}")
    if not req.value.strip():
        raise HTTPException(400, "值不可為空")
    config_io.set_env_var(req.key, req.value.strip())
    return {"status": "saved"}


# ---------- 資料管理（初始化 / 背景回補）----------
_BACKFILL_JOB = "backfill"


@app.post("/api/init-db")
def init_db():
    db.init_db(get_settings().db_path)
    return {"status": "ok", "tables": list(db.SCHEMA.keys())}


class BackfillReq(BaseModel):
    mode: str = "limit"          # all | stocks | limit
    start: str = "2020-01-01"
    stocks: str | None = None    # 空格分隔
    limit: int | None = 50
    force: bool = False


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
