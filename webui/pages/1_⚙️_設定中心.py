"""設定中心：表單化編輯 settings.yaml，免手改設定檔。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src import config_io  # noqa: E402

st.set_page_config(page_title="設定中心", page_icon="⚙️", layout="wide")
st.title("⚙️ 設定中心")
st.caption("修改後按「儲存」寫回 config/settings.yaml，立即生效。")

data = config_io.load_raw()

tab_cap, tab_risk, tab_screen, tab_llm, tab_data = st.tabs(
    ["💰 資金", "🛡️ 風險", "🔍 選股因子", "🤖 LLM", "🗄️ 資料/回補"]
)

with tab_cap:
    cap = data.get("capital", {})
    total = st.number_input("總資金 (TWD)", min_value=0, value=int(cap.get("total", 1_000_000)), step=100_000)
    if st.button("儲存資金設定", key="save_cap"):
        config_io.update_section("capital", {"total": int(total)})
        st.success("已儲存")

with tab_risk:
    r = data.get("risk", {})
    c1, c2 = st.columns(2)
    with c1:
        per_trade = st.number_input("單筆最大風險 (% 總資金)", 0.1, 10.0, float(r.get("per_trade_risk_pct", 1.0)), 0.1)
        max_pos = st.number_input("單一持股上限 (%)", 1.0, 100.0, float(r.get("max_single_position_pct", 15.0)), 1.0)
        rr = st.number_input("R:R 下限（報酬/風險比）", 1.0, 5.0, float(r.get("min_reward_risk_ratio", 1.5)), 0.1)
    with c2:
        cooldown = st.number_input("停損後冷卻天數", 0, 60, int(r.get("cooldown_days", 5)))
        halt = st.number_input("回撤熔斷門檻 (%)", 1.0, 50.0, float(r.get("max_drawdown_halt_pct", 15.0)), 1.0)
    if st.button("儲存風險設定", key="save_risk"):
        config_io.update_section("risk", {
            "per_trade_risk_pct": float(per_trade),
            "max_single_position_pct": float(max_pos),
            "min_reward_risk_ratio": float(rr),
            "cooldown_days": int(cooldown),
            "max_drawdown_halt_pct": float(halt),
        })
        st.success("已儲存")

with tab_screen:
    sc = data.get("screener", {})
    c1, c2 = st.columns(2)
    with c1:
        top_n = st.number_input("選出檔數 Top N", 5, 100, int(sc.get("top_n", 30)))
        min_turnover = st.number_input("20 日均成交額下限 (元)", 0, value=int(sc.get("min_avg_turnover", 20_000_000)), step=1_000_000)
    with c2:
        chips_lb = st.number_input("籌碼回看天數", 5, 60, int(sc.get("chips_lookback", 20)))
    st.markdown("**因子權重**（越大越重要）")
    weights = sc.get("weights", {})
    new_w = {}
    wc = st.columns(len(weights) or 1)
    for i, (k, v) in enumerate(weights.items()):
        with wc[i % len(wc)]:
            new_w[k] = st.number_input(k, -3.0, 3.0, float(v), 0.1, key=f"w_{k}")
    if st.button("儲存選股設定", key="save_screen"):
        config_io.update_section("screener", {
            "top_n": int(top_n),
            "min_avg_turnover": int(min_turnover),
            "chips_lookback": int(chips_lb),
            "weights": new_w,
        })
        st.success("已儲存")

with tab_llm:
    import os
    llm = data.get("llm", {})
    analyst = st.text_input("分析師模型", llm.get("analyst_model", "claude-sonnet-4-6"))
    trader = st.text_input("交易員模型", llm.get("trader_model", "claude-opus-4-8"))
    reflect = st.text_input("反思模型", llm.get("reflection_model", "claude-opus-4-8"))
    if st.button("儲存 LLM 設定", key="save_llm"):
        config_io.update_section("llm", {
            "analyst_model": analyst, "trader_model": trader, "reflection_model": reflect,
        })
        st.success("已儲存")

    st.divider()
    st.markdown("**Anthropic API Key**（Phase 2 分析師/交易員 Agent 需要）")
    key_set = bool(os.getenv("ANTHROPIC_API_KEY"))
    st.metric("目前狀態", "已設定 ✅" if key_set else "未設定 ⚠️")
    st.caption("取得：https://console.anthropic.com/ 。填入後寫進 .env 並立即生效。")
    new_key = st.text_input("貼上 Anthropic API Key", type="password", placeholder="留空則不變更")
    if st.button("儲存 API Key", key="save_anthropic"):
        if new_key.strip():
            config_io.set_env_var("ANTHROPIC_API_KEY", new_key.strip())
            st.success("已寫入 .env 並生效")
            st.rerun()
        else:
            st.warning("未輸入")

with tab_data:
    import os
    d = data.get("data", {})
    start = st.text_input("歷史回補起始日", d.get("backfill_start", "2020-01-01"))
    if st.button("儲存回補起始日", key="save_data"):
        d["backfill_start"] = start
        config_io.update_section("data", d)
        st.success("已儲存")

    st.divider()
    st.markdown("**FinMind API Token**")
    token_set = bool(os.getenv("FINMIND_TOKEN"))
    st.metric("目前狀態", "已設定 ✅" if token_set else "未設定（用匿名額度，約 70 檔即用罄）⚠️")
    st.caption("免費註冊取得：https://finmindtrade.com/ 。填入後寫進 .env 並立即生效。")
    new_token = st.text_input("貼上 FinMind Token", type="password", placeholder="留空則不變更")
    if st.button("儲存 Token", key="save_token"):
        if new_token.strip():
            config_io.set_env_var("FINMIND_TOKEN", new_token.strip())
            st.success("Token 已寫入 .env 並生效")
            st.rerun()
        else:
            st.warning("未輸入 Token")

with st.expander("查看目前完整 settings.yaml"):
    st.code(Path(config_io.DEFAULT_SETTINGS_PATH).read_text(encoding="utf-8"), language="yaml")
