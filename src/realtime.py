"""盤中即時 K 線服務：shioaji tick 訂閱 → 合成 1 分 K / 今日日 K → WebSocket 推播。

設計（住在 API 行程的 asyncio 迴圈裡，與排程器同模式）：
- 前端開 K 線圖 → WS /api/ws/kbars/{stock_id} → 首位訂閱者觸發 shioaji subscribe
- tick callback（shioaji 執行緒）→ call_soon_threadsafe 丟進 asyncio queue
- 聚合器（asyncio task）：更新當前 1 分 K 與今日日 K → 廣播給該檔所有 WS 客戶端
- 分鐘切換：完成的 1 分 K 寫入 kbar_1min、今日日 K upsert price_daily——
  盤中日 K 圖即時跳動，收盤後 daily_quotes 官方數字覆蓋
- 最後一位客戶端離開 → unsubscribe（行情連線額度 5 條/人，省著用）

時間慣例與歷史 kbars 一致：1 分 K 的 ts＝該分鐘「結束」時間（首根 09:01），
13:30 收盤競價歸入 13:30 那根。試撮（simtrade）與盤中零股（intraday_odd）不計入。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import threading

from src.config import get_settings
from src.data import database as db
from src.logging_setup import get_logger

log = get_logger("realtime")

_CLOSE_AUCTION = dt.time(13, 30)


def taipei_epoch(t: dt.datetime) -> int:
    """台北牆鐘時間 → 「當作 UTC」的 epoch 秒。

    lightweight-charts 以 UTC 顯示 timestamp；把台北時間直接標成 UTC，
    前端不做時區換算就能顯示 09:00~13:30。歷史端點同用此慣例。
    """
    return int(t.replace(tzinfo=dt.timezone.utc).timestamp())


class _Sub:
    """單一股票的訂閱狀態：WS 客戶端佇列 + 進行中的 1 分 K / 今日日 K。"""

    def __init__(self) -> None:
        self.clients: set[asyncio.Queue] = set()
        self.bar: dict | None = None       # 進行中的 1 分 K（end/o/h/l/c/v/partial）
        self.day: dict | None = None       # 今日日 K（date/o/h/l/c/v）
        self.seen_first = False            # 訂閱後首根可能漏接前段 tick → 不入庫


class RealtimeKbarService:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._subs: dict[str, _Sub] = {}
        self._stream_ready = False
        self._lock = threading.Lock()

    # ---- 生命週期 ----
    def attach_loop(self) -> None:
        """FastAPI startup 呼叫：綁定事件迴圈並啟動聚合器（不登入券商）。"""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._loop.create_task(self._aggregate_loop())

    def _ensure_stream(self) -> None:
        """首次訂閱時才登入 shioaji 並註冊 tick callback（惰性，避免拖慢啟動）。"""
        with self._lock:
            if self._stream_ready:
                return
            from src.data import shioaji_source
            api = shioaji_source.get_api()
            api.set_on_tick_stk_v1_callback(self._on_tick)
            self._stream_ready = True
            log.info("即時行情串流就緒（tick callback 已註冊）")

    # ---- 客戶端註冊（WS 端點呼叫）----
    async def register(self, stock_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        sub = self._subs.setdefault(stock_id, _Sub())
        first = not sub.clients
        sub.clients.add(q)
        if first and stock_id.isdigit():   # 指數無個股 tick 頻道，僅股票/ETF 訂閱
            try:
                await asyncio.to_thread(self._subscribe, stock_id)
            except Exception as e:  # noqa: BLE001 — 訂閱失敗不擋 WS（前端仍可看歷史）
                log.error("即時訂閱 %s 失敗：%s", stock_id, e)
        return q

    async def unregister(self, stock_id: str, q: asyncio.Queue) -> None:
        sub = self._subs.get(stock_id)
        if not sub:
            return
        sub.clients.discard(q)
        if not sub.clients:
            self._subs.pop(stock_id, None)
            if stock_id.isdigit():
                try:
                    await asyncio.to_thread(self._unsubscribe, stock_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("取消訂閱 %s 失敗：%s", stock_id, e)

    def _subscribe(self, stock_id: str) -> None:
        import shioaji as sj
        from src.data import shioaji_source
        self._ensure_stream()
        api = shioaji_source.get_api()
        contract = shioaji_source.get_contract(api, stock_id)
        if contract is None:
            raise ValueError(f"查無合約 {stock_id}")
        api.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
        log.info("即時訂閱 %s（tick）", stock_id)

    def _unsubscribe(self, stock_id: str) -> None:
        import shioaji as sj
        from src.data import shioaji_source
        api = shioaji_source.get_api()
        contract = shioaji_source.get_contract(api, stock_id)
        if contract is not None:
            api.unsubscribe(contract, quote_type=sj.constant.QuoteType.Tick)
            log.info("取消即時訂閱 %s", stock_id)

    # ---- shioaji 執行緒 → asyncio ----
    def _on_tick(self, tick) -> None:
        if self._loop is None or self._queue is None:
            return
        if getattr(tick, "simtrade", 0) or getattr(tick, "intraday_odd", False):
            return  # 試撮/盤中零股不入 K 線
        data = {
            "code": tick.code,
            "t": tick.datetime,
            "price": float(tick.close),
            "vol": int(tick.volume),                # 張
            "o": float(tick.open), "h": float(tick.high), "l": float(tick.low),
            "tv": int(tick.total_volume),           # 今日累計（張）
            "amt": float(tick.total_amount),
        }
        self._loop.call_soon_threadsafe(self._enqueue, data)

    def _enqueue(self, data: dict) -> None:
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:  # 消化不及時丟最舊的（K 線由累計欄位自我修正）
            self._queue.get_nowait()
            self._queue.put_nowait(data)

    # ---- 聚合器 ----
    async def _aggregate_loop(self) -> None:
        while True:
            d = await self._queue.get()
            try:
                self._apply_tick(d)
            except Exception as e:  # noqa: BLE001 — 單筆 tick 壞資料不掛掉服務
                log.error("tick 聚合失敗：%s（%s）", e, d.get("code"))

    def _apply_tick(self, d: dict) -> None:
        sub = self._subs.get(d["code"])
        if sub is None or not sub.clients:
            return
        t: dt.datetime = d["t"]
        # 分鐘桶（結束時間標記）；13:30 收盤競價歸入 13:30 那根
        if t.time() >= _CLOSE_AUCTION:
            bucket_end = t.replace(hour=13, minute=30, second=0, microsecond=0)
        else:
            bucket_end = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)

        if sub.bar is None or bucket_end > sub.bar["end"]:
            done = sub.bar
            sub.bar = {"end": bucket_end, "o": d["price"], "h": d["price"],
                       "l": d["price"], "c": d["price"], "v": d["vol"],
                       "partial": not sub.seen_first}
            sub.seen_first = True
            if done and not done["partial"]:
                day_snapshot = dict(sub.day) if sub.day else None
                asyncio.get_running_loop().run_in_executor(
                    None, self._persist, d["code"], dict(done), day_snapshot)
        else:
            b = sub.bar
            b["h"] = max(b["h"], d["price"])
            b["l"] = min(b["l"], d["price"])
            b["c"] = d["price"]
            b["v"] += d["vol"]

        # 今日日 K：tick 自帶今日開高低與累計量，直接覆蓋（自我修正、無漂移）
        sub.day = {"date": t.date().isoformat(), "o": d["o"], "h": d["h"],
                   "l": d["l"], "c": d["price"], "v": d["tv"] * 1000,  # 股
                   "amt": d["amt"]}

        b = sub.bar
        bar_msg = {"type": "bar1m", "code": d["code"], "t": taipei_epoch(b["end"]),
                   "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
        day_msg = {"type": "day", "code": d["code"], **sub.day}
        for q in list(sub.clients):
            try:
                q.put_nowait(bar_msg)
                q.put_nowait(day_msg)
            except asyncio.QueueFull:  # 慢客戶端：丟訊息不丟連線（下一筆會補上最新狀態）
                pass

    def _persist(self, code: str, bar: dict, day: dict | None) -> None:
        """分鐘完成：1 分 K 入庫；今日日 K 同步 upsert（盤中讓日線圖即時）。"""
        try:
            with db.connect(get_settings().db_path) as conn:
                ts = bar["end"].strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT OR REPLACE INTO kbar_1min "
                    "(stock_id, ts, open, high, low, close, volume) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (code, ts, bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]))
                if day:
                    conn.execute(
                        "INSERT OR REPLACE INTO price_daily "
                        "(stock_id, date, open, high, low, close, volume, trading_money) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (code, day["date"], day["o"], day["h"], day["l"], day["c"],
                         day["v"], day["amt"]))
        except Exception as e:  # noqa: BLE001
            log.error("即時 K 線入庫失敗 %s：%s", code, e)


service = RealtimeKbarService()
