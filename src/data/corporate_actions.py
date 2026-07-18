"""公司行動跳空偵測器：分割/反分割/減資/面額變更 的價格調整事件。

原理：台股漲跌幅限制 ±10%，正常交易日 |收盤對前收報酬| 不可能超過 ~10%。
超過門檻的跳空、且當日不是已知除權息日 → 必為公司行動（0050 一拆四、
減資彈升、面額變更等），寫入 capital_change 供還原價計算合併使用。

比逐一介接官方公告表（TWSE 減資 TWTAVU / ETF 分割 / 面額變更、TPEx 對應表
共 6+ 張、多為快照式）穩健：任何市場、任何行動類型、歷史與未來一體適用。

已知限制（精準優先於召回）：
- 小幅減資（跳空 < 門檻）偵測不到——對指標影響微小，可接受
- 新上市前 10 個交易日無漲跌幅限制 → 跳過（避免誤判蜜月行情）
"""
from __future__ import annotations

from src.data import database as db
from src.logging_setup import get_logger

log = get_logger(__name__)

# |日報酬| 超過此值視為公司行動（漲跌幅限制 10% + 安全餘裕）
GAP_THRESHOLD = 0.15
# 新上市頭 N 個交易日不設漲跌幅限制 → 不偵測
IPO_SKIP_DAYS = 10


def detect(conn, stock_id: str | None = None) -> int:
    """掃描 price_daily 找公司行動跳空，寫入 capital_change。冪等。

    stock_id=None 掃全部。回傳新寫入事件數。
    """
    where = "WHERE stock_id=?" if stock_id else ""
    params: tuple = (stock_id,) if stock_id else ()

    # 自清理：移除與 dividend 同日的 capital_change 誤判列。早期版本在 dividend
    # 尚未入庫時就偵測，除息跳空（>15%）被誤判成減資寫入本表，之後與同日除息
    # 事件疊乘造成還原價雙重調整。dividend 是同日事件權威來源，這裡實體移除誤判
    # （query 層另有 NOT EXISTS 防護；此處讓每次回補自愈，涵蓋既有壞資料）。
    cleaned = conn.execute(
        "DELETE FROM capital_change WHERE EXISTS ("
        "  SELECT 1 FROM dividend d WHERE d.stock_id=capital_change.stock_id "
        "  AND d.date=capital_change.date)"
        + (" AND stock_id=?" if stock_id else ""),
        params).rowcount
    if cleaned:
        log.info("清理與除權息同日的公司行動誤判：%d 列", cleaned)

    rows = conn.execute(f"""
        WITH seq AS (
            SELECT stock_id, date, close,
                   LAG(close) OVER (PARTITION BY stock_id ORDER BY date) AS prev_close,
                   ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY date) AS rn
            FROM price_daily {where}
        )
        SELECT s.stock_id, s.date, s.prev_close, s.close
        FROM seq s
        LEFT JOIN dividend d ON d.stock_id = s.stock_id AND d.date = s.date
        WHERE s.rn > {IPO_SKIP_DAYS}
          AND s.prev_close > 0 AND s.close > 0
          AND ABS(s.close / s.prev_close - 1.0) > {GAP_THRESHOLD}
          AND d.stock_id IS NULL          -- 除權息日的跳空由 dividend 表處理
          AND s.stock_id NOT IN ('TAIEX', 'TPEx')
    """, params).fetchall()

    n = 0
    for sid, date, before, after in rows:
        kind = "auto_split" if after < before else "auto_reduction"
        cur = conn.execute(
            "INSERT INTO capital_change (stock_id, date, before_price, after_price, kind) "
            "VALUES (?,?,?,?,?) ON CONFLICT(stock_id, date) DO NOTHING",
            (sid, date, round(before, 2), round(after, 2), kind))
        if cur.rowcount:
            n += 1
            log.info("公司行動偵測：%s %s %s %.2f→%.2f（係數 %.4f）",
                     sid, date, kind, before, after, after / before)
    if n:
        conn.commit()
    return n
