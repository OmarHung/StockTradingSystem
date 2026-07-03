"""Claude API 封裝：結構化輸出 + provider fallback chain + 呼叫記錄。

- 用官方 anthropic SDK 的 client.messages.parse() 取得經 schema 驗證的物件。
- fallback：主模型遇到過載/限流/伺服器錯誤時，自動改用備援模型重試，
  讓每日排程不因單次 API 故障中斷。
- 每次呼叫都寫入 brain_log（供 WebUI「大腦活動」頁）。
"""
from __future__ import annotations

import datetime as dt
import json
import os

import anthropic
from pydantic import BaseModel

from src.config import get_settings
from src.data import database as db
from src.logging_setup import get_logger

log = get_logger(__name__)

# 主模型失敗時的備援順序（能力遞減但可用性遞增）
FALLBACK_CHAIN = ["claude-sonnet-4-6", "claude-haiku-4-5"]


class LLMUnavailable(RuntimeError):
    """未設定 API key 或所有模型都失敗。"""


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _client() -> anthropic.Anthropic:
    if not has_api_key():
        raise LLMUnavailable("未設定 ANTHROPIC_API_KEY，請到 WebUI 設定中心填入。")
    # 防護：某些 shell profile 會設一個「空的」ANTHROPIC_AUTH_TOKEN，
    # SDK 會誤用它送出壞掉的 `Authorization: Bearer `（LocalProtocolError）。
    # 空值時移除，讓 SDK 走 x-api-key 路徑。
    if not (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip():
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def call_structured(
    *,
    model: str,
    system: str,
    user_prompt: str,
    schema: type[BaseModel],
    agent: str,
    stock_id: str | None = None,
    as_of: str | None = None,
    max_tokens: int = 4000,
    use_thinking: bool = False,
) -> BaseModel:
    """呼叫 Claude 取得結構化輸出（經 Pydantic schema 驗證）。

    依 [主模型] + FALLBACK_CHAIN 順序嘗試，遇可重試錯誤才降級。回傳 schema 實例。
    """
    client = _client()
    models = [model] + [m for m in FALLBACK_CHAIN if m != model]

    kwargs: dict = {}
    if use_thinking:
        # Opus 4.8 adaptive thinking；不可用 temperature/budget_tokens
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "high"}

    last_err: Exception | None = None
    for m in models:
        try:
            resp = client.messages.parse(
                model=m,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
                output_format=schema,
                **kwargs,
            )
            result = resp.parsed_output
            _log_call(agent, m, user_prompt, result.model_dump_json(), stock_id, as_of)
            return result
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            log.warning("模型 %s 暫時失敗（%s），嘗試 fallback", m, type(e).__name__)
            last_err = e
            continue
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code == 529:
                log.warning("模型 %s 伺服器錯誤 %s，fallback", m, e.status_code)
                last_err = e
                continue
            raise  # 4xx（如 schema 問題）不重試

    raise LLMUnavailable(f"所有模型都失敗：{last_err}")


def _log_call(agent, model, prompt, response, stock_id, as_of) -> None:
    try:
        with db.connect(get_settings().db_path) as conn:
            conn.execute(
                "INSERT INTO brain_log (ts, as_of, stock_id, agent, model, prompt, response) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dt.datetime.now().isoformat(timespec="seconds"), as_of, stock_id,
                 agent, model, prompt, response),
            )
    except Exception as e:  # noqa: BLE001 — 記錄失敗不應中斷主流程
        log.error("brain_log 寫入失敗：%s", e)


def log_note(agent: str, note: str, stock_id: str | None = None, as_of: str | None = None) -> None:
    """記錄非 LLM 事件（如驗證層攔截）到 brain_log。"""
    try:
        with db.connect(get_settings().db_path) as conn:
            conn.execute(
                "INSERT INTO brain_log (ts, as_of, stock_id, agent, note) VALUES (?, ?, ?, ?, ?)",
                (dt.datetime.now().isoformat(timespec="seconds"), as_of, stock_id, agent, note),
            )
    except Exception as e:  # noqa: BLE001
        log.error("brain_log note 寫入失敗：%s", e)
