"""PaperBroker：模擬交易帳本（真實價格撮合、台股成本模型、DB 持久化）。

設計（與 LLM_trader 相同哲學：paper trading against real data）：
- 帳本自建（positions/orders/fills/equity_history 表），行情用資料庫真實日K
- 限價買單「隔日有效」：決策日掛單 → 次交易日開盤價 ≤ 限價才成交，否則失效
- 停損/停利：每日收盤檢查 low/high 觸價（同日雙觸保守以停損計），以觸價價位成交
- 賣出計已實現損益（扣手續費+證交稅）；停損自動記冷卻（餵 Guard）

Phase 6 換 LiveBroker（shioaji 實單）時，daily pipeline 介面不變。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from src.config import get_settings
from src.data import database as db
from src.data import market_calendar as mcal
from src.data import query as q
from src.env.costs import CostModel
from src.logging_setup import get_logger
from src.risk.guard import PortfolioState, Position, RiskConfig

log = get_logger(__name__)

COST = CostModel()


def _keep_higher(old, new) -> float | None:
    """加碼合併停損/停利：取較高者，任一為 None 時保留另一個（None-safe max）。"""
    vals = [float(v) for v in (old, new) if v is not None]
    return max(vals) if vals else None


def _ex_gap_events(conn, sid: str, after: str, upto: str) -> list[tuple[str, float]]:
    """回傳 [(除息日, gap)]：after(不含)~upto(含) 間各除權息名目跳空（gap=before-after）。

    名目價撮合下，除息當日 raw open/low 較前收跳空下跌約當息值，會誤觸停損/成交；
    比照真實券商在除息日對條件單同步下修息值。gap<=0（除權配股不降或資料異常）略過。
    dividend 為同日事件權威來源，capital_change 只補非除權息公司行動（與 query/outcome 一致）。
    """
    rows = db.read_sql(
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
        (sid, after, upto, sid, after, upto),
    )
    out: list[tuple[str, float]] = []
    for _, ev in rows.iterrows():
        gap = float(ev["before_price"]) - float(ev["after_price"])
        if gap > 0:
            out.append((str(ev["date"]), gap))
    return out


class PaperBroker:
    def __init__(self):
        self.db_path = get_settings().db_path
        self._ensure_state()

    # ---------- 狀態 ----------
    def _ensure_state(self) -> None:
        cap = float(get_settings()["capital"]["total"])
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT value FROM broker_state WHERE key='cash'").fetchone()
            if row is None:
                conn.execute("INSERT INTO broker_state (key, value) VALUES ('cash', ?)", (str(cap),))
                conn.execute("INSERT OR REPLACE INTO broker_state (key, value) VALUES ('start_capital', ?)", (str(cap),))
            # 除權息條件單調整標記：記已對該持倉套用過息值調整的最後除息日。
            # 自查補欄（database._migrate 不歸本組管，這裡冪等補上舊帳本缺的欄）。
            cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
            if cols and "ex_adj_date" not in cols:
                conn.execute("ALTER TABLE positions ADD COLUMN ex_adj_date TEXT")

    def _get_state(self, conn, key: str, default: str | None = None) -> str | None:
        row = conn.execute("SELECT value FROM broker_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_state(self, conn, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO broker_state (key, value) VALUES (?, ?)", (key, value))

    @property
    def cash(self) -> float:
        with db.connect(self.db_path) as conn:
            return float(self._get_state(conn, "cash", "0"))

    def trading_enabled(self) -> bool:
        with db.connect(self.db_path) as conn:
            return self._get_state(conn, "trading_enabled", "true") == "true"

    def set_trading_enabled(self, enabled: bool) -> None:
        with db.connect(self.db_path) as conn:
            self._set_state(conn, "trading_enabled", "true" if enabled else "false")

    # ---------- 查詢 ----------
    def positions(self) -> pd.DataFrame:
        # 返回的 stop_loss/target 已套用「截至今日」的除權息名目跳空調整。
        # 盤中停損監控（intraday_monitor）在盤中即讀本方法的 stop_loss，對照 shioaji
        # raw 快照 low 判觸價，且早於收盤 check_stops 發生——除息日當天若不同步，監控會
        # 拿「決策當日尺度」的停損去比「已除息跳空下跌」的 raw low，把配息名目跳空誤判
        # 成觸損砍出假虧損。此處與 check_stops 同口徑（_ex_gap_events），但只在記憶體內
        # 就地下修返回值、不回寫 DB：positions() 為純讀，避免每 30 秒輪詢造成鎖競爭，
        # 也避免歷史重放時以 today 為界誤扣掉 as_of 之後才發生的除息（check_stops 才是
        # 唯一以正確 as_of 回寫 ex_adj_date 的權威路徑）。
        with db.connect(self.db_path) as conn:
            pos = db.read_sql(conn, "SELECT * FROM positions ORDER BY stock_id")
            if pos.empty:
                return pos
            today = dt.date.today().isoformat()
            for idx, p in pos.iterrows():
                after = max(p.get("ex_adj_date") or "", p.get("opened_at") or "")
                if not after:   # 無安全基準日 → 不動（避免誤扣整段歷史除息）
                    continue
                total_gap = sum(g for _, g in _ex_gap_events(conn, p["stock_id"], after, today))
                if total_gap <= 0:
                    continue
                if pos.at[idx, "stop_loss"] is not None and pd.notna(pos.at[idx, "stop_loss"]):
                    pos.at[idx, "stop_loss"] = float(pos.at[idx, "stop_loss"]) - total_gap
                if pos.at[idx, "target"] is not None and pd.notna(pos.at[idx, "target"]):
                    pos.at[idx, "target"] = float(pos.at[idx, "target"]) - total_gap
            return pos

    def pending_orders(self) -> pd.DataFrame:
        with db.connect(self.db_path) as conn:
            return db.read_sql(conn, "SELECT * FROM orders WHERE status='pending' ORDER BY id")

    def orders(self, limit: int = 100) -> pd.DataFrame:
        """委託史（全狀態，新到舊）：pending/filled/expired/cancelled。"""
        with db.connect(self.db_path) as conn:
            return db.read_sql(conn, "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    def fills(self, limit: int = 200) -> pd.DataFrame:
        with db.connect(self.db_path) as conn:
            return db.read_sql(conn, "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))

    def equity_history(self) -> pd.DataFrame:
        with db.connect(self.db_path) as conn:
            return db.read_sql(conn, "SELECT * FROM equity_history ORDER BY date")

    def recent_stops(self, days: int = 30, as_of: str | None = None) -> dict[str, str]:
        """近期停損紀錄（餵 Guard 冷卻閘）。

        reason LIKE 'stop%' 同時涵蓋收盤 'stop' 與盤中 'stop_intraday'——盤中監控
        是排程預設啟用的主要停損路徑，只認 'stop' 會讓絕大多數停損漏進冷卻，剛停損
        的股票隔天就能被買回，違反 cooldown_days。cutoff 以 as_of（非 today）為基準，
        歷史重放時冷卻判斷才正確。
        """
        base = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
        cutoff = (base - dt.timedelta(days=days)).isoformat()
        sql = ("SELECT stock_id, MAX(date) AS d FROM fills "
               "WHERE reason LIKE 'stop%' AND date>=?")
        params: list = [cutoff]
        if as_of:  # 重放不看未來的停損
            sql += " AND date<=?"
            params.append(as_of)
        sql += " GROUP BY stock_id"
        with db.connect(self.db_path) as conn:
            rows = db.read_sql(conn, sql, tuple(params))
        return dict(zip(rows["stock_id"], rows["d"])) if not rows.empty else {}

    def portfolio_state(self, as_of: str | None = None) -> PortfolioState:
        """組出 Guard pipeline 需要的組合狀態（真實持倉 + 未成交委託）。

        納入 pending 委託：同一批決策逐檔跑 Guard 時，前面已掛的限價買單代表「已
        承諾的曝險與資金占用」，後面標的必須看得到——否則現金可被超掛、max_positions
        可被突破、同一檔可重複掛單（Guard 只看已成交持倉會全數漏掉）。
        pending 以限價估市值、預留現金（含手續費），冪等：撮合成交後委託消失、
        真實持倉補上，狀態自然接續。
        """
        cfg = get_settings()
        pos_df = self.positions()
        positions: dict[str, Position] = {}
        for r in pos_df.itertuples():
            px = q.get_price(r.stock_id, end=as_of)
            last = float(px["close"].iloc[-1]) if not px.empty else r.avg_cost
            positions[r.stock_id] = Position(
                shares=int(r.shares), value=int(r.shares) * last,
                industry=r.industry or "")
        reserved = 0.0
        for o in self.pending_orders().itertuples():
            cost = int(o.shares) * float(o.limit_price)
            reserved += cost + COST.buy_cost(cost)    # 預留資金（含手續費）
            if o.stock_id in positions:               # 已有持倉再掛加碼單 → 疊加曝險
                positions[o.stock_id].shares += int(o.shares)
                positions[o.stock_id].value += cost
            else:
                positions[o.stock_id] = Position(
                    shares=int(o.shares), value=cost, industry=(o.industry or ""))
        eq_hist = self.equity_history()
        peak = float(eq_hist["equity"].max()) if not eq_hist.empty else None
        # 停損窗口須 ≥ 冷卻天數：cooldown_days 若被設成 >30（季度級冷卻），
        # 固定 30 天窗口撈不到 31~cooldown 天前的停損，Guard 冷卻閘會靜默漏擋。
        cooldown = RiskConfig.from_settings(cfg).cooldown_days
        return PortfolioState(
            total_capital=float(cfg["capital"]["total"]),
            cash=max(0.0, self.cash - reserved),      # 扣掉已掛委託占用的現金
            positions=positions,
            recent_stops=self.recent_stops(days=max(30, cooldown), as_of=as_of),
            peak_equity=peak,
        )

    # ---------- 下單 ----------
    def place_buy(self, as_of: str, stock_id: str, shares: int, limit_price: float,
                  stop_loss: float | None, target: float | None, industry: str = "") -> int | None:
        """掛隔日限價買單。緊急停止時拒單回 None——kill-switch 在「掛單當下」
        再查一次，避免每日流程開跑後才按停止、進行中的決策照樣下單的競態。"""
        if not self.trading_enabled():
            return None
        # 預計撮合日＝掛單日之後的次「交易日」（跳過週末/假日）——明天休市時
        # 委託不會憑空消失，會保留到下一個開盤日撮合，這裡先算給人看
        expected = mcal.next_trading_day(as_of)
        with db.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO orders (created_as_of, stock_id, side, limit_price, shares, "
                "stop_loss, target, industry, status, expected_fill_date) "
                "VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
                (as_of, stock_id, "BUY", limit_price, shares, stop_loss, target,
                 industry, expected))
            return int(cur.lastrowid)

    def cancel_all_pending(self) -> int:
        with db.connect(self.db_path) as conn:
            cur = conn.execute("UPDATE orders SET status='cancelled' WHERE status='pending'")
            return cur.rowcount

    # ---------- 撮合與風控執行 ----------
    def execute_pending(self, date: str) -> list[dict]:
        """開盤撮合限價買單（隔日有效單，僅撮合 date 之前掛的單）：

        - 開盤價 ≤ 限價 → 以開盤價成交（買方更划算）；
        - 否則盤中最低 ≤ 限價 → 以限價成交（盤中觸價成交，貼近真實限價單）；
        - 全日皆未觸價 → 失效。

        只撈 created_as_of < date 的單：同日重複執行流程時，當天新掛的單留待
        次交易日撮合（冪等，避免拿掛單當天的行情誤殺）。

        休市防護（雙層）：date 非交易日、或 TAIEX 當日無日K（颱風臨時休市、
        價格尚未回補）→ 不動任何單直接返回。否則個股「無資料＝停牌失效」的
        規則會在市場根本沒開的日子把所有 pending 單誤殺。

        緊急停止防護：交易停用時，昨日掛的限價買單代表「未實現的新曝險」，
        撮合成交等於違反 kill-switch「不開新倉」語義——這裡直接撤銷所有待撮合
        委託（place_buy 只擋新單，擋不到已在隊列裡的舊單，此為那個洞的補防）。
        """
        if not mcal.is_trading_day(date):
            log.info("%s 非交易日（週末/假日），撮合跳過、委託保留", date)
            return []
        if not self.trading_enabled():
            with db.connect(self.db_path) as conn:
                pend = db.read_sql(
                    conn, "SELECT stock_id FROM orders WHERE status='pending'")
                conn.execute("UPDATE orders SET status='cancelled' WHERE status='pending'")
            out = [{"stock_id": s, "status": "cancelled_kill_switch"}
                   for s in pend["stock_id"].tolist()]
            if out:
                log.warning("⛔ 交易已停止：撤銷 %d 筆待撮合委託（kill-switch，不開新倉）",
                            len(out))
            return out
        if q.get_price("TAIEX", start=date, end=date).empty:
            log.warning("%s 無 TAIEX 日K（臨時休市或價格未回補）——撮合跳過、委託保留", date)
            return []
        results = []
        with db.connect(self.db_path, immediate=True) as conn:  # 序列化現金讀改寫
            orders = db.read_sql(
                conn, "SELECT * FROM orders WHERE status='pending' AND created_as_of < ?",
                (date,))
            cash = float(self._get_state(conn, "cash", "0"))
            for o in orders.to_dict(orient="records"):
                px = q.get_price(o["stock_id"], start=date, end=date)
                if px.empty:  # 當日無資料（停牌等）→ 失效
                    conn.execute("UPDATE orders SET status='expired' WHERE id=?", (o["id"],))
                    continue
                open_px = float(px["open"].iloc[0])
                low_px = float(px["low"].iloc[0])
                limit = float(o["limit_price"])
                fill_px = (open_px if open_px <= limit
                           else limit if low_px <= limit else None)
                if o["side"] == "BUY" and fill_px is not None:
                    open_px = fill_px
                    amount = o["shares"] * open_px
                    fee = COST.buy_cost(amount)
                    if amount + fee > cash:
                        conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (o["id"],))
                        results.append({"stock_id": o["stock_id"], "status": "cancelled_no_cash"})
                        continue
                    cash -= amount + fee
                    # 建倉/加碼（加權平均成本）
                    row = conn.execute(
                        "SELECT shares, avg_cost, stop_loss, target FROM positions WHERE stock_id=?",
                        (o["stock_id"],)).fetchone()
                    if row:
                        total_sh = row[0] + o["shares"]
                        avg = (row[0] * row[1] + amount) / total_sh
                        # 加碼不放寬既有部位的保護：停損取較高者（不下移，否則整個
                        # 合併部位的風險被靜默放大，超出 Guard 對新增股數算的預算）；
                        # 停利取較高者（新委託是加碼決策，朝更樂觀目標放行）；新單
                        # 缺值時保留舊值（不讓 None 抹掉既有停損停利）。
                        new_stop = _keep_higher(row[2], o["stop_loss"])
                        new_tgt = _keep_higher(row[3], o["target"])
                        # ex_adj_date 推進到成交日：加碼帶進的新 stop/target 是決策當日尺度，
                        # 成交日(含)以前的除息已內含於進場/加碼價，不可再被 _apply_ex 重扣。
                        conn.execute("UPDATE positions SET shares=?, avg_cost=?, stop_loss=?, target=?, "
                                     "ex_adj_date=? WHERE stock_id=?",
                                     (total_sh, avg, new_stop, new_tgt, date, o["stock_id"]))
                    else:
                        # ex_adj_date 設為建倉日：進場價已是成交日 raw 價（含當日除息跳空），
                        # 該日(含)以前的除息事件不得再對 stop/target 重扣。
                        conn.execute(
                            "INSERT INTO positions (stock_id, shares, avg_cost, stop_loss, target, "
                            "industry, opened_at, plan_as_of, ex_adj_date) VALUES (?,?,?,?,?,?,?,?,?)",
                            (o["stock_id"], o["shares"], open_px, o["stop_loss"], o["target"],
                             o["industry"], date, o["created_as_of"], date))
                    conn.execute("UPDATE orders SET status='filled', fill_date=?, fill_price=? WHERE id=?",
                                 (date, open_px, o["id"]))
                    conn.execute("INSERT INTO fills (date, stock_id, side, shares, price, fee, tax, reason) "
                                 "VALUES (?,?,?,?,?,?,0,'entry')",
                                 (date, o["stock_id"], "BUY", o["shares"], open_px, fee))
                    results.append({"stock_id": o["stock_id"], "status": "filled",
                                    "price": open_px, "shares": o["shares"]})
                else:
                    conn.execute("UPDATE orders SET status='expired' WHERE id=?", (o["id"],))
                    results.append({"stock_id": o["stock_id"], "status": "expired"})
            self._set_state(conn, "cash", str(cash))
        return results

    def intraday_exit(self, date: str, stock_id: str, price: float, reason: str) -> dict | None:
        """盤中即時出場（停損監控觸發）：以指定價格立即平倉單一持股。

        與 check_stops 相同的費稅/損益/帳本邏輯；持股不存在回 None（冪等，
        收盤 check_stops 再跑到同一檔也不會重複出場）。
        """
        with db.connect(self.db_path, immediate=True) as conn:  # 序列化現金讀改寫
            pos = db.read_sql(conn, "SELECT * FROM positions WHERE stock_id=?", (stock_id,))
            if pos.empty:
                return None
            p = pos.to_dict(orient="records")[0]
            cash = float(self._get_state(conn, "cash", "0"))
            amount = p["shares"] * price
            fee = max(COST.fee_rate * COST.fee_discount * amount, COST.min_fee)  # 最低 20 元
            tax = COST.tax_rate * amount
            pnl = (price - p["avg_cost"]) * p["shares"] - fee - tax
            cash += amount - fee - tax
            conn.execute("DELETE FROM positions WHERE stock_id=?", (stock_id,))
            conn.execute(
                "INSERT INTO fills (date, stock_id, side, shares, price, fee, tax, pnl, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (date, stock_id, "SELL", p["shares"], price, fee, tax, pnl, reason))
            self._set_state(conn, "cash", str(cash))
        return {"stock_id": stock_id, "reason": reason, "price": price,
                "shares": p["shares"], "pnl": round(pnl, 0)}

    def _apply_ex_dividend_adjust(self, conn, date: str) -> None:
        """除權息同步：對持倉的 stop_loss/target 扣掉截至 date(含) 未套用的名目跳空。

        撮合/停損用 raw 日K，除息日 raw 價跳空下跌約當息值，但決策的 stop/target 建立
        在決策當日價格尺度——不校正會把「配息名目跳空」誤判成觸損/觸標，砍出假虧損。
        比照真實券商在除息日對條件單同步下修息值。

        冪等：ex_adj_date 記已套用的最後除息日，只套用 date>ex_adj_date（起點為
        opened_at∪ex_adj_date，取較晚者）的事件——同一除息日不會重複扣兩次。
        須在 check_stops 讀持倉判觸價「之前」呼叫（同一 immediate 交易內）。
        """
        pos = db.read_sql(
            conn, "SELECT stock_id, stop_loss, target, opened_at, ex_adj_date FROM positions")
        for p in pos.to_dict(orient="records"):
            # 起算點：建倉日與上次已調整日取較晚者（建倉當日的除息事件已內含於進場價，
            # 不重扣；execute_pending 建倉時已把 ex_adj_date 設為建倉日）。
            after = max(p.get("ex_adj_date") or "", p.get("opened_at") or "")
            if not after:   # 兩者皆缺 → 無安全基準日，跳過（避免誤扣整段歷史除息）
                continue
            events = _ex_gap_events(conn, p["stock_id"], after, date)
            if not events:
                continue
            total_gap = sum(g for _, g in events)
            last_ex = events[-1][0]
            new_stop = (float(p["stop_loss"]) - total_gap) if p["stop_loss"] is not None else None
            new_tgt = (float(p["target"]) - total_gap) if p["target"] is not None else None
            conn.execute(
                "UPDATE positions SET stop_loss=?, target=?, ex_adj_date=? WHERE stock_id=?",
                (new_stop, new_tgt, last_ex, p["stock_id"]))
            log.info("除權息條件單調整 %s：除息日 %s 累計息值 %.2f → 停損 %s、停利 %s",
                     p["stock_id"], last_ex, total_gap,
                     f"{new_stop:.2f}" if new_stop is not None else "—",
                     f"{new_tgt:.2f}" if new_tgt is not None else "—")

    def check_stops(self, date: str) -> list[dict]:
        """收盤風控：low ≤ 停損 → 以停損價出場；high ≥ 停利 → 以目標價出場（雙觸以停損計）。

        跳空穿越夾價：出場價夾回當日實際 [open, high/low] 區間——開盤就跳空跌破
        停損（open < stop）以開盤價成交（停損價當日根本不存在，否則虧損被低估、
        甚至記出當日最高價之上的假成交）；跳空突破停利（open > target）以開盤價
        成交（實際賣得更好）。

        觸價判定前先做除權息同步（見 _apply_ex_dividend_adjust）：抱過除息日的持倉，
        raw 價的名目跳空已同步扣進 stop/target，避免配息被誤判成觸損砍出假虧損。
        """
        results = []
        with db.connect(self.db_path, immediate=True) as conn:  # 序列化現金讀改寫
            self._apply_ex_dividend_adjust(conn, date)   # 先同步除權息，再判觸價
            pos = db.read_sql(conn, "SELECT * FROM positions")
            cash = float(self._get_state(conn, "cash", "0"))
            for p in pos.to_dict(orient="records"):
                px = q.get_price(p["stock_id"], start=date, end=date)
                if px.empty:
                    continue
                open_, low, high = (float(px["open"].iloc[0]), float(px["low"].iloc[0]),
                                    float(px["high"].iloc[0]))
                exit_price, reason = None, None
                if p["stop_loss"] and low <= float(p["stop_loss"]):
                    exit_price, reason = min(float(p["stop_loss"]), open_), "stop"
                elif p["target"] and high >= float(p["target"]):
                    exit_price, reason = max(float(p["target"]), open_), "target"
                if exit_price is None:
                    continue
                amount = p["shares"] * exit_price
                fee = max(COST.fee_rate * COST.fee_discount * amount, COST.min_fee)  # 最低 20 元
                tax = COST.tax_rate * amount
                pnl = (exit_price - p["avg_cost"]) * p["shares"] - fee - tax
                cash += amount - fee - tax
                conn.execute("DELETE FROM positions WHERE stock_id=?", (p["stock_id"],))
                conn.execute(
                    "INSERT INTO fills (date, stock_id, side, shares, price, fee, tax, pnl, reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (date, p["stock_id"], "SELL", p["shares"], exit_price, fee, tax, pnl, reason))
                results.append({"stock_id": p["stock_id"], "reason": reason,
                                "price": exit_price, "pnl": round(pnl, 0)})
            self._set_state(conn, "cash", str(cash))
        return results

    def mark_to_market(self, date: str) -> dict:
        """收盤權益快照（含 TAIEX 對照）。"""
        with db.connect(self.db_path) as conn:
            pos = db.read_sql(conn, "SELECT * FROM positions")
            cash = float(self._get_state(conn, "cash", "0"))
            value = 0.0
            for p in pos.to_dict(orient="records"):
                px = q.get_price(p["stock_id"], end=date)
                last = float(px["close"].iloc[-1]) if not px.empty else p["avg_cost"]
                value += p["shares"] * last
            taiex = q.get_price("TAIEX", start=date, end=date)
            taiex_close = float(taiex["close"].iloc[0]) if not taiex.empty else None
            equity = cash + value
            conn.execute(
                "INSERT OR REPLACE INTO equity_history (date, cash, positions_value, equity, taiex_close) "
                "VALUES (?,?,?,?,?)", (date, cash, value, equity, taiex_close))
        return {"date": date, "cash": round(cash, 0), "positions_value": round(value, 0),
                "equity": round(equity, 0)}