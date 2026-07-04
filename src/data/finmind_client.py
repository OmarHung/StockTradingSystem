"""FinMind API v4 薄客戶端。

只負責「打 API 拿回 DataFrame」，不碰資料庫。含重試與請求節流。
FinMind 文件：https://finmind.github.io/
"""
from __future__ import annotations

import time

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.logging_setup import get_logger

log = get_logger(__name__)


class FinMindError(RuntimeError):
    pass


class FinMindQuotaExhausted(RuntimeError):
    """HTTP 402：當日額度用罄。重試無意義，呼叫端應改用備援資料源。"""


class FinMindClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        request_interval_sec: float = 0.35,
        max_retries: int = 4,
        quota_wait: bool = False,
        on_quota_wait=None,
        max_quota_waits: int = 30,
    ):
        """quota_wait=True：402 額度用罄時自動等到下個整點（FinMind 每小時重置）再重試，
        而非拋出例外——供背景/過夜自動更新使用。on_quota_wait(resume_at) 為等待通知回呼。"""
        self.base_url = base_url
        self.token = token
        self.request_interval_sec = request_interval_sec
        self.max_retries = max_retries
        self.quota_wait = quota_wait
        self.on_quota_wait = on_quota_wait
        self.max_quota_waits = max_quota_waits
        self._quota_waits = 0
        self._last_request_ts = 0.0
        self._session = requests.Session()

    def _wait_for_quota_reset(self) -> None:
        """睡到下個整點 + 90 秒緩衝（FinMind 額度以小時為單位重置）。"""
        import datetime as dt

        self._quota_waits += 1
        if self._quota_waits > self.max_quota_waits:
            raise FinMindQuotaExhausted(
                f"已等待額度重置 {self.max_quota_waits} 次仍用罄，放棄（明日再續）")
        now = dt.datetime.now()
        resume = (now.replace(minute=0, second=0, microsecond=0)
                  + dt.timedelta(hours=1, seconds=90))
        wait_sec = max((resume - now).total_seconds(), 60)
        log.warning("FinMind 額度用罄，等待至 %s 自動續跑（第 %d 次等待）",
                    resume.strftime("%H:%M:%S"), self._quota_waits)
        if self.on_quota_wait:
            try:
                self.on_quota_wait(resume.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception:  # noqa: BLE001
                pass
        time.sleep(wait_sec)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.request_interval_sec:
            time.sleep(self.request_interval_sec - elapsed)
        self._last_request_ts = time.monotonic()

    def get_dataset(
        self,
        dataset: str,
        data_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """呼叫單一 dataset，回傳 DataFrame（無資料則為空 DataFrame）。"""

        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type((requests.RequestException, FinMindError)),
            reraise=True,
        )
        def _call() -> pd.DataFrame:
            self._throttle()
            params: dict[str, str] = {"dataset": dataset}
            if data_id:
                params["data_id"] = data_id
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
            if self.token:
                params["token"] = self.token

            resp = self._session.get(self.base_url, params=params, timeout=30)

            # 402 = 額度用罄：重試無意義，直接讓呼叫端切換備援源
            if resp.status_code == 402:
                raise FinMindQuotaExhausted("FinMind 額度用罄（HTTP 402）")
            # 429 = 頻率限制：退避重試
            if resp.status_code == 429:
                raise FinMindError("FinMind 頻率限制（HTTP 429），稍後重試")
            resp.raise_for_status()

            payload = resp.json()
            if payload.get("status") != 200:
                raise FinMindError(f"FinMind 回應異常：{payload.get('msg')}")

            data = payload.get("data", [])
            return pd.DataFrame(data)

        # quota_wait 模式：402 時等到額度重置自動續跑（背景/過夜更新用）
        while True:
            try:
                return _call()
            except FinMindQuotaExhausted:
                if not self.quota_wait:
                    raise
                self._wait_for_quota_reset()
