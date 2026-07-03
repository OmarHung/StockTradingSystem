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


class FinMindClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        request_interval_sec: float = 0.35,
        max_retries: int = 4,
    ):
        self.base_url = base_url
        self.token = token
        self.request_interval_sec = request_interval_sec
        self.max_retries = max_retries
        self._last_request_ts = 0.0
        self._session = requests.Session()

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

            # 402 = 免費額度用罄；429 = 頻率限制 → 皆值得重試等待
            if resp.status_code in (402, 429):
                raise FinMindError(
                    f"FinMind 額度/頻率限制（HTTP {resp.status_code}），稍後重試"
                )
            resp.raise_for_status()

            payload = resp.json()
            if payload.get("status") != 200:
                raise FinMindError(f"FinMind 回應異常：{payload.get('msg')}")

            data = payload.get("data", [])
            return pd.DataFrame(data)

        return _call()
