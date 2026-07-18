"""REST API 後端（FastAPI）。

啟動：
    arch -arm64 .venv/bin/uvicorn api.main:app --reload --port 8000

所有端點都薄薄地包裝現有 src/ 的函式，不重複業務邏輯。
"""
from __future__ import annotations

import json
import os
import threading
import time

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket
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
from src.logging_setup import get_logger, setup_logging  # noqa: E402
from src.screener.screener import run_screener  # noqa: E402

# API 是 VPS 上的常駐主行程：這裡不初始化的話，API/排程器的 log 只剩 stderr、logs/system.log 不會生成
_cfg = get_settings()
setup_logging(_cfg.log_level, _cfg.log_dir)

log = get_logger("api")

app = FastAPI(title="StockTradingSystem API", version="0.1.0")

# 確保資料表齊全（新增資料集後，API 先於回補啟動也不會查表失敗）
db.init_db(get_settings().db_path)


# 常駐背景 task 的強參考：event loop 只持弱參考，不保存會被 GC 中途回收，
# 排程心臟／行情聚合器就這樣靜默消失。存 app.state 保命並留屍檢線索。
app.state.bg_tasks = set()


def _spawn_bg(coro):
    import asyncio
    t = asyncio.create_task(coro)
    app.state.bg_tasks.add(t)
    t.add_done_callback(lambda tt: (
        app.state.bg_tasks.discard(tt),
        tt.cancelled() or tt.exception() and log.error("背景 task 異常結束：%r", tt.exception()),
    ))
    return t


@app.on_event("startup")
async def _start_scheduler():
    """後端內建排程器（取代 launchd）：asyncio 常駐迴圈，觸發走 jobs.py 子行程。"""
    from src import scheduler
    _spawn_bg(scheduler.run_loop())


@app.on_event("startup")
async def _start_realtime():
    """即時 K 線服務：綁定事件迴圈（券商登入延到首次訂閱才發生）。"""
    from src import realtime
    realtime.service.attach_loop()


@app.on_event("shutdown")
async def _stop_bg_tasks():
    for t in list(app.state.bg_tasks):
        t.cancel()

@app.middleware("http")
async def _log_requests(request, call_next):
    """請求記錄：例外全 traceback、4xx/5xx/慢請求進 system.log（正常請求 DEBUG 級）。"""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("%s %s 未捕捉例外", request.method, request.url.path)
        raise
    dur_ms = (time.perf_counter() - start) * 1000
    line = f"{request.method} {request.url.path} -> {response.status_code} ({dur_ms:.0f}ms)"
    if response.status_code >= 500:
        log.error(line)
    elif response.status_code >= 400:
        log.warning(line)
    elif dur_ms > 2000:
        log.info("%s [慢]", line)
    else:
        log.debug(line)
    return response


_ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173",
                    "http://localhost:8000", "http://127.0.0.1:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _csrf_guard(request, call_next):
    """CSRF 防護：CORS 只限制「回應可否被讀取」，擋不住無 body/無自訂 header 的
    跨站 simple request 送達（惡意網頁可對 localhost 盲發 POST 清空帳本/觸發花費）。
    對有副作用的方法，若帶了 Origin 且不在白名單即 403（同源請求通常不帶 Origin，
    帶了就必須匹配）。"""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in _ALLOWED_ORIGINS:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "CSRF: origin 不被允許"}, status_code=403)
    return await call_next(request)


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


@app.get("/api/logs")
def logs_tail(name: str = "system", tail: int = 300):
    """讀取 log 尾端（除錯用）：name=system 或 logs/jobs/ 下的任一 job log。"""
    cfg = get_settings()
    files = {"system": cfg.log_dir / "system.log"}
    jobs_dir = cfg.log_dir / "jobs"
    if jobs_dir.is_dir():
        for p in sorted(jobs_dir.glob("*.log")):
            files[p.stem] = p
    if name not in files:
        raise HTTPException(404, f"log 不存在，可用：{', '.join(files)}")
    path = files[name]
    if not path.exists():
        return {"name": name, "available": sorted(files), "lines": []}
    tail = max(1, min(tail, 5000))
    with path.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-tail:]
    return {"name": name, "available": sorted(files),
            "path": str(path), "lines": [ln.rstrip("\n") for ln in lines]}


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


# ---------- WebUI 介面狀態（面板佈局，存 DB → 跨瀏覽器一致）----------
class UiLayoutReq(BaseModel):
    layout: list


@app.get("/api/ui/layout")
def ui_layout_get():
    with db.connect(get_settings().db_path) as conn:
        row = conn.execute("SELECT value FROM ui_state WHERE key='layout'").fetchone()
    return {"layout": json.loads(row[0]) if row else None}


@app.put("/api/ui/layout")
def ui_layout_save(req: UiLayoutReq):
    with db.connect(get_settings().db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO ui_state (key, value) VALUES ('layout', ?)",
                     (json.dumps(req.layout),))
    return {"saved": True}


@app.delete("/api/ui/layout")
def ui_layout_reset():
    with db.connect(get_settings().db_path) as conn:
        conn.execute("DELETE FROM ui_state WHERE key='layout'")
    return {"reset": True}


# ---------- 行情 ----------
@app.get("/api/price/{stock_id}")
def price(stock_id: str, start: str | None = None, end: str | None = None,
          limit: int = 250, tf: str = "D", adjusted: bool = True):
    """K 線（tf=D/W/M 多時間框架；adjusted=還原價，預設開）。格式對齊 lightweight-charts。

    include_today：官方日行情未出時，今天的日 K 由分鐘資料即時合成（盤中跳動）。
    切換股票時先同步該檔今天的分鐘資料（20 秒節流＋收盤後落定即跳過，不重複打 API），
    確保任何一檔一點開就是最新 K 棒，不必等排程回補。
    """
    from src.data import shioaji_source
    if shioaji_source.available():
        try:
            with db.connect(get_settings().db_path) as conn:
                shioaji_source.ensure_minute_bars(conn, stock_id, days=3)
        except Exception as e:  # noqa: BLE001 — 同步失敗仍回庫存資料
            log.warning("切換同步 %s 分鐘資料失敗（回庫存資料）：%s", stock_id, e)
    df = q.get_price(stock_id, start, end, adjusted=adjusted, include_today=True)
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


@app.get("/api/kbars/{stock_id}")
def kbars(stock_id: str, tf: int = 1, days: int = 10, limit: int = 500):
    """分鐘 K（tf=1/5/15/60）。像看盤軟體：先自動回補缺口（shioaji），再回庫存資料。

    time 為「台北牆鐘時間當作 UTC」的 epoch 秒（前端 lightweight-charts 免時區換算）。
    """
    from src.data import shioaji_source
    from src.realtime import taipei_epoch

    if tf not in (1, 5, 15, 60):
        raise HTTPException(400, "tf 須為 1/5/15/60（分鐘）")
    days = max(1, min(days, 120))
    if shioaji_source.available():
        try:
            with db.connect(get_settings().db_path) as conn:
                shioaji_source.ensure_minute_bars(conn, stock_id, days=days)
        except Exception as e:  # noqa: BLE001 — 回補失敗仍回庫存資料
            log.warning("分鐘K自動回補 %s 失敗（回庫存資料）：%s", stock_id, e)
    import datetime as _dt

    import pandas as pd
    since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    df = q.get_minute_price(stock_id, since=since, tf_min=tf).tail(limit)
    if df.empty:
        return {"candles": [], "volume": []}
    times = [taipei_epoch(t) for t in pd.to_datetime(df["ts"])]
    candles = [
        {"time": t, "open": r.open, "high": r.high, "low": r.low, "close": r.close}
        for t, r in zip(times, df.itertuples())
    ]
    volume = [
        {"time": t, "value": int(r.volume or 0),
         "color": "#26a69a" if r.close >= r.open else "#ef5350"}
        for t, r in zip(times, df.itertuples())
    ]
    return {"candles": candles, "volume": volume}


@app.websocket("/api/ws/kbars/{stock_id}")
async def ws_kbars(ws: WebSocket, stock_id: str):
    """盤中即時推播：bar1m（進行中 1 分 K）+ day（今日日 K）。

    前端開圖即連線；首位訂閱者觸發 shioaji tick 訂閱，最後一位離開自動退訂。
    """
    import asyncio
    from src import realtime

    await ws.accept()
    queue = await realtime.service.register(stock_id)

    async def _send():
        while True:
            await ws.send_json(await queue.get())

    send_task = asyncio.create_task(_send())
    recv_task = asyncio.create_task(ws.receive_text())  # 只為偵測客戶端斷線
    try:
        done, pending = await asyncio.wait({send_task, recv_task},
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:   # 消化斷線例外，避免 unretrieved exception 警告
            if not t.cancelled():
                t.exception()
    finally:
        await realtime.service.unregister(stock_id, queue)


@app.get("/api/quote/{stock_id}")
def quote(stock_id: str):
    """最新報價（今天有分鐘資料時即時；否則最近收盤）。供自選清單顯示。"""
    df = q.get_price(stock_id, include_today=True).tail(2)
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


@app.get("/api/quotes")
def quotes(ids: str):
    """自選清單批量報價。盤中用 shioaji snapshots（一次請求全數檔位，額度
    50 req/5s，遠省於逐檔 tick 訂閱）；非盤中或快照拿不到的檔位回庫存最近收盤。
    """
    import datetime as _dt

    from src.data import shioaji_source

    codes = [c.strip() for c in ids.split(",") if c.strip()]
    if not codes:
        return []
    now = _dt.datetime.now()
    in_session = now.weekday() < 5 and _dt.time(8, 55) <= now.time() <= _dt.time(13, 35)
    snaps: dict[str, dict] = {}
    if in_session and shioaji_source.available():
        try:
            snaps = shioaji_source.fetch_snapshots([c for c in codes if c.isdigit()])
        except Exception as e:  # noqa: BLE001 — 快照失敗退回庫存報價
            log.warning("批量快照失敗（回庫存報價）：%s", e)
    today = now.date().isoformat()
    names = q.list_stocks()
    name_map = dict(zip(names["stock_id"], names["stock_name"])) if not names.empty else {}
    # 需要庫存報價的檔位（無即時快照者）一次 SQL 取回，避免逐檔全歷史讀＋重掃 list_stocks
    fallback = [c for c in codes
                if not (snaps.get(c) and snaps[c].get("ts_date") == today and snaps[c].get("close"))]
    fb = _bulk_recent_quotes(fallback, name_map)
    out = []
    for c in codes:
        s = snaps.get(c)
        if s and s.get("ts_date") == today and s.get("close"):
            out.append({"stock_id": c, "name": name_map.get(c, ""),
                        "last": s["close"],
                        "change": round(s["change_price"], 2),
                        "change_pct": round(s["change_pct"], 2), "date": today})
        else:
            out.append(fb.get(c) or {"stock_id": c, "name": name_map.get(c, ""),
                                     "last": None, "change": None, "change_pct": None})
    return out


def _bulk_recent_quotes(codes: list[str], name_map: dict) -> dict:
    """一次 SQL 取多檔最近兩個交易日收盤，算報價（供 quotes fallback，免 N+1 全歷史）。"""
    if not codes:
        return {}
    ph = ",".join("?" * len(codes))
    with db.connect(get_settings().db_path) as conn:
        rows = db.read_sql(conn, f"""
            WITH recent AS (
                SELECT stock_id, date, close,
                       ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY date DESC) rn
                FROM price_daily
                WHERE stock_id IN ({ph}) AND close > 0
                  AND date >= date((SELECT MAX(date) FROM price_daily), '-15 days')
            )
            SELECT a.stock_id, a.date, a.close AS last, b.close AS prev
            FROM recent a LEFT JOIN recent b ON b.stock_id=a.stock_id AND b.rn=2
            WHERE a.rn=1
        """, tuple(codes))
    out = {}
    for r in rows.itertuples():
        last = float(r.last)
        prev = float(r.prev) if r.prev is not None and r.prev == r.prev else last  # NaN!=NaN
        out[r.stock_id] = {
            "stock_id": r.stock_id, "name": name_map.get(r.stock_id, ""), "last": last,
            "change": round(last - prev, 2),
            "change_pct": round((last / prev - 1) * 100, 2) if prev else 0.0,
            "date": r.date}
    return out


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


# 背景選股（供 WebUI 實時進度）：start 啟動執行緒，status 輪詢階段/逐檔進度
_scr_lock = threading.Lock()
_scr_state: dict = {"running": False, "as_of": None, "stage": "", "current": 0,
                    "total": 0, "error": None}


class ScreenerStartReq(BaseModel):
    as_of: str
    top_n: int = 30


@app.post("/api/screener/start")
def screener_start(req: ScreenerStartReq):
    with _scr_lock:
        if _scr_state["running"]:
            return {"started": False, "running": True}
        _scr_state.update(running=True, as_of=req.as_of, stage="準備中",
                          current=0, total=0, error=None)

    def _work():
        try:
            def prog(stage: str, cur: int = 0, tot: int = 0) -> None:
                _scr_state.update(stage=stage, current=cur, total=tot)
            ranked = run_screener(req.as_of, progress=prog)
            rows = _records(ranked.head(req.top_n)) if not ranked.empty else []
            q.save_screener_result(req.as_of, rows, req.top_n)
            _scr_state.update(stage="完成", current=0, total=0)
        except Exception as e:  # noqa: BLE001 — 錯誤放進 status 讓前端顯示
            log.error("背景選股失敗：%s", e)
            _scr_state["error"] = str(e)
        finally:
            _scr_state["running"] = False

    threading.Thread(target=_work, daemon=True).start()
    return {"started": True, "running": True}


@app.get("/api/screener/status")
def screener_status():
    return dict(_scr_state)


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
# 用絕對路徑（與 jobs.py 子行程的 cwd=ROOT 同源）：相對 CWD 在非專案目錄啟動
# uvicorn 時會解析到別處，unlink 刪不到舊檔、status 讀到過期結果且錯得很安靜
_BT_RESULT = jobs.JOBS_DIR / "backtest_result.json"


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


def _gather_picks(as_of: str, top_n: int, progress=None) -> tuple[list[str], dict[str, str]]:
    """收集選股報告候選：量化 top_n + 政策題材偵察額外名額。回傳 (picks, sources)。"""
    if progress:
        progress("載入量化候選…")
    ranked = run_screener(as_of)
    if ranked.empty:
        return [], {}
    picks = ranked["stock_id"].head(top_n).tolist()
    # 政策題材偵察：額外名額（不佔量化 top_n；失敗不影響量化候選）
    sources = {sid: "screener" for sid in picks}
    if progress:
        progress("政策題材偵察…")
    try:
        from src.agents import scout as news_scout
        for c in news_scout.run_news_scout(as_of):
            if c["stock_id"] not in picks:
                picks.append(c["stock_id"])
                sources[c["stock_id"]] = "news_scout"
    except Exception as e:  # noqa: BLE001
        log.error("政策題材偵察失敗：%s", e)
    return picks, sources


@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    if not llm.has_api_key():
        raise HTTPException(400, "未設定 ANTHROPIC_API_KEY")
    picks, sources = _gather_picks(req.as_of, req.top_n)
    if not picks:
        return []
    return pipeline.analyze_stocks(picks, req.as_of, sources=sources)


# 背景選股報告（供 WebUI 實時進度）：start 啟動執行緒，status 輪詢階段/逐檔進度，
# cancel 設旗標於下一檔前中止並移除本次已寫入的交易計畫
_ana_lock = threading.Lock()
_ana_cancel = threading.Event()
_ana_state: dict = {"running": False, "as_of": None, "stage": "", "current": 0,
                    "total": 0, "error": None}


def _cleanup_analyze_run(as_of: str, stock_ids: list[str]) -> None:
    """中斷選股報告：移除本次已寫入的交易計畫與對應風控駁回紀錄（不動行情/帳本/題材快照）。"""
    if not stock_ids:
        return
    ph = ",".join("?" * len(stock_ids))
    with db.connect(get_settings().db_path) as conn:
        conn.execute(f"DELETE FROM trade_plan WHERE as_of=? AND stock_id IN ({ph})",
                     (as_of, *stock_ids))
        conn.execute(f"DELETE FROM friction_log WHERE as_of=? AND stock_id IN ({ph})",
                     (as_of, *stock_ids))
    log.info("已中斷選股報告並移除 %d 檔本次計畫（%s）", len(stock_ids), as_of)


@app.post("/api/analyze/start")
def analyze_start(req: AnalyzeReq):
    if not llm.has_api_key():
        raise HTTPException(400, "未設定 ANTHROPIC_API_KEY")
    with _ana_lock:
        if _ana_state["running"]:
            return {"started": False, "running": True}
        _ana_cancel.clear()
        _ana_state.update(running=True, as_of=req.as_of, stage="準備中",
                          current=0, total=0, error=None)

    def _work():
        try:
            def prog(stage: str, cur: int = 0, tot: int = 0) -> None:
                _ana_state.update(stage=stage, current=cur, total=tot)
            picks, sources = _gather_picks(req.as_of, req.top_n, progress=prog)
            records = []
            if picks and not _ana_cancel.is_set():
                records = pipeline.analyze_stocks(
                    picks, req.as_of, sources=sources, progress=prog,
                    should_cancel=_ana_cancel.is_set)
            if _ana_cancel.is_set():
                _cleanup_analyze_run(req.as_of, [r["stock_id"] for r in records])
                _ana_state.update(stage="已中斷", current=0, total=0)
            else:
                _ana_state.update(stage="完成", current=0, total=0)
        except Exception as e:  # noqa: BLE001 — 錯誤放進 status 讓前端顯示
            log.error("背景選股報告失敗：%s", e)
            _ana_state["error"] = str(e)
        finally:
            _ana_state["running"] = False

    threading.Thread(target=_work, daemon=True).start()
    return {"started": True, "running": True}


@app.get("/api/analyze/status")
def analyze_status():
    return dict(_ana_state)


@app.post("/api/analyze/cancel")
def analyze_cancel():
    """請求中斷進行中的選股報告；實際中止與資料清理在背景工作於下一檔前完成。"""
    if not _ana_state["running"]:
        return {"cancelled": False, "running": False}
    _ana_cancel.set()
    _ana_state.update(stage="中斷中…")
    return {"cancelled": True, "running": True}


@app.get("/api/news")
def news_all(limit: int = 300, q_kw: str = "", stock_id: str = ""):
    """庫存個股新聞（新到舊，含股名）。q_kw 過濾標題、stock_id 過濾個股。"""
    sql = ("SELECT n.stock_id, COALESCE(s.stock_name,'') AS name, n.date, "
           "n.published_at, n.title, n.source, n.url "
           "FROM news n LEFT JOIN stock_info s ON s.stock_id = n.stock_id")
    conds, params = [], []
    if stock_id.strip():
        conds.append("n.stock_id = ?")
        params.append(stock_id.strip())
    if q_kw.strip():
        conds.append("n.title LIKE ?")
        params.append(f"%{q_kw.strip()}%")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY n.date DESC, n.published_at DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    with db.connect(get_settings().db_path) as conn:
        return _records(db.read_sql(conn, sql, tuple(params)))


@app.get("/api/scout/dates")
def scout_dates():
    """已有題材偵察快照的日期清單（新到舊，含統計）。"""
    with db.connect(get_settings().db_path) as conn:
        conn.execute(db.SCHEMA["scout_log"])
        rows = conn.execute(
            "SELECT as_of, source, headlines_json, candidates_json FROM scout_log "
            "ORDER BY as_of DESC LIMIT 60").fetchall()
    return [{"as_of": r[0], "source": r[1],
             "headlines": len(json.loads(r[2] or "[]")),
             "candidates": len(json.loads(r[3] or "[]"))} for r in rows]


@app.get("/api/scout")
def scout_snapshot(as_of: str):
    """某日政策題材偵察快照（掃到的新聞標題 + 總結 + 候選）；無則回 null。"""
    with db.connect(get_settings().db_path) as conn:
        conn.execute(db.SCHEMA["scout_log"])  # 防禦性建表（舊 DB 未重啟 init 也能查）
        row = conn.execute(
            "SELECT source, headlines_json, summary, candidates_json, created_at "
            "FROM scout_log WHERE as_of=?", (as_of,)).fetchone()
    if not row:
        return None
    return {"as_of": as_of, "source": row[0],
            "headlines": json.loads(row[1] or "[]"), "summary": row[2],
            "candidates": json.loads(row[3] or "[]"), "created_at": row[4]}


@app.get("/api/trade-plans")
def trade_plans(as_of: str):
    return pipeline.load_plans(as_of)


@app.get("/api/trade-plans/latest-date")
def trade_plans_latest_date():
    """最近一次有交易計畫的 as_of（供報告面板預設載入）。"""
    with db.connect(get_settings().db_path) as conn:
        row = conn.execute("SELECT MAX(as_of) FROM trade_plan").fetchone()
    return {"as_of": row[0] if row else None}


@app.get("/api/brain-log")
def brain_log(limit: int = 100):
    return _records(q.brain_log(limit=limit))


@app.get("/api/llm-usage")
def llm_usage():
    """Claude API 用量成本彙總 + 剩餘 credit 估計。

    官方 API 沒有查餘額的端點，故採本地估算：每次呼叫記錄 token 用量並依
    模型價目換算 USD；若設定 llm.credit_total_usd（儲值總額），回傳估計剩餘。
    """
    summary = q.llm_usage_summary()
    # 儲值額屬個人資訊，存 .env（CLAUDE_CREDIT_TOTAL_USD），不進 git 的 settings.yaml
    try:
        credit_total = float(os.getenv("CLAUDE_CREDIT_TOTAL_USD") or 0) or None
    except ValueError:
        credit_total = None
    summary["credit_total_usd"] = credit_total
    summary["remaining_usd"] = (credit_total - summary["total_usd"]) if credit_total else None
    return summary


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
    """清除所有 AI 產出：分析記錄、交易計畫、Guard 駁回、題材偵察快照、反思記憶。

    不動行情資料（股價/法人/營收/個股新聞原文等）、模擬交易帳本（positions/orders/fills），
    也不動量化選股快照（screener_result 是純量化排名，非 LLM 產出，可隨時重算）。
    """
    import sqlite3

    tables = ("brain_log", "trade_plan", "friction_log", "scout_log")
    deleted: dict = {}
    try:
        with db.connect(get_settings().db_path) as conn:
            for t in tables:
                deleted[t] = conn.execute(f"DELETE FROM {t}").rowcount
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            raise HTTPException(409, "資料庫正被背景任務寫入中，請等任務結束或先停止任務再清除")
        raise
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
    names = q.list_stocks()
    name_map = dict(zip(names["stock_id"], names["stock_name"])) if not names.empty else {}

    def _with_name(recs: list[dict]) -> list[dict]:
        return [{**r, "name": name_map.get(r.get("stock_id"), "")} for r in recs]

    pos = broker.positions()
    rows = []
    for r in pos.to_dict(orient="records"):
        px = q.get_price(r["stock_id"])
        last = float(px["close"].iloc[-1]) if not px.empty else r["avg_cost"]
        mv = r["shares"] * last
        cost = r["shares"] * r["avg_cost"]
        rows.append({**r, "name": name_map.get(r["stock_id"], ""),
                     "last": last, "market_value": round(mv, 0),
                     "unrealized_pnl": round(mv - cost, 0),
                     "unrealized_pct": round((last / r["avg_cost"] - 1) * 100, 2) if r["avg_cost"] else 0})
    import datetime as _dt
    from src.data import market_calendar as mcal
    today = _dt.date.today().isoformat()
    return {
        "cash": broker.cash,
        "trading_enabled": broker.trading_enabled(),
        "positions": rows,
        "pending_orders": _with_name(_records(broker.pending_orders())),
        "orders": _with_name(_records(broker.orders(100))),
        "fills": _with_name(_records(broker.fills(100))),
        "performance": performance_summary(),
        # 交易日資訊：待撮合委託的預計撮合日提示（假日/週末順延）
        "is_trading_day": mcal.is_trading_day(today, allow_fetch=False),
        "next_trading_day": mcal.next_trading_day(today, allow_fetch=False),
    }


@app.post("/api/portfolio/reset")
def portfolio_reset():
    """重置模擬帳本（清空持倉/委託/成交/權益史，現金回到設定資金）。"""
    import sqlite3
    from src.data import database as _db
    cap = float(get_settings()["capital"]["total"])
    try:
        # immediate 寫鎖：多步刪除＋回設 cash 成為單一交易，避免與背景任務交錯出現中間不一致
        with _db.connect(get_settings().db_path, immediate=True) as conn:
            for t in ("positions", "orders", "fills", "equity_history"):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("INSERT OR REPLACE INTO broker_state (key, value) VALUES ('cash', ?)", (str(cap),))
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            raise HTTPException(409, "資料庫正被背景任務寫入中，請等任務結束或先停止任務再重置")
        raise
    return {"status": "reset", "cash": cap}


@app.get("/api/calendar/status")
def calendar_status():
    """交易日曆狀態：今天是否開盤、下一交易日、假日表覆蓋年度、近期休市日。"""
    from src.data import market_calendar as mcal
    return mcal.status()


@app.post("/api/calendar/sync")
def calendar_sync():
    """手動同步 TWSE 年度假日表（nightly 回補也會自動同步）。"""
    from src.data import market_calendar as mcal
    try:
        return {"synced": mcal.sync_holidays()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"假日表同步失敗：{e}")


class TradingToggle(BaseModel):
    enabled: bool


@app.post("/api/trading/toggle")
def trading_toggle(req: TradingToggle):
    """緊急停止/恢復：停用時每日流程只做保護性出場，不開新倉。

    停用時立即撤銷所有待撮合委託——否則昨日掛的限價買單隔天開盤照樣成交建倉，
    kill-switch 的「不開新倉」語義會被打破（execute_pending 另有同樣的防呆撤單）。
    """
    from src.broker.paper import PaperBroker
    broker = PaperBroker()
    broker.set_trading_enabled(req.enabled)
    cancelled = broker.cancel_all_pending() if not req.enabled else 0
    return {"trading_enabled": req.enabled, "cancelled_orders": cancelled}


class DailyRunReq(BaseModel):
    as_of: str | None = None
    top_n: int | None = None   # None = 用 settings.yaml daily.top_n


@app.post("/api/daily/run")
def daily_run(req: DailyRunReq):
    """背景執行每日主流程（撮合→風控→快照→決策掛單）。"""
    if jobs.is_running(_DAILY_JOB):
        raise HTTPException(409, "每日流程已在執行中")
    # 比照排程器 wait_for=["backfill"]：回補進行中不啟動，避免併發寫 DB 鎖衝突、且不用未補完的 T 日資料決策
    if jobs.is_running(_BACKFILL_JOB):
        raise HTTPException(409, "資料回補進行中，請等回補結束再執行每日流程")
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
        "telegram_token": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "telegram_chat": bool(os.getenv("TELEGRAM_CHAT_ID")),
    }


class EnvUpdate(BaseModel):
    key: str
    value: str


_ALLOWED_ENV = {"FINMIND_TOKEN", "ANTHROPIC_API_KEY", "SJ_API_KEY", "SJ_SEC_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CLAUDE_CREDIT_TOTAL_USD"}


@app.post("/api/set-env")
def set_env(req: EnvUpdate):
    if req.key not in _ALLOWED_ENV:
        raise HTTPException(400, f"不允許設定 {req.key}")
    if not req.value.strip():
        raise HTTPException(400, "值不可為空")
    config_io.set_env_var(req.key, req.value.strip())
    return {"status": "saved"}


# ---------- 通知（Telegram Bot）----------
@app.post("/api/notify/test")
def notify_test():
    """發送測試訊息（不看 enabled 開關，只需 token + chat_id）。"""
    from src.notify import telegram
    try:
        telegram.send_message("✅ <b>StockTradingSystem</b>\nTelegram 通知已就緒，每日決策報告將發送到此對話。")
    except Exception as e:  # noqa: BLE001 — 把設定/API 錯誤原樣回給前端
        raise HTTPException(400, str(e))
    return {"sent": True}


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
        df = q.get_price(sid, include_today=True).tail(2)
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


# ---- WebUI 靜態檔（部署用；frontend/ 內 npm run build 產出）----
# 掛在所有 /api 路由之後，開發模式（無 dist、走 vite dev server）不受影響。
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="webui")
