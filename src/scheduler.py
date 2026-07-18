"""後端內建排程器（取代 macOS launchd）。

設計：排程器只是「觸發器」，住在 API 行程的 asyncio 迴圈裡；
實際工作仍透過 src/jobs.py 開獨立子行程執行（與 WebUI 手動觸發同一路徑），
所以 pipeline 崩潰不影響 API、API 重啟也只是暫停觸發、不會殺掉進行中的工作。

補跑語義：每 30 秒檢查一次「今天(平日)、已過設定時間、今天還沒跑過、job 沒在跑」
→ 觸發。因此 API 行程在排定時間之後才啟動（如中午開機），當天仍會補跑一次。
執行紀錄存 DB scheduler_runs 表（每 job 每天一列，UPSERT）。
"""
from __future__ import annotations

import asyncio
import datetime as dt

from src import jobs
from src.config import get_settings
from src.data import database as db
from src.data import market_calendar as mcal
from src.logging_setup import get_logger

log = get_logger("scheduler")

# job 定義：name -> (顯示名, jobs.py 的 job 名, 啟動參數)
JOB_DEFS = {
    "intraday": {
        "label": "盤中停損監控",
        "job_name": "intraday",
        "args": ["scripts.intraday_monitor"],   # 09:00 啟動，收盤/無持倉自行結束
        # keepalive：盤中時段窗口內只要沒在跑就（重）啟動，不看「今天跑過沒」——
        # 崩潰後當天能重啟，持倉停損保護不消失。收盤後窗口關閉，重啟也會即刻自退。
        "keepalive": True,
        "keepalive_until": "13:32",
    },
    "dataupdate": {
        "label": "盤後價格更新",
        "job_name": "backfill",                      # 與 WebUI 資料視窗共用監控
        # 只拉股價日K（shioaji 單路徑，不吃 FinMind 額度、幾分鐘完成）——
        # 盤後決策唯一硬相依的 T 日資料。不帶 --auto-wait：價格路徑不走 FinMind，
        # 帶了反而會在 stock_info 額度用罄時空等整點。
        "args": ["scripts.backfill", "--datasets", "price_daily"],
    },
    "nightly": {
        "label": "夜間資料補全",
        "job_name": "backfill",
        # 法人/融資券/估值 T 日資料約 16:00 後才發布，14:30 拉必定落空；
        # 移到晚上一次補全（決策容忍這些資料 T-1，見 daily.py 新鮮度閘門）。
        "args": ["scripts.backfill", "--auto-wait"],
    },
    "daily": {
        "label": "每日交易主流程",
        "job_name": "daily",
        "args": ["scripts.run_daily"],
        # 回補還在跑就延後：避免 SQLite 寫入鎖衝突（曾使流程中途崩潰），
        # 也確保決策用的是更新完的資料。每 30 秒重試，回補結束即補跑。
        "wait_for": ["backfill"],
    },
}

_DEFER_LOGGED: set[tuple[str, str]] = set()  # (job, date) 延後只記一次 log，避免每 30 秒洗版

_DDL = """
    CREATE TABLE IF NOT EXISTS scheduler_runs (
        name       TEXT NOT NULL,
        run_date   TEXT NOT NULL,     -- 觸發日 YYYY-MM-DD
        started_at TEXT,
        source     TEXT,              -- schedule / manual
        PRIMARY KEY (name, run_date)
    )
"""


def _conn():
    conn = db.get_connection(get_settings().db_path)
    conn.execute(_DDL)
    return conn


def _cfg() -> dict:
    """讀 settings.yaml 的 scheduler 區塊（每次直讀檔案，即時生效）。

    直讀 config_io.load_raw（非 get_settings）：後者是 lru_cache，只有經 WebUI
    save_raw 才清快取；在 VPS 上直接 vim settings.yaml 改排程時間/開關時，
    get_settings 會一直回舊值到重啟。每 30 秒讀一次檔的成本可忽略。
    """
    try:
        from src import config_io
        raw = config_io.load_raw().get("scheduler") or {}
        return {k: dict(v) for k, v in dict(raw).items()}
    except Exception:  # noqa: BLE001
        return {}


def trigger(name: str, source: str = "schedule") -> bool:
    """啟動一個排程 job（若已在執行則跳過）。記錄到 scheduler_runs。"""
    spec = JOB_DEFS[name]
    if jobs.is_running(spec["job_name"]):
        log.info("排程 %s 略過：job %s 仍在執行", name, spec["job_name"])
        return False
    ok = jobs.start_job(spec["job_name"], spec["args"])
    if ok:
        now = dt.datetime.now()
        with _conn() as conn:
            conn.execute(
                "INSERT INTO scheduler_runs (name, run_date, started_at, source) "
                "VALUES (?,?,?,?) ON CONFLICT(name, run_date) DO UPDATE SET "
                "started_at=excluded.started_at, source=excluded.source",
                (name, now.date().isoformat(), now.isoformat(timespec="seconds"), source),
            )
            conn.commit()
        log.info("排程觸發 %s（%s，source=%s）", name, spec["label"], source)
    return ok


def _ran_today(name: str, today: str) -> bool:
    with _conn() as conn:
        r = conn.execute("SELECT 1 FROM scheduler_runs WHERE name=? AND run_date=?",
                         (name, today)).fetchone()
    return r is not None


def _due(name: str, spec_cfg: dict, now: dt.datetime) -> bool:
    """今天(交易日)、已過設定時間、今天沒跑過 → 到期。"""
    if not spec_cfg.get("enabled", False):
        return False
    # 週末與國定假日都不跑（allow_fetch=False：asyncio 迴圈內不打網路，
    # 假日表由 nightly 回補與各子行程 lazy 同步）
    if not mcal.is_trading_day(now.date().isoformat(), allow_fetch=False):
        return False
    try:
        hh, mm = str(spec_cfg.get("time", "")).split(":")
        due_at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except ValueError:
        return False
    if now < due_at:
        return False
    spec = JOB_DEFS[name]
    if spec.get("keepalive"):
        # 時段窗口內只要沒在跑就（重）啟動；trigger 另有 is_running 防雙啟動
        try:
            eh, em = str(spec.get("keepalive_until", "13:32")).split(":")
            until = now.replace(hour=int(eh), minute=int(em), second=0, microsecond=0)
        except ValueError:
            return False
        return now < until and not jobs.is_running(spec["job_name"])
    return not _ran_today(name, now.date().isoformat())


async def run_loop(interval_sec: int = 30) -> None:
    """常駐檢查迴圈（FastAPI startup 啟動）。單一 API 行程內執行。"""
    log.info("內建排程器啟動（每 %d 秒檢查；設定見 settings.yaml scheduler 區塊）", interval_sec)
    while True:
        try:
            cfg = _cfg()
            now = dt.datetime.now()
            for name in JOB_DEFS:
                if name not in cfg or not _due(name, cfg[name], now):
                    continue
                blockers = [b for b in JOB_DEFS[name].get("wait_for", [])
                            if jobs.is_running(b)]
                if blockers:
                    key = (name, now.date().isoformat())
                    if key not in _DEFER_LOGGED:
                        _DEFER_LOGGED.add(key)
                        log.info("排程 %s 延後：等待 %s 結束（結束後自動補跑）",
                                 name, "、".join(blockers))
                    continue
                trigger(name, source="schedule")
        except Exception as e:  # noqa: BLE001
            log.error("排程器檢查失敗（下輪重試）：%s", e)
        await asyncio.sleep(interval_sec)


def status() -> list[dict]:
    """各排程 job 的監控狀態（供 WebUI）。"""
    cfg = _cfg()
    now = dt.datetime.now()
    out = []
    with _conn() as conn:
        for name, spec in JOB_DEFS.items():
            c = cfg.get(name, {})
            last = conn.execute(
                "SELECT run_date, started_at, source FROM scheduler_runs "
                "WHERE name=? ORDER BY run_date DESC LIMIT 1", (name,)).fetchone()
            # 下次執行：今天未到時間→今天；否則下一個交易日（跳過週末/假日）
            nxt = None
            try:
                hh, mm = str(c.get("time", "")).split(":")
                cand = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if cand <= now or _ran_today(name, now.date().isoformat()):
                    cand += dt.timedelta(days=1)
                for _ in range(30):
                    if mcal.is_trading_day(cand.date().isoformat(), allow_fetch=False):
                        break
                    cand += dt.timedelta(days=1)
                nxt = cand.isoformat(timespec="minutes")
            except ValueError:
                pass
            out.append({
                "name": name, "label": spec["label"],
                "enabled": bool(c.get("enabled", False)),
                "time": str(c.get("time", "")),
                "running": jobs.is_running(spec["job_name"]),
                "last_run": ({"date": last[0], "started_at": last[1], "source": last[2]}
                             if last else None),
                "next_run": nxt if c.get("enabled", False) else None,
                "log_tail": "\n".join(
                    l for l in jobs.read_log(spec["job_name"], tail=8).splitlines()
                    if not l.startswith("@@PROGRESS@@"))[-400:],
            })
    return out
