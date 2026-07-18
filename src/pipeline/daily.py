"""每日主流程（Phase 5 全自動循環的心臟）。

run_daily(as_of) 依台股日節奏執行：
  ① 開盤：撮合昨日掛的限價買單（開盤價 ≤ 限價成交，否則失效）
  ② 收盤：停損/停利檢查（觸價出場、已實現損益、停損記冷卻）
  ③ 收盤：權益快照（equity_history，vs TAIEX）
  ④ 盤後：選股 → LLM 決策 → Guard（真實持倉狀態）→ 掛明日限價單
  ⑤ 週五加跑：成果評估 + 反思（規則庫更新）

緊急停止（trading_enabled=false）：只跑 ①②③（保護性出場照做，不開新倉）。
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from src.broker.paper import PaperBroker
from src.config import get_settings
from src.logging_setup import get_logger

log = get_logger("daily")

_TAIPEI = ZoneInfo("Asia/Taipei")
_MARKET_CLOSE = "13:30"          # 台股收盤時間（撮合/停損以收盤定案的最早安全時點）


def _now_taipei() -> dt.datetime:
    """當前台北時間（VPS 可能落在其他時區，收盤判斷一律以台北為準）。"""
    return dt.datetime.now(_TAIPEI)


def _closing_settled(as_of: str) -> bool:
    """as_of 當日行情是否已定案，可安全做撮合/停損/快照結算。

    as_of 早於今天（台北）＝歷史交易日重放，行情早已定案 → True；
    as_of 就是今天則須已過收盤（含台北時區）才 True，否則盤中手動觸發
    會拿仍在變動的 open/low/high 永久扣現金、刪持倉、寫 fills。
    as_of 若為未來日（不應發生）保守視為未定案。
    """
    now = _now_taipei()
    today = now.date().isoformat()
    if as_of < today:
        return True
    if as_of > today:
        return False
    return now.strftime("%H:%M") >= _MARKET_CLOSE


def run_daily(as_of: str | None = None, top_n: int = 3, decide: bool = True,
              reflect_weekly: bool = True) -> dict:
    from src.notify import telegram
    try:
        summary = _run_daily(as_of=as_of, top_n=top_n, decide=decide,
                             reflect_weekly=reflect_weekly)
    except Exception as e:
        # 崩潰也要讓人知道：進 log + TG 告警，否則只能靠「沒收到報告」發現
        log.exception("每日流程失敗")
        telegram.send_error_alert("每日流程失敗", f"{type(e).__name__}: {e}")
        raise
    if summary.get("skipped"):
        return summary   # 休市日跳過：不發每日報告
    # 流程收尾：Telegram 每日報告（未設定則跳過；失敗只記 log，不影響交易）
    telegram.send_daily_report(summary)
    return summary


def _run_daily(as_of: str | None = None, top_n: int = 3, decide: bool = True,
               reflect_weekly: bool = True) -> dict:
    as_of = as_of or dt.date.today().isoformat()

    # 交易日閘門：休市日（週末/國定假日）整個流程免跑——撮合/停損/快照/決策
    # 都以「當日行情存在」為前提，休市日跑了輕則空轉、重則誤殺 pending 委託
    from src.data import market_calendar as mcal
    if not mcal.is_trading_day(as_of):
        log.info("%s 非交易日（週末/假日），每日流程跳過；下一交易日 %s",
                 as_of, mcal.next_trading_day(as_of))
        return {"as_of": as_of, "skipped": "non_trading_day",
                "next_trading_day": mcal.next_trading_day(as_of)}

    broker = PaperBroker()
    summary: dict = {"as_of": as_of}

    # 資料新鮮度閘門：決策唯一硬相依的 T 日資料是股價日K。
    # 未達 as_of 就等待進行中的回補或立即觸發價格更新；仍不新鮮 →
    # 保護性步驟（撮合/停損/快照）照跑，但跳過決策（寧可不交易，不用舊價格交易）。
    data_fresh, fresh_note = _ensure_price_fresh(as_of)
    summary["data_fresh"] = data_fresh
    if not data_fresh:
        summary["data_note"] = fresh_note
        log.warning("價格資料未更新到 %s：%s", as_of, fresh_note)

    # 收盤結算閘門：撮合/停損/快照全靠 as_of 當日的 open/low/high 定案帳務，
    # 這些值在收盤前仍在變動，盤中撮合＝以尚未定案的行情永久扣現金/刪持倉。
    # 唯有 as_of 就是「今天（台北）」且尚未過收盤才擋——歷史日重放（as_of 為
    # 過去交易日，行情早已定案）照常結算。
    settled = _closing_settled(as_of)
    summary["closing_settled"] = settled
    if settled:
        # ① 開盤撮合昨日委託
        fills = broker.execute_pending(as_of)
        summary["morning_fills"] = fills
        for f in fills:
            log.info("開盤撮合 %s: %s", f.get("stock_id"), f.get("status"))

        # ② 停損/停利
        exits = broker.check_stops(as_of)
        summary["exits"] = exits
        for e in exits:
            log.info("風控出場 %s [%s] 價 %s 損益 %s", e["stock_id"], e["reason"], e["price"], e["pnl"])

        # ③ 權益快照
        snap = broker.mark_to_market(as_of)
        summary["equity"] = snap
        log.info("權益快照 %s：現金 %s + 持倉 %s = %s",
                 as_of, f"{snap['cash']:,.0f}", f"{snap['positions_value']:,.0f}", f"{snap['equity']:,.0f}")
    else:
        # 盤中手動觸發：只跑決策掛單（下方 ④），跳過所有以當日行情定案帳務的步驟
        summary["morning_fills"] = []
        summary["exits"] = []
        log.warning("⛔ %s 尚未收盤（台北時間 %s < %s），跳過撮合/停損/快照，"
                    "僅執行盤後決策；結算步驟待收盤後再跑", as_of,
                    _now_taipei().strftime("%H:%M"), _MARKET_CLOSE)
        from src.notify import telegram
        telegram.send_error_alert(
            "收盤結算延後：尚未過收盤時間",
            f"as_of={as_of}：當前台北時間 {_now_taipei().strftime('%H:%M')} 未達收盤 "
            f"{_MARKET_CLOSE}，撮合/停損/快照跳過（避免以盤中行情定案帳務）。")

    # ⑤ 每週最後交易日：成果評估 + 反思（在決策前，讓新規則立即生效）。
    # 用「下一交易日落在不同 ISO 週」判定，而非死綁週五——週五逢國定假日時，
    # 綁週五會讓當週反思與成果評估整週消失。
    _next_td = mcal.next_trading_day(as_of)
    _is_week_end = (dt.date.fromisoformat(as_of).isocalendar()[:2]
                    != dt.date.fromisoformat(_next_td).isocalendar()[:2])
    if reflect_weekly and _is_week_end:
        try:
            from src.memory import outcome, reflect
            ev = outcome.evaluate_pending(today=as_of)
            outcome.sync_friction_to_blocked()
            rf = reflect.run_reflection()
            summary["reflection"] = {"evaluation": ev,
                                     "rules_added": rf["rules_added"] if rf else 0}
            log.info("週反思：評估 %d 筆、新增規則 %d 條",
                     ev["evaluated"], rf["rules_added"] if rf else 0)
        except Exception as e:  # noqa: BLE001 — 反思失敗不影響交易流程
            log.error("週反思失敗：%s", e)

    # ④ 盤後決策 → 掛明日委託
    enabled = broker.trading_enabled()
    summary["trading_enabled"] = enabled
    if not decide:
        summary["orders"] = []
        return summary
    if not enabled:
        log.warning("⛔ 交易已緊急停止（trading_enabled=false），跳過決策與掛單")
        summary["orders"] = []
        return summary
    if not data_fresh:
        from src.notify import telegram
        log.warning("⛔ 價格資料未達 %s（%s），跳過決策與掛單", as_of, fresh_note)
        # 保護性步驟是否實際執行，取決於收盤結算閘門：收盤前盤中觸發時同樣被跳過
        _protective = ("保護性步驟（撮合/停損/快照）已照常執行。" if settled
                       else "保護性步驟（撮合/停損/快照）因尚未收盤同步跳過，待收盤後再跑。")
        telegram.send_error_alert(
            "盤後決策跳過：價格資料未更新",
            f"as_of={as_of}：{fresh_note}\n{_protective}")
        summary["orders"] = []
        return summary

    from src.screener.screener import run_screener

    ranked = run_screener(as_of)
    if ranked.empty:
        summary["orders"] = []
        return summary
    held = set(broker.positions()["stock_id"]) if not broker.positions().empty else set()
    picks = [s for s in ranked["stock_id"].head(top_n * 2).tolist() if s not in held][:top_n]
    log.info("盤後決策候選：%s", picks)

    # 關鍵新聞偵察：新聞先行的候選股（額外名額，不佔量化 top_n；失敗不影響主流程）
    scout_map: dict[str, dict] = {}
    try:
        from src.agents import scout as news_scout
        for c in news_scout.run_news_scout(as_of):
            if c["stock_id"] not in held and c["stock_id"] not in picks:
                picks.append(c["stock_id"])
                scout_map[c["stock_id"]] = c
        if scout_map:
            summary["scout"] = list(scout_map.values())
            log.info("關鍵新聞候選：%s", [f"{s} {c['theme']}" for s, c in scout_map.items()])
    except Exception as e:  # noqa: BLE001
        log.error("關鍵新聞偵察失敗（不影響量化候選）：%s", e)

    orders = []
    decisions = []  # 每檔決策明細（含不掛單原因，供每日報告）
    summary["decisions"] = decisions
    for sid in picks:
        # 單檔失敗（如 DB 鎖、LLM 逾時）只跳過該檔，不讓整個每日流程陪葬
        try:
            _decide_one(broker, sid, as_of, scout_map, orders, decisions)
        except Exception as e:  # noqa: BLE001
            log.exception("決策 %s 失敗（跳過該檔）", sid)
            decisions.append({"stock_id": sid, "action": "error",
                              "ordered": False, "note": f"{type(e).__name__}: {e}"})
    summary["orders"] = orders
    return summary


def _ensure_price_fresh(as_of: str, wait_minutes: int = 30) -> tuple[bool, str]:
    """盤後決策的資料新鮮度閘門：price_daily 必須有 as_of 當日資料（以 TAIEX 為準）。

    不新鮮時依序：① 回補 job 進行中 → 等它結束複查（避免 SQLite 鎖衝突與重複拉取）；
    ② 立即執行價格增量更新（shioaji 單路徑，不吃 FinMind 額度）後複查。
    回傳 (是否新鮮, 說明)。歷史日期重放（as_of < 最新資料日）直接視為新鮮。
    """
    import time

    from src import jobs
    from src.config import get_settings
    from src.data import database as db

    def _latest() -> str:
        with db.connect(get_settings().db_path) as conn:
            r = conn.execute(
                "SELECT MAX(date) FROM price_daily WHERE stock_id='TAIEX'").fetchone()
        return (r[0] if r else None) or ""

    if _latest() >= as_of:
        return True, "已是最新"

    waited = 0
    while jobs.is_running("backfill") and waited < wait_minutes * 60:
        if waited == 0:
            log.info("價格資料未達 %s，回補進行中——等待完成後複查", as_of)
        time.sleep(20)
        waited += 20
    if _latest() >= as_of:
        return True, "等待回補完成後已最新"

    log.warning("價格資料未達 %s，立即執行價格增量更新（shioaji）", as_of)
    try:
        from scripts.backfill import backfill
        backfill(stocks=None, start=None, end=None, limit=None,
                 datasets=["price_daily"])
    except Exception as e:  # noqa: BLE001 — 更新失敗交由呼叫端決定跳過決策
        return False, f"價格即時更新失敗：{type(e).__name__}: {e}"
    if _latest() >= as_of:
        return True, "已即時更新"
    return False, "更新後仍無當日價格（可能為休市日，或行情源尚未發布）"


def _decide_one(broker: PaperBroker, sid: str, as_of: str, scout_map: dict,
                orders: list, decisions: list) -> None:
    """分析單檔 → Guard → 掛單，結果附加到 orders/decisions。"""
    from src.agents import pipeline as agent_pipeline

    rec = agent_pipeline.analyze_stock(
        sid, as_of, source="news_scout" if sid in scout_map else "screener")
    if not rec:
        decisions.append({"stock_id": sid, "action": "error",
                          "ordered": False, "note": "分析失敗"})
        return
    plan, guard = rec["plan"], rec.get("guard")
    if plan["action"] == "buy" and guard and guard["approved"] and guard["shares"] > 0:
        oid = broker.place_buy(
            as_of=as_of, stock_id=sid, shares=guard["shares"],
            limit_price=float(plan["entry_high"]),
            stop_loss=plan.get("stop_loss"), target=plan.get("target_price"),
            industry=(guard.get("industry") or ""),
        )
        if oid is None:
            log.warning("⛔ 交易已緊急停止（流程中途按下），%s 掛單被拒", sid)
            decisions.append({"stock_id": sid, "action": plan["action"],
                              "confidence": plan.get("confidence"),
                              "ordered": False, "note": "緊急停止，掛單被拒"})
            return
        orders.append({"order_id": oid, "stock_id": sid, "shares": guard["shares"],
                       "limit": plan["entry_high"], "stop": plan.get("stop_loss"),
                       "target": plan.get("target_price")})
        decisions.append({"stock_id": sid, "action": plan["action"],
                          "confidence": plan.get("confidence"),
                          "ordered": True, "note": None})
        log.info("掛明日限價單 #%d %s %d 股 @≤%s（損 %s / 標 %s）",
                 oid, sid, guard["shares"], plan["entry_high"],
                 plan.get("stop_loss"), plan.get("target_price"))
    else:
        reason = plan["action"] if plan["action"] != "buy" else \
            (guard or {}).get("reject_reason", "guard 未核准")
        decisions.append({"stock_id": sid, "action": plan["action"],
                          "confidence": plan.get("confidence"), "ordered": False,
                          "note": None if plan["action"] != "buy" else reason})
        log.info("不掛單 %s：%s", sid, reason)