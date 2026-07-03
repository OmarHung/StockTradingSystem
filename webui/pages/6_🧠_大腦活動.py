"""大腦活動：檢視各 Agent 的 prompt/回應與驗證層攔截記錄（可觀測性）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.data import query as q  # noqa: E402

st.set_page_config(page_title="大腦活動", page_icon="🧠", layout="wide")
st.title("🧠 大腦活動")
st.caption("每次 LLM 呼叫與驗證層攔截的完整記錄，讓 AI 決策可追溯、可稽核。")

c1, c2 = st.columns([1, 1])
with c1:
    limit = st.number_input("顯示筆數", 10, 500, 100)
with c2:
    only_flags = st.checkbox("只看驗證層攔截", value=False)

df = q.brain_log(limit=int(limit))
if df.empty:
    st.info("尚無記錄。到「📋 選股報告」跑一次分析後即可在此檢視。")
    st.stop()

if only_flags:
    df = df[df["note"].notna()]

st.caption(f"共 {len(df)} 筆")
for _, row in df.iterrows():
    if row["note"]:  # 驗證層攔截等事件
        st.warning(f"🛡️ [{row['ts']}] {row['agent']}｜{row['stock_id'] or ''}　{row['note']}")
        continue
    icon = {"technical": "📊", "chips": "💰", "fundamental": "📈", "trader": "🧑‍💼"}.get(row["agent"], "🤖")
    with st.expander(f"{icon} [{row['ts']}] {row['agent']}｜{row['stock_id'] or ''}｜{row['model'] or ''}"):
        if row["prompt"]:
            st.markdown("**Prompt**")
            st.code(row["prompt"])
        if row["response"]:
            st.markdown("**回應（結構化）**")
            st.code(row["response"], language="json")
