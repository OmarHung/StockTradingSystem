"""Claude 模型清單與能力查詢。

兩個用途：
1. list_models()：呼叫 Anthropic Models API 取「最新前 N 個」模型與能力（context 上限、
   output 上限、是否支援 adaptive thinking），供 WebUI 設定頁下拉切換。無 API key 或
   查詢失敗時，回傳內建靜態後備清單，讓 UI 仍可運作。
2. get_caps(model_id)：查單一模型能力，供 client.call_structured 動態決定——
   不支援思考的模型（如 Haiku 4.5）就不送 thinking 參數（否則 400），
   並把 max_tokens 夾在該模型的 output 上限內。

get_caps 走「靜態表為主、Models API 快取為輔」——即使沒先連過網路也能安全判斷，
不會拖慢每次 LLM 呼叫。
"""
from __future__ import annotations

from src.logging_setup import get_logger

log = get_logger(__name__)

# 內建能力表（key 以基礎 model id 前綴比對，故含日期尾碼的 id 也能命中）。
# supports_thinking = 是否支援 adaptive thinking（Haiku 4.5 不支援）。
_STATIC: dict[str, dict] = {
    "claude-fable-5":    {"display_name": "Claude Fable 5",    "context_window": 1_000_000, "max_output": 128_000, "supports_thinking": True},
    "claude-opus-4-8":   {"display_name": "Claude Opus 4.8",   "context_window": 1_000_000, "max_output": 128_000, "supports_thinking": True},
    "claude-opus-4-7":   {"display_name": "Claude Opus 4.7",   "context_window": 1_000_000, "max_output": 128_000, "supports_thinking": True},
    "claude-opus-4-6":   {"display_name": "Claude Opus 4.6",   "context_window": 1_000_000, "max_output": 128_000, "supports_thinking": True},
    "claude-sonnet-4-6": {"display_name": "Claude Sonnet 4.6", "context_window": 1_000_000, "max_output": 64_000,  "supports_thinking": True},
    "claude-haiku-4-5":  {"display_name": "Claude Haiku 4.5",  "context_window": 200_000,   "max_output": 64_000,  "supports_thinking": False},
}
# 未知模型的保守預設：不開思考、低 output 上限（避免對不支援的模型送出會 400 的參數）。
_DEFAULT_CAPS: dict = {"display_name": None, "context_window": 200_000, "max_output": 8_000, "supports_thinking": False}

# Models API 查到的即時能力快取（key 為完整 model id），由 list_models() 填入，供 get_caps 優先參考。
_LIVE_CAPS: dict[str, dict] = {}

# 靜態後備清單的顯示順序（新到舊，取前 5）。
_STATIC_ORDER = [
    "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-sonnet-4-6", "claude-haiku-4-5",
]


def _dig(d, *keys):
    """安全巡覽巢狀 dict；任一層缺失回傳 None。"""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _match(table: dict[str, dict], model_id: str) -> dict | None:
    """以前綴比對從能力表取出模型（先試完全相等再試 startswith）。"""
    if model_id in table:
        return table[model_id]
    for base, caps in table.items():
        if model_id.startswith(base):
            return caps
    return None


def get_caps(model_id: str) -> dict:
    """回傳模型能力 dict：display_name / context_window / max_output / supports_thinking。

    先查 Models API 即時快取，再查內建靜態表，都沒有則回傳保守預設。
    此函式不觸發網路，可安全用於每次 LLM 呼叫。
    """
    return _match(_LIVE_CAPS, model_id) or _match(_STATIC, model_id) or dict(_DEFAULT_CAPS)


def _caps_from_api_model(m) -> dict:
    """把 Models API 的模型物件轉成本模組的能力 dict。"""
    caps = getattr(m, "capabilities", None)
    # capabilities 是 Pydantic 模型（ModelCapabilities），先轉純 dict 才能巡覽。
    if caps is not None and hasattr(caps, "model_dump"):
        caps = caps.model_dump()
    adaptive = _dig(caps, "thinking", "types", "adaptive", "supported")
    return {
        "id": m.id,
        "display_name": getattr(m, "display_name", None) or m.id,
        "context_window": getattr(m, "max_input_tokens", None),
        "max_output": getattr(m, "max_tokens", None),
        "supports_thinking": bool(adaptive),
    }


def _static_list() -> list[dict]:
    return [{"id": k, **_STATIC[k]} for k in _STATIC_ORDER]


def list_models(top_n: int = 5) -> list[dict]:
    """回傳最新前 top_n 個 Claude 模型與能力（供 WebUI 下拉）。

    有 API key 時走 Models API（依 created_at 由新到舊排序取前 N），並把能力寫入即時快取；
    無 key 或失敗時回傳內建靜態後備清單。
    """
    # 延遲載入避免與 client 互相 import 造成循環。
    from src.llm import client as llm

    if not llm.has_api_key():
        return _static_list()[:top_n]
    try:
        client = llm._client()
        models = list(client.models.list())
        # 依建立時間新到舊排序（缺 created_at 者排後）。
        models.sort(key=lambda m: getattr(m, "created_at", "") or "", reverse=True)
        out: list[dict] = []
        for m in models:
            if not str(m.id).startswith("claude-"):
                continue
            caps = _caps_from_api_model(m)
            _LIVE_CAPS[m.id] = {k: caps[k] for k in ("display_name", "context_window", "max_output", "supports_thinking")}
            out.append(caps)
            if len(out) >= top_n:
                break
        return out or _static_list()[:top_n]
    except Exception as e:  # noqa: BLE001 — 查詢失敗不應讓設定頁壞掉
        log.warning("Models API 查詢失敗（%s），改用內建清單", e)
        return _static_list()[:top_n]
