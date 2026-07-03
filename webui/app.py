"""StockTradingSystem WebUI 入口（Streamlit 多頁應用）。

啟動：
    .venv/bin/streamlit run webui/app.py

Phase 1 頁面：設定中心 / 資料狀態 / 智慧選股 / 回測。
後續階段將於 pages/ 增加：選股報告、大腦活動、持倉績效、反思規則庫。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 讓 pages/ 內能 import src.*
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="台股智慧交易系統", page_icon="📈", layout="wide")

st.title("📈 台股智慧交易系統")
st.caption("架構融合 CryptoTrade（多 LLM Agent + Reflection）與 LLM_trader（向量記憶 + 輸出驗證 + 視覺分析）")

st.markdown(
    """
    ### 歡迎使用
    請由左側選單進入各功能頁面：

    | 頁面 | 功能 | 階段 |
    |---|---|---|
    | ⚙️ **設定中心** | 資金、風險、選股因子、LLM 模型——表單化編輯，免改設定檔 | Phase 1 |
    | 📦 **資料狀態** | 各資料源覆蓋概況、缺漏檢查、手動更新/回補 | Phase 1 |
    | 🔍 **智慧選股** | 任選日期跑多因子選股，看 Top N 排名與因子拆解 | Phase 1 |
    | 📈 **回測** | 選策略/期間，一鍵回測，看權益曲線與績效指標 | Phase 1 |

    後續階段將加入：📋 選股報告（LLM）、🧠 大腦活動、💰 持倉績效、📚 反思規則庫。
    """
)

st.info("首次使用請先到 **⚙️ 設定中心** 確認參數，並到 **📦 資料狀態** 確認資料已回補。", icon="💡")
