# StockTradingSystem

台股全自動交易系統，架構融合 [CryptoTrade](https://github.com/Xtra-Computing/CryptoTrade)（多 LLM Agent + Reflection）與 [LLM_trader](https://github.com/qrak/LLM_trader)（向量記憶、輸出驗證、視覺分析）。完整規劃見 [`PLAN.md`](PLAN.md)。

## 進度

- [x] **Phase 0 — 資料層地基**（完成）
- [x] **Phase 1 — 量化選股 + 回測環境 + WebUI v1**（完成）
- [x] **Phase 2 — LLM 分析師 + 交易員 Agent + 驗證層**（核心完成，端到端需 API key）
- [ ] Phase 3 — 資金控管 + Guard pipeline 風控
- [ ] Phase 4 — Reflection + 向量記憶
- [ ] Phase 5 — 模擬交易上線 + 報表 + WebUI
- [ ] Phase 6 — 實盤

## 安裝

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # 選填 FINMIND_TOKEN 以提高 API 額度
```

## Phase 0 使用方式

```bash
# 1) 建立資料庫
.venv/bin/python -m scripts.init_db

# 2) 回補歷史資料
.venv/bin/python -m scripts.backfill --stocks 2330 2317 --start 2024-01-01   # 指定股票測試
.venv/bin/python -m scripts.backfill --limit 50                              # 前 50 檔冒煙測試
.venv/bin/python -m scripts.backfill                                         # 全市場（依 settings.yaml 起始日）

# 3) 查詢
.venv/bin/python -c "from src.data import query as q; print(q.data_status())"

# 測試
.venv/bin/python -m pytest tests/ -q
```

回補具**增量與冪等性**：重跑只補新資料，不重抓已有資料（依 `fetch_log` 判斷）。
回填缺口用 `--force`（忽略 fetch_log 全期重抓）。

> ⚠️ FinMind 免費**匿名**額度約 70 檔即用罄（HTTP 402）。要回補全市場，請到
> [finmindtrade.com](https://finmindtrade.com/) 免費註冊拿 token 填入 `.env` 的 `FINMIND_TOKEN`。

## Phase 1 使用方式

```bash
.venv/bin/pip install -r requirements.txt          # 補裝 streamlit/plotly/ruamel.yaml

# 啟動 WebUI（設定中心 / 資料狀態 / 智慧選股 / 回測）
.venv/bin/streamlit run webui/app.py

# 或用 CLI 跑回測
.venv/bin/python -m scripts.run_backtest --strategy screener --start 2022-06-01 --end 2025-06-30
```

## 資料來源

FinMind API v4（免 token 可用，有額度限制）。抓取：股票清單、日 K、三大法人買賣超、融資融券、月營收。
設定集中於 [`config/settings.yaml`](config/settings.yaml)。
