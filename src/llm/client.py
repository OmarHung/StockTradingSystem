"""Claude API 封裝：結構化輸出 + provider fallback chain + 呼叫記錄。

- 用官方 anthropic SDK 的 client.messages.parse() 取得經 schema 驗證的物件。
- fallback：主模型遇到過載/限流/伺服器錯誤時，自動改用備援模型重試，
  讓每日排程不因單次 API 故障中斷。
- 每次呼叫都寫入 brain_log（供 WebUI「大腦活動」頁）。
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import uuid

import anthropic
from pydantic import BaseModel

from src.config import get_settings
from src.data import database as db
from src.llm import models as model_caps
from src.logging_setup import get_logger

log = get_logger(__name__)

# 主模型失敗時的備援順序（能力遞減但可用性遞增）
FALLBACK_CHAIN = ["claude-sonnet-4-6", "claude-haiku-4-5"]


class LLMUnavailable(RuntimeError):
    """未設定 API key 或所有模型都失敗。"""


# 當前決策管線批次 ID：pipeline 開跑時 new_run()，其間所有 LLM 呼叫/攔截
# 記錄都帶同一 run_id，供「大腦活動」把一次分析的多個 Agent 分成同一組。
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("brain_run_id", default=None)


def new_run() -> str:
    """開始一個新的決策管線批次，回傳批次 ID。"""
    rid = uuid.uuid4().hex[:12]
    _run_id.set(rid)
    return rid


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

    last_err: Exception | None = None
    for m in models:
        # 依「該模型」能力決定參數：不支援 adaptive thinking 的模型（如 Haiku 4.5）
        # 不可送 thinking/effort（會 400）；max_tokens 也夾在該模型的 output 上限內。
        caps = model_caps.get_caps(m)
        kwargs: dict = {}
        if use_thinking and caps.get("supports_thinking"):
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": "high"}
        cap_out = caps.get("max_output") or max_tokens
        mt = min(max_tokens, cap_out)
        try:
            resp = client.messages.parse(
                model=m,
                max_tokens=mt,
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
                "INSERT INTO brain_log (ts, as_of, stock_id, agent, model, prompt, response, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (dt.datetime.now().isoformat(timespec="seconds"), as_of, stock_id,
                 agent, model, prompt, response, _run_id.get()),
            )
    except Exception as e:  # noqa: BLE001 — 記錄失敗不應中斷主流程
        log.error("brain_log 寫入失敗：%s", e)


def log_note(agent: str, note: str, stock_id: str | None = None, as_of: str | None = None) -> None:
    """記錄非 LLM 事件（如驗證層攔截）到 brain_log。"""
    try:
        with db.connect(get_settings().db_path) as conn:
            conn.execute(
                "INSERT INTO brain_log (ts, as_of, stock_id, agent, note, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (dt.datetime.now().isoformat(timespec="seconds"), as_of, stock_id,
                 agent, note, _run_id.get()),
            )
    except Exception as e:  # noqa: BLE001
        log.error("brain_log note 寫入失敗：%s", e)
