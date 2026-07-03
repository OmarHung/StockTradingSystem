"""資料狀態：初始化資料庫、覆蓋概況、背景回補與即時進度。

涵蓋所有資料相關 CLI：init_db、backfill（全市場/指定/限檔數/force）。
長任務以背景 job 執行（src.jobs），不阻塞 UI，可即時看進度、可中止。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

import streamlit as st  # noqa: E402

from src import jobs  # noqa: E402
from src.data import database as db  # noqa: E402
from src.data import query as q  # noqa: E402

st.set_page_config(page_title="資料狀態", page_icon="📦", layout="wide")
st.title("📦 資料狀態")

JOB = "backfill"

# ---- 資料庫初始化 ----
with st.container(border=True):
    st.subheader("① 資料庫")
    c1, c2 = st.columns([1, 4])
    with c1:
        if st.button("初始化資料庫", help="建立所有資料表（等同 scripts.init_db，可重複執行）"):
            from src.config import get_settings
            db.init_db(get_settings().db_path)
            st.success("資料庫已初始化")
    with c2:
        try:
            n = len(q.all_stock_ids())
            st.metric("已入庫股票數", n)
        except Exception:  # noqa: BLE001
            st.warning("資料庫尚未初始化或無資料，請先按左側「初始化資料庫」。")

# ---- 覆蓋概況 ----
with st.container(border=True):
    st.subheader("② 各資料表覆蓋概況")
    try:
        st.dataframe(q.data_status(), use_container_width=True, hide_index=True)
    except Exception as e:  # noqa: BLE001
        st.error(f"讀取失敗：{e}")

# ---- 回補 ----
with st.container(border=True):
    st.subheader("③ 歷史回補")
    token_set = bool(os.getenv("FINMIND_TOKEN"))
    if not token_set:
        st.warning("未設定 FINMIND_TOKEN，免費匿名額度約 70 檔即用罄。請到「⚙️ 設定中心」填入 token。", icon="⚠️")

    mode = st.radio("回補範圍", ["全市場", "指定股票", "限制檔數（測試）"], horizontal=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        start = st.text_input("起始日", "2020-01-01")
    stocks_arg, limit_arg = None, None
    if mode == "指定股票":
        with c2:
            stocks_arg = st.text_input("股票代號（空格分隔）", "2330 2317 0050")
    elif mode == "限制檔數（測試）":
        with c2:
            limit_arg = st.number_input("檔數上限", 1, 3000, 50)
    with c3:
        force = st.checkbox("強制全期重抓（回填缺口）", value=False,
                            help="忽略 fetch_log，全期重抓。用於補回 last_date 之前的缺口。")

    running = jobs.is_running(JOB)
    b1, b2, b3 = st.columns([1, 1, 4])
    with b1:
        if st.button("開始回補", type="primary", disabled=running):
            args = ["scripts.backfill", "--start", start]
            if stocks_arg and stocks_arg.strip():
                args += ["--stocks", *stocks_arg.split()]
            if limit_arg:
                args += ["--limit", str(int(limit_arg))]
            if force:
                args += ["--force"]
            if jobs.start_job(JOB, args):
                st.success("回補已於背景啟動")
                st.rerun()
    with b2:
        if st.button("中止回補", disabled=not running):
            jobs.stop_job(JOB)
            st.warning("已送出中止")
            st.rerun()

    # 進度顯示
    status = "🟢 執行中" if jobs.is_running(JOB) else "⚪ 閒置 / 已完成"
    st.caption(f"回補狀態：{status}")
    log = jobs.read_log(JOB, tail=25)
    if log:
        st.code(log)
    if jobs.is_running(JOB):
        st.button("🔄 重新整理進度")  # 按下即觸發 rerun 讀取最新 log
        st.caption("回補進行中——按上方「重新整理進度」更新畫面。")
