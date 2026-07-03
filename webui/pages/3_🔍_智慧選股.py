"""智慧選股：任選日期跑多因子選股，看 Top N 排名、因子拆解與個股 K 線。"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from src import indicators as ind  # noqa: E402
from src.data import query as q  # noqa: E402
from src.screener.screener import run_screener  # noqa: E402

st.set_page_config(page_title="智慧選股", page_icon="🔍", layout="wide")
st.title("🔍 智慧選股")
st.caption("量化多因子初篩（Phase 1）。Phase 2 將接上 LLM 分析師團隊做精選與報告。")

c1, c2 = st.columns([1, 3])
with c1:
    as_of = st.date_input("選股基準日", value=dt.date(2025, 6, 30))
    run = st.button("執行選股", type="primary")

if run:
    with st.spinner("計算因子與排名中…"):
        ranked = run_screener(as_of.isoformat())
    if ranked.empty:
        st.warning("該日無股票通過流動性/因子條件（可能是資料不足或非交易日）。")
    else:
        st.session_state["ranked"] = ranked

ranked = st.session_state.get("ranked")
if ranked is not None and not ranked.empty:
    show_cols = ["rank", "stock_id", "stock_name", "industry_category", "score",
                 "momentum_20", "momentum_60", "chips_net_buy", "revenue_yoy", "above_ma60"]
    show_cols = [c for c in show_cols if c in ranked.columns]
    st.subheader(f"選股結果（Top {len(ranked)}）")
    st.dataframe(
        ranked[show_cols].style.format({
            "score": "{:.3f}", "momentum_20": "{:.2%}", "momentum_60": "{:.2%}",
            "revenue_yoy": "{:.2%}", "chips_net_buy": "{:,.0f}",
        }, na_rep="—"),
        use_container_width=True, hide_index=True, height=400,
    )

    st.divider()
    st.subheader("個股 K 線與均線")
    pick = st.selectbox("選擇個股", ranked["stock_id"] + " " + ranked["stock_name"].fillna(""))
    sid = pick.split()[0]
    px = q.get_price(sid)
    if not px.empty:
        px = ind.add_indicators(px).tail(120)
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=px["date"], open=px["open"], high=px["high"], low=px["low"], close=px["close"], name="K線"))
        for ma, color in [("ma5", "#ff9800"), ("ma20", "#2196f3"), ("ma60", "#9c27b0")]:
            fig.add_trace(go.Scatter(x=px["date"], y=px[ma], name=ma.upper(), line=dict(width=1, color=color)))
        fig.update_layout(height=450, xaxis_rangeslider_visible=False, margin=dict(t=20, b=20))
        st.plotly_chart(fig, use_container_width=True)
else:
    st.info("設定基準日後按「執行選股」。")
