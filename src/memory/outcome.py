"""成果評估器：把過去的交易計畫用「後續真實價格」評分，寫入經驗記憶庫。

這讓「檢討→學習」閉環在 Phase 5 實倉前就能運轉：
  trade_plan（as_of 的決策）＋ 之後的日K → 觸標/觸損/持有到期的實際結果。

評估規則（模擬保守成交）：
- 進場：as_of 次一交易日開盤價 ≤ entry_high 才視為成交（限價邏輯），否則 no_fill
- 之後逐日：low ≤ 停損 → stopped（以停損價出場）；high ≥ 目標 → target_hit（以目標價出場）
  同日兩者皆觸及時保守以 stopped 計
- horizon 天內未觸發 → timeout，以第 horizon 天收盤計報酬
- hold/avoid 計畫：記錄「假如區間報酬」（驗證避開是否正確），不入場
"""
from __future__ import annotations

import datetime as dt
import json

from src.config import get_settings
from src.data import database as db
from src.data import query as q
from src.logging_setup import get_logger
from src.memory import store

log = get_logger(__name__)

HORIZON_DAYS = 20   # 評估視窗（交易日）


def _situation_text(rec: dict) -> str:
    """把決策當下的情境壓成一段可檢索文字（語意檢索鍵）。"""
    p = rec.get("plan", {})
    parts = [f"{rec.get('stock_id')}"]
    for name, label in (("technical", "技術"), ("chips", "籌碼"), ("fundamental", "基本")):
        e = (rec.get("analysts") or {}).get(name)
        if e:
            r = e["report"]
            parts.append(f"{label}:{r.get('signal')}({r.get('score'):+.2f})")
            pts = r.get("key_points") or []
            if pts:
                parts.append(pts[0])
    parts.append(f"交易員:{p.get('action')} 信心{p.get('confidence', 0):.0%}")
    if p.get("rationale"):
        parts.append(str(p["rationale"])[:120])
    return " | ".join(str(x) for x in parts)


def _ex_gap_offset(sid: str, start: str, end: str) -> dict[str, float]:
    """回傳 {date: 累計除權息跳空幅度}——某交易日(含)之前發生過的除息名目跳空總和。

    名目價評估下，除息當日開盤會較前收跳空下跌約當息值（gap=before_price-after_price），
    使窗口內的 low 系統性偏低而誤觸名目停損。以此累計值把停損/目標同步下修，
    抵銷跳空、只保留真實價格變動。gap<=0（除權配股使還原後不降或資料異常）者略過。
    dividend 為同日事件權威來源，capital_change 只補非除權息的公司行動（與 query 一致）。
    """
    with db.connect(get_settings().db_path) as conn:
        div = db.read_sql(
            conn,
            "SELECT date, before_price, after_price FROM dividend "
            "WHERE stock_id=? AND date>? AND date<=? "
            "AND before_price>0 AND after_price>0 "
            "UNION ALL "
            "SELECT c.date, c.before_price, c.after_price FROM capital_change c "
            "WHERE c.stock_id=? AND c.date>? AND c.date<=? "
            "AND c.before_price>0 AND c.after_price>0 "
            "AND NOT EXISTS (SELECT 1 FROM dividend d "
            "                WHERE d.stock_id=c.stock_id AND d.date=c.date) "
            "ORDER BY date",
            (sid, start, end, sid, start, end),
        )
    offsets: dict[str, float] = {}
    cum = 0.0
    for _, ev in div.iterrows():
        gap = float(ev["before_price"]) - float(ev["after_price"])
        if gap <= 0:
            continue
        cum += gap
        offsets[str(ev["date"])] = cum
    return offsets


def _cum_offset_at(offsets: dict[str, float], date: str) -> float:
    """該交易日(含)之前已發生的累計除息跳空幅度。offsets 已按日累計。"""
    total = 0.0
    for d, cum in offsets.items():
        if d <= date:
            total = cum
    return total


def _evaluate_one(plan_row: dict, today: str) -> dict | None:
    """回傳 {outcome, ret} 或 None（資料不足/未到期）。"""
    p = json.loads(plan_row["plan_json"])["plan"]
    sid, as_of = plan_row["stock_id"], plan_row["as_of"]
    # 用原始價（非還原價）：計畫的 entry_high/stop_loss/target 是決策當日的名目價，
    # 還原價會被 as_of 之後的除權息壓低整段窗口 → 觸價/成交判定與計畫不同框
    # （fill_open 更易 ≤ entry_cap 誤判成交、low 更易 ≤ stop 誤判停損）。
    px = q.get_price(sid, start=as_of, end=today, adjusted=False)
    after = px[px["date"] > as_of].reset_index(drop=True)
    if after.empty:
        return None

    action = p.get("action")
    if action != "buy" or not p.get("stop_loss") or not p.get("entry_high"):
        # 非進場計畫：記錄假如報酬。與 buy 計畫一致等滿 HORIZON——原本 min(,3)
        # 恒等於 3 日就定案，把 3 日 watched 報酬與 20 日 buy 報酬混在一起餵反思。
        window = after.head(HORIZON_DAYS)
        if len(window) < HORIZON_DAYS:
            return None
        ret = float(window["close"].iloc[-1] / window["open"].iloc[0] - 1)
        return {"outcome": f"{action}_watched", "ret": round(ret, 4)}

    entry_cap = float(p["entry_high"])
    stop, target = float(p["stop_loss"]), p.get("target_price")
    fill_open = float(after["open"].iloc[0])
    if fill_open > entry_cap:
        return {"outcome": "no_fill", "ret": 0.0}
    entry = fill_open

    window = after.head(HORIZON_DAYS)
    # 窗口內若跨除權息日，名目價會在除息當日(含)之後整段下修跳空幅度——
    # 觸價門檻同步下修累計息值以抵銷跳空；報酬則把已入袋的配息(off)加回，
    # 讓填息前出場的名目賣價 + 配息＝真實總報酬（避免把配息記成價格虧損）。
    ex_off = _ex_gap_offset(sid, as_of, str(window["date"].iloc[-1]))
    for r in window.itertuples():
        off = _cum_offset_at(ex_off, str(r.date)) if ex_off else 0.0
        if float(r.low) <= stop - off:                 # 保守：同日雙觸以停損計
            return {"outcome": "stopped", "ret": round(stop / entry - 1, 4)}
        if target and float(r.high) >= float(target) - off:
            return {"outcome": "target_hit", "ret": round(float(target) / entry - 1, 4)}
    if len(window) < HORIZON_DAYS:
        return None                                    # 視窗未滿，之後再評
    last = window.iloc[-1]
    ret_off = _cum_offset_at(ex_off, str(last["date"])) if ex_off else 0.0
    return {"outcome": "timeout", "ret": round((float(last["close"]) + ret_off) / entry - 1, 4)}


def evaluate_pending(today: str | None = None) -> dict:
    """評估所有未評估且已到期的計畫，寫回 trade_plan 並存入經驗庫。"""
    today = today or dt.date.today().isoformat()
    done, skipped = 0, 0
    with db.connect(get_settings().db_path) as conn:
        rows = db.read_sql(
            conn, "SELECT as_of, stock_id, plan_json FROM trade_plan "
                  "WHERE evaluated_at IS NULL ORDER BY as_of")
        for row in rows.to_dict(orient="records"):
            try:
                res = _evaluate_one(row, today)
            except Exception as e:  # noqa: BLE001
                log.error("評估 %s@%s 失敗：%s", row["stock_id"], row["as_of"], e)
                continue
            if res is None:
                skipped += 1
                continue
            now = dt.datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE trade_plan SET outcome=?, outcome_return=?, evaluated_at=? "
                "WHERE as_of=? AND stock_id=?",
                (res["outcome"], res["ret"], now, row["as_of"], row["stock_id"]),
            )
            # 先逐筆 commit SQL，再寫 Chroma：與向量庫解耦，任一筆 add_experience
            # 拋例外不會回滾整批已評估的 UPDATE，也不會讓兩庫狀態分歧或癱瘓整批。
            conn.commit()
            rec = json.loads(row["plan_json"])
            try:
                store.add_experience(
                    exp_id=f"{row['as_of']}_{row['stock_id']}",
                    situation=_situation_text(rec),
                    metadata={
                        "stock_id": row["stock_id"], "as_of": row["as_of"],
                        "action": rec["plan"].get("action", ""),
                        "outcome": res["outcome"], "ret": res["ret"],
                        "evaluated_at": now,
                    },
                )
            except Exception as e:  # noqa: BLE001 — 向量庫故障不影響 SQL 已寫回
                log.error("寫入經驗 %s@%s 失敗：%s", row["stock_id"], row["as_of"], e)
            done += 1
    log.info("成果評估：完成 %d 筆、未到期 %d 筆", done, skipped)
    return {"evaluated": done, "pending": skipped}


def sync_friction_to_blocked() -> int:
    """把 friction_log 鏡像進 blocked collection（供反思檢討風控鬆緊）。"""
    with db.connect(get_settings().db_path) as conn:
        rows = db.read_sql(
            conn, "SELECT id, ts, as_of, stock_id, gate, reason FROM friction_log")
    n = 0
    for r in rows.to_dict(orient="records"):
        store.add_blocked(
            block_id=f"friction_{r['id']}",
            text=f"{r['stock_id']} 被 [{r['gate']}] 駁回：{r['reason']}",
            metadata={"stock_id": r["stock_id"], "gate": r["gate"],
                      "as_of": r["as_of"] or "", "ts": r["ts"]},
        )
        n += 1
    return n