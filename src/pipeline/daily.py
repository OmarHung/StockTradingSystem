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

from src.broker.paper import PaperBroker
from src.config import get_settings
from src.logging_setup import get_logger

log = get_logger("daily")


def run_daily(as_of: str | None = None, top_n: int = 3, decide: bool = True,
              reflect_weekly: bool = True) -> dict:
    summary = _run_daily(as_of=as_of, top_n=top_n, decide=decide,
                         reflect_weekly=reflect_weekly)
    # 流程收尾：Telegram 每日報告（未設定則跳過；失敗只記 log，不影響交易）
    from src.notify import telegram
    telegram.send_daily_report(summary)
    return summary


def _run_daily(as_of: str | None = None, top_n: int = 3, decide: bool = True,
               reflect_weekly: bool = True) -> dict:
    as_of = as_of or dt.date.today().isoformat()
    broker = PaperBroker()
    summary: dict = {"as_of": as_of}

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

    # ⑤ 週五：成果評估 + 反思（在決策前，讓新規則立即生效）
    if reflect_weekly and dt.date.fromisoformat(as_of).weekday() == 4:
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

    from src.agents import pipeline as agent_pipeline
    from src.screener.screener import run_screener

    ranked = run_screener(as_of)
    if ranked.empty:
        summary["orders"] = []
        return summary
    held = set(broker.positions()["stock_id"]) if not broker.positions().empty else set()
    picks = [s for s in ranked["stock_id"].head(top_n * 2).tolist() if s not in held][:top_n]
    log.info("盤後決策候選：%s", picks)

    # 政策題材偵察：新聞先行的候選股（額外名額，不佔量化 top_n；失敗不影響主流程）
    scout_map: dict[str, dict] = {}
    try:
        from src.agents import scout as news_scout
        for c in news_scout.run_news_scout(as_of):
            if c["stock_id"] not in held and c["stock_id"] not in picks:
                picks.append(c["stock_id"])
                scout_map[c["stock_id"]] = c
        if scout_map:
            summary["scout"] = list(scout_map.values())
            log.info("政策題材候選：%s", [f"{s} {c['theme']}" for s, c in scout_map.items()])
    except Exception as e:  # noqa: BLE001
        log.error("政策題材偵察失敗（不影響量化候選）：%s", e)

    orders = []
    decisions = []  # 每檔決策明細（含不掛單原因，供每日報告）
    summary["decisions"] = decisions
    for sid in picks:
        rec = agent_pipeline.analyze_stock(
            sid, as_of, source="news_scout" if sid in scout_map else "screener")
        if not rec:
            decisions.append({"stock_id": sid, "action": "error",
                              "ordered": False, "note": "分析失敗"})
            continue
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
                continue
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
    summary["orders"] = orders
    return summary