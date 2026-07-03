"""選股報告：對量化初篩出的候選股，跑 LLM 分析師團隊 + 交易員決策，產出報告。"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.agents import pipeline  # noqa: E402
from src.llm import client as llm  # noqa: E402
from src.screener.screener import run_screener  # noqa: E402

st.set_page_config(page_title="選股報告", page_icon="📋", layout="wide")
st.title("📋 智慧選股報告")
st.caption("量化初篩 → LLM 分析師團隊（技術/籌碼/基本面）→ 驗證層 → 交易員決策。")

if not llm.has_api_key():
    st.error("尚未設定 ANTHROPIC_API_KEY，請到「⚙️ 設定中心 → 🤖 LLM」填入後再使用。", icon="🔑")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    as_of = st.date_input("基準日", value=dt.date(2025, 6, 30))
with c2:
    n = st.number_input("分析檔數（取初篩前 N）", 1, 15, 3,
                        help="每檔約需 4 次 LLM 呼叫，檔數越多越花時間與成本。")

col_run, col_load = st.columns(2)
run = col_run.button("🚀 跑量化初篩 + LLM 分析", type="primary")
load = col_load.button("📂 載入上次結果")

if run:
    with st.spinner("量化初篩中…"):
        ranked = run_screener(as_of.isoformat())
    if ranked.empty:
        st.warning("該日無候選股。")
        st.stop()
    picks = ranked["stock_id"].head(int(n)).tolist()
    st.info(f"初篩候選：{', '.join(picks)}，開始 LLM 分析（每檔 3 分析師 + 驗證 + 交易員）…")
    prog = st.progress(0.0)
    records = []
    for i, sid in enumerate(picks):
        with st.spinner(f"分析 {sid}（{i+1}/{len(picks)}）…"):
            rec = pipeline.analyze_stock(sid, as_of.isoformat())
            if rec:
                records.append(rec)
        prog.progress((i + 1) / len(picks))
    st.session_state["records"] = records
    st.success(f"完成，共 {len(records)} 檔。")

if load:
    st.session_state["records"] = pipeline.load_plans(as_of.isoformat())

records = st.session_state.get("records", [])
if records:
    # 依 action_score 排序
    records = sorted(records, key=lambda r: r["plan"]["action_score"], reverse=True)
    st.divider()
    for rec in records:
        p = rec["plan"]
        sid = rec["stock_id"]
        badge = {"buy": "🟢 買進", "hold": "🟡 觀望", "avoid": "🔴 避開"}.get(p["action"], p["action"])
        with st.expander(f"{badge}　{sid}　｜　動作分 {p['action_score']:+.2f}　信心 {p['confidence']:.0%}", expanded=(p["action"] == "buy")):
            cols = st.columns(4)
            cols[0].metric("進場區間", f"{p.get('entry_low','—')} ~ {p.get('entry_high','—')}")
            cols[1].metric("停損", p.get("stop_loss") or "—")
            cols[2].metric("目標價", p.get("target_price") or "—")
            cols[3].metric("報酬風險比", f"{p['reward_risk']:.2f}" if p.get("reward_risk") else "—")
            st.markdown(f"**交易員理由**：{p['rationale']}")
            if p.get("risks"):
                st.markdown("**風險提示**：" + "；".join(p["risks"]))

            st.markdown("---\n**分析師報告**")
            for name, entry in rec.get("analysts", {}).items():
                r = entry["report"]
                label = {"technical": "技術面", "chips": "籌碼面", "fundamental": "基本面"}.get(name, name)
                flag = " ⚠️被驗證層攔截" if entry.get("validation_flags") else ""
                st.markdown(
                    f"- **{label}**（{r['signal']}, 分數 {r['score']:+.2f}, "
                    f"調整後信心 {entry['adjusted_confidence']:.0%}{flag}）：{r['summary']}"
                )
                if entry.get("validation_flags"):
                    st.caption("　攔截：" + "；".join(entry["validation_flags"]))
else:
    st.info("按「跑量化初篩 + LLM 分析」開始，或載入上次結果。")
