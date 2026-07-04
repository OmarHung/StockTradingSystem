# StockTradingSystem — 台股 AI 全自動交易系統

「持續檢討、學習、優化」的台股全自動交易系統。架構融合
[CryptoTrade](https://github.com/Xtra-Computing/CryptoTrade)（多 LLM 分析師 + 交易員 Agent + Reflection）
與 [LLM_trader](https://github.com/qrak/LLM_trader)（向量記憶、輸出驗證層、Guard 風控）。
完整規劃與缺口清單見 [`PLAN.md`](PLAN.md)。

## 系統能力（Phase 0–5 已完成）

```
資料層 ── shioaji股價(主) + TWSE/TPEx官方籌碼/除權息/估值(主) + FinMind(純備援+402自動等待)
   ↓        還原價、查詢時清洗、品質檢查、最新優先回補、背景排程
選股 ──── 多因子量化初篩(動能/籌碼/營收/季線，橫斷面z-score，無前視)
   ↓
決策 ──── LLM 分析師×3(技術/籌碼/基本面) → 驗證層(引用數字比對實算，防幻覺)
   ↓      → 交易員 Agent(進場區間/停損/目標/R:R) ← 向量記憶注入(相似經驗+反思規則)
風控 ──── Guard pipeline 九閘(處置股/熔斷/R:R≥1.5/冷卻/風險部位/單股/產業/現金)
   ↓      LLM 不可逾越；駁回寫 friction log
執行 ──── PaperBroker 模擬帳本(隔日限價/停損停利/費稅損益) + 每日主流程 + 內建排程
   ↓
學習 ──── 成果評估器(計畫 vs 後續真實價格) → ChromaDB 三集合(經驗/規則/被擋)
          → 週反思(LLM 歸納規則/反模式) → 注入下次決策  ⟲ 閉環
```

## 快速開始

```bash
# 安裝（⚠️ Apple Silicon 一律用 arch -arm64，否則裝成 x86_64 會壞）
python3 -m venv .venv
arch -arm64 .venv/bin/pip install -r requirements.txt
cd frontend && npm install && cd ..

# 啟動交易終端（FastAPI :8000 + React :5173）
bash scripts/dev.sh
```

開 http://localhost:5173，依序：
1. **⚙️ 設定 → 🔑 API 金鑰**：填 Anthropic（LLM 必需）、永豐 shioaji（股價主源+排行）、FinMind（指數價格+備援，可後補）
2. **📦 資料**：按「回補」（全市場、額度自動等待預設開，可過夜）
3. 主畫面即可跑 🔍 選股、🧑‍💼 AI 報告、🧪 回測、💰 模擬交易

## 介面總覽（React 交易終端）

| 面板/視窗 | 功能 |
|---|---|
| ⭐ 自選清單 / 🏆 排行榜 | 報價與漲跌幅/成交值排行（shioaji），點選切主圖 |
| 📈 K 線圖 | 還原↔原始價切換、日/週/月切換（lightweight-charts） |
| 🔍 智慧選股 | 多因子初篩 Top30 + 因子拆解，結果落庫可回看 |
| 🧑‍💼 AI 選股報告 | 分析師團隊→驗證→交易員→Guard 核准部位/駁回原因 |
| 🧪 策略回測 | 含風控版（ATR停損+冷卻+大盤濾網），權益曲線+逐筆明細 |
| 💰 持倉績效 | 權益曲線 vs 大盤、持倉/成交、▶每日流程、🛑緊急停止、♻️重置 |
| 🧠 大腦活動 / 📚 反思規則庫 | LLM 呼叫全紀錄+驗證攔截；規則/反模式管理、一鍵反思 |
| 🗂 股票（TopBar） | 全市場瀏覽器：開高低收/漲跌幅列表、當月除權息標示、產業/市場/狀態篩選；點股票開整頁詳情（總覽/籌碼/基本面/除權息/資料覆蓋 分頁＋走勢/法人/融資/營收/PER 圖表） |
| 📦 資料（TopBar） | 健康報告(覆蓋率/新鮮度/建議)、分類型回補（股價/法人/融資券/月營收/除權息/估值）+逐日進度、品質檢查 |
| ⚙️ 設定（TopBar） | 資金/風險/因子權重/LLM模型/⏰排程監控與調整/金鑰/券商模擬↔正式切換 |

（`webui/` 內有早期 Streamlit 版，功能已被 React 終端全面取代，僅留參考。）

## 無人值守排程（後端內建，⚙️ 設定 → ⏰ 排程）

排程器住在 FastAPI 行程內（asyncio 迴圈），毋須 launchd/cron：
- **資料增量更新** 平日 14:30、**每日交易主流程** 平日 15:00（時間/啟停皆可在 WebUI 調整，立即生效）
- 實際工作走獨立子行程（src/jobs.py）——API 重啟不中斷進行中任務；API 晚於排定時間啟動會當天自動補跑
- WebUI 可監控：執行中狀態、上次/下次執行、log 尾巴、▶ 立即執行
- ⚠️ 前提：`bash scripts/dev.sh`（或後端 uvicorn）需常駐執行

每日流程：開盤撮合昨日委託 → 停損停利 → 權益快照 → (週五)反思 → LLM 決策 → Guard → 掛明日單。
緊急停止後只做保護性出場、不開新倉。

## CLI（所有操作 WebUI 皆可執行，CLI 供排程/除錯）

```bash
.venv/bin/python -m scripts.backfill --auto-wait          # 全市場資料回補
.venv/bin/python -m scripts.run_daily                     # 每日交易主流程
.venv/bin/python -m scripts.run_backtest --strategy screener_risk
.venv/bin/python -m pytest tests/ -q                      # 44 tests
```

## 專案結構

```
api/main.py            FastAPI 後端（React 終端的 REST API）
frontend/              React 19 + Vite + lightweight-charts + react-grid-layout
src/
  data/                資料層：shioaji/TWSE官方/FinMind 三源、還原價、品質檢查
  screener/            多因子選股
  agents/              LLM 分析師/驗證層/交易員/決策管線
  risk/                Guard pipeline 九閘 + 風險部位
  memory/              ChromaDB 向量記憶、成果評估、反思引擎
  broker/              PaperBroker 模擬帳本（Phase 6 加 LiveBroker）
  pipeline/daily.py    每日主流程
  backtest/ env/       回測引擎（TWEnv、台股成本模型）
  report/              績效統計
scripts/               CLI 腳本（backfill/run_daily/run_backtest）
config/settings.yaml   全部參數（WebUI 設定頁讀寫，金鑰在 .env）
```

## 金鑰取得

| 金鑰 | 用途 | 取得 |
|---|---|---|
| `ANTHROPIC_API_KEY` | LLM 分析/決策/反思 | console.anthropic.com |
| `SJ_API_KEY` / `SJ_SEC_KEY` | 股價主源、排行、Phase 5+ 交易 | sinotrade.com.tw → Python API 管理頁 |
| `FINMIND_TOKEN` | 指數價格 + 全面備援 | finmindtrade.com 免費註冊 |

全部可在 WebUI ⚙️ 設定填寫（寫入 `.env`，不入版控）。
