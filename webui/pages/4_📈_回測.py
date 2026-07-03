"""回測：選策略/期間/參數，一鍵回測，看權益曲線與績效指標。"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from src.backtest.runner import run_backtest  # noqa: E402

st.set_page_config(page_title="回測", page_icon="📈", layout="wide")
st.title("📈 策略回測")
st.caption("事件驅動日線回測，含台股成本模型（手續費 0.1425% + 證交稅 0.3%），隔日開盤成交、無前視偏差。")

STRATS = {
    "多因子選股（月調倉）": "screener",
    "買進持有 0050（基準）": "buy_and_hold",
    "0050 均線策略（基準）": "ma_cross",
}

c1, c2, c3, c4 = st.columns(4)
with c1:
    strat_label = st.selectbox("策略", list(STRATS.keys()))
with c2:
    start = st.date_input("起始日", value=dt.date(2022, 6, 1))
with c3:
    end = st.date_input("結束日", value=dt.date(2025, 6, 30))
with c4:
    cash = st.number_input("初始資金", min_value=100_000, value=1_000_000, step=100_000)

max_pos = 10
if STRATS[strat_label] == "screener":
    max_pos = st.slider("同時持有檔數", 3, 30, 10)

if st.button("執行回測", type="primary"):
    with st.spinner("回測中…"):
        try:
            res, m = run_backtest(
                STRATS[strat_label], start.isoformat(), end.isoformat(),
                initial_cash=cash, max_positions=max_pos,
            )
            st.session_state["bt_res"] = res
            st.session_state["bt_m"] = m
        except Exception as e:  # noqa: BLE001
            st.error(f"回測失敗：{e}")

res = st.session_state.get("bt_res")
m = st.session_state.get("bt_m")
if res is not None:
    st.subheader("績效指標")
    cols = st.columns(6)
    cols[0].metric("總報酬", f"{m['total_return']:.2%}")
    cols[1].metric("年化 (CAGR)", f"{m['cagr']:.2%}")
    cols[2].metric("Sharpe", f"{m['sharpe']:.2f}")
    cols[3].metric("最大回撤", f"{m['max_drawdown']:.2%}")
    cols[4].metric("年化波動", f"{m['annual_vol']:.2%}")
    wr = m.get("win_rate")
    cols[5].metric("勝率", f"{wr:.1%}" if wr is not None else "—")

    st.subheader("權益曲線")
    ec = res.equity_curve
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(ec.index), y=list(ec.values), name="策略權益", line=dict(color="#2196f3")))
    fig.add_hline(y=res.initial_cash, line_dash="dash", line_color="gray")
    fig.update_layout(height=400, margin=dict(t=20, b=20), yaxis_title="總權益 (TWD)")
    st.plotly_chart(fig, use_container_width=True)

    with st.expander(f"逐筆成交紀錄（{m['n_trades']} 筆）"):
        st.dataframe(res.trades, use_container_width=True, hide_index=True, height=300)
else:
    st.info("設定參數後按「執行回測」。")
