# StockTradingSystem 規劃書

融合兩個開源專案的理念，打造「持續檢討、學習、優化」的台股全自動交易系統：

- [CryptoTrade](https://github.com/Xtra-Computing/CryptoTrade)：多 LLM 分析師 + 交易員 Agent + 每日 Reflection 的整體架構
- [LLM_trader](https://github.com/qrak/LLM_trader)：向量記憶庫、LLM 輸出驗證層（falsification）、視覺化 K 線分析、規則/反模式合成、Guard pipeline 風控

兩者分工：**CryptoTrade 給骨架（多 Agent 決策流程），LLM_trader 給神經系統（記憶、驗證、自我修正的工程細節）**。

---

## 一、CryptoTrade 核心理念 → 台股對應

CryptoTrade 的架構是：多個 LLM 分析師各自產出報告 → 交易員 Agent 綜合報告輸出動作分數 ∈ [-1, 1] → 每日 Reflection Agent 回顧近期績效、修正策略傾向（保守 ↔ 積極）。

| CryptoTrade 元件 | 台股版對應 |
|---|---|
| On-Chain Analyst（鏈上數據） | **籌碼分析師**：三大法人買賣超、融資融券、借券、大戶持股比、主力券商分點 |
| News Analyst | **新聞/事件分析師**：財經新聞、重大訊息（MOPS）、月營收、財報、除權息 |
| Technical（MACD 等） | **技術分析師**：K 線、均線、MACD、KD、RSI、量價、支撐壓力 |
| （新增） | **基本面分析師**：EPS、營收 YoY、毛利率、本益比、殖利率 |
| Trading Agent | **交易員 Agent**：綜合各分析師報告，對每檔標的輸出 action score ∈ [-1, 1] |
| Reflection Analyst | **反思 Agent**：每日/每週回顧交易結果，歸納有效與無效訊號，產出策略調整建議，寫入經驗記憶庫供後續決策引用 |
| eth_env.py 交易環境 | **TWEnv**：台股交易環境（回測 / 模擬 / 實盤同一介面），內含手續費 0.1425%、證交稅 0.3%、漲跌幅 10%、整股/零股、T+2 交割 |

### LLM_trader 補強的關鍵機制

| LLM_trader 機制 | 台股版採用方式 |
|---|---|
| **向量記憶庫**（ChromaDB，三個 collection：交易經驗 / 語意規則 / 被擋交易） | 每筆平倉交易存入「情境+決策+結果」；反思引擎定期合成「有效規則」與「反模式」；決策時以語意相似度檢索近似歷史情境注入 prompt |
| **驗證層（falsification）**：LLM 的宣稱逐項與計算指標交叉比對 | 分析師說「趨勢強勁」→ 用實算 ADX 驗證；說「爆量」→ 與實際量能百分位比對；宣稱的籌碼面必須與資料庫實際數字一致，不符則駁回或降信心 |
| **視覺分析**：產生 K 線圖 PNG 給多模態 LLM 看圖 | 技術分析師採「指標數據 + K 線圖」雙模輸入，用視覺能力辨識型態，取代硬編碼型態邏輯 |
| **Guard pipeline**：白名單、冷卻期、friction tracking | 風控閘門：停損後同標的 N 日冷卻、被駁回交易記錄原因（friction log）供反思分析、標的白名單/黑名單 |
| **R:R 下限** | 交易計畫的（目標價−進場價)/(進場價−停損價）< 1.5 一律不進場 |
| **Provider fallback chain** | LLM 呼叫失敗自動降級備援模型，排程不因 API 故障中斷 |
| **RAG 新聞相關性評分** | 新聞先經 embedding 相關性評分過濾，只把高相關新聞餵給新聞分析師 |

### 台股特有差異（設計時必須處理）

- 交易時段 09:00–13:30（+ 盤後零股/定價），非 24h → 決策節奏以「日」為主，盤中僅做出場監控
- 漲跌幅 ±10%、處置股/警示股、全額交割股需排除
- 交易成本高於幣圈，頻繁進出會被成本吃掉 → 以波段（數日～數週）為主

## 二、系統架構

```
┌────────────────────────── Scheduler（後端內建排程）────────────────────────┐
│                                                                          │
│  [1] Data Layer 資料層                                                    │
│      股價（shioaji 主源、FinMind 指數+備援）                                 │
│      籌碼/除權息+預告/估值（TWSE/TPEx 官方主源）、月營收（MOPS 主源）            │
│      → SQLite 本地資料庫，每日盤後增量更新                                    │
│                                                                          │
│  [2] Screener 智慧選股（量化初篩 → LLM 精選）                                │
│      量化漏斗：流動性/非處置股 → 動能、籌碼、營收多因子評分 → Top 30             │
│      LLM 分析師團隊對候選逐檔產出報告 → 精選候選 + 選股報告                     │
│                                                                          │
│  [3] Decision 決策層                                                      │
│      交易員 Agent：候選報告 + 持倉狀態 + 向量記憶檢索出的                       │
│      相似歷史情境/規則 → 每檔 action score [-1,1]                            │
│      + 進出場計畫（進場價區間、停損價、目標價、加減碼條件）                       │
│      ↓ Validation 驗證層：LLM 宣稱逐項對照實算指標（ADX/量能/籌碼數字），        │
│        不符則駁回或降信心，杜絕幻覺決策                                        │
│                                                                          │
│  [4] Risk & Money Management（純規則 Guard pipeline，LLM 不可逾越）          │
│      單筆風險 ≤ 總資金 1%、單一持股 ≤ 15%、總持倉上限、同產業曝險上限            │
│      R:R ≥ 1.5 才進場、停損後同標的冷卻 N 日、白名單/黑名單                     │
│      強制停損、移動停利、最大回撤熔斷（DD > X% 全面降倉/停機）                   │
│      被駁回交易記錄原因（friction log）→ 回饋給反思層                          │
│                                                                          │
│  [5] Execution 執行層（統一 Broker 介面）                                    │
│      BacktestBroker → PaperBroker（模擬帳本）→ LiveBroker（shioaji 實盤）     │
│                                                                          │
│  [6] Reflection + Memory 反思學習層（ChromaDB 向量記憶庫）                    │
│      collection A 交易經驗：每筆平倉的 情境+決策+理由+結果                     │
│      collection B 語意規則：反思合成的規則與反模式                             │
│      collection C 被擋交易：friction log（若當初成交會如何？）                 │
│      決策時語意檢索相似情境與規則注入 prompt → 持續檢討→學習→優化 閉環            │
│                                                                          │
│  [7] Reporting 報表/展示層                                                 │
│      React 交易終端（FastAPI 後端）：K 線圖/選股/AI 報告/回測/持倉績效/           │
│      大腦活動/反思規則庫/資料管理/設定中心/排程監控/緊急停止                      │
└──────────────────────────────────────────────────────────────────────────┘
```

## 三、技術選型

| 項目 | 選擇 | 說明 |
|---|---|---|
| 語言 | Python 3.11+ | |
| 資料庫 | SQLite（+ Parquet/DuckDB 預留給 tick 級） | 單機即可，免運維 |
| 行情/籌碼/基本面 | **shioaji**（股價主源）、**TWSE/TPEx 官方**（籌碼/除權息/估值主源）、**MOPS**（月營收主源）、FinMind（指數價格 + 全面備援，額度用罄自動等待） | 官方源免額度、按日全市場 |
| 券商 API | **shioaji**（永豐金證券） | 台股最成熟 Python API，有模擬環境，紙上交易→實盤無縫切換 |
| LLM | Claude API（分析師用 Sonnet、交易員/反思用進階模型），失敗自動 fallback | 結構化輸出 JSON |
| 向量記憶 | ChromaDB（本地） | 交易經驗/語意規則/被擋交易 三個 collection |
| 回測 | 自建 TWEnv（gym 式介面） | 事件驅動日線回測，含台股成本模型 |
| WebUI | React 19 + Vite + lightweight-charts / FastAPI 後端 | 專業交易終端；所有 CLI 操作皆可在 WebUI 執行，長任務走 jobs 背景機制 |
| 排程 | 後端內建排程器（asyncio） | 實際工作走獨立子行程，API 重啟不中斷；晚啟動當天自動補跑；WebUI 可監控與調整 |
| 通知 | Telegram Bot | 每日決策報告推播（未設定金鑰則自動跳過，失敗不影響交易） |

## 四、專案結構

```
StockTradingSystem/
├── config/settings.yaml          # 資金、風險參數、股票池、LLM 模型（金鑰在 .env）
├── src/
│   ├── data/                     # fetchers（shioaji/twse/mops/finmind）+ 資料庫 + 品質檢查
│   ├── screener/                 # 量化多因子初篩
│   ├── agents/                   # 技術/籌碼/基本面分析師、驗證層、交易員、決策管線
│   ├── memory/                   # ChromaDB 向量記憶（經驗/規則/被擋交易）、成果評估、反思引擎
│   ├── risk/                     # Guard pipeline：部位規模、R:R、冷卻期、停損停利、熔斷
│   ├── env/ backtest/            # TWEnv 回測環境、成本模型、策略
│   ├── broker/                   # base / backtest / paper / live(Phase 6)
│   ├── report/                   # 績效統計
│   └── pipeline/                 # 每日主流程
├── api/ + frontend/              # FastAPI 後端 + React 交易終端
├── scripts/                      # backfill、run_daily、run_backtest、intraday_monitor
└── tests/
```

## 五、分階段執行計畫

| 階段 | 範圍 | 狀態 |
|---|---|---|
| Phase 0 | 地基：資料層、股票池、回補、品質檢查 | ✅ 完成 |
| Phase 1 | 量化選股 + 回測環境 + WebUI | ✅ 完成 |
| Phase 2 | LLM 分析師 + 交易員 Agent + 驗證層 | ✅ 核心完成 |
| Phase 3 | 資金控管 + Guard pipeline 風控 | ✅ 完成 |
| Phase 4 | Reflection + 向量記憶 | ✅ 完成 |
| Phase 5 | 模擬交易上線 | ✅ 機制完成，4 週無人值守實跑驗收進行中 |
| Phase 6 | 實盤（小資金漸進） | ⏳ 未開始 |

### Phase 0：地基 ✅

資料層與基礎設施：settings.yaml + .env 設定、SQLite schema、三源抓取器（shioaji/TWSE 官方/FinMind）、股票池篩選（處置股排除）、增量冪等回補、除權息還原價（backward adjust）、查詢時清洗、品質檢查器（交易日曆缺日 + OHLC 異常）、大盤指數、週/月 K 聚合。

**驗收**：可查任一股票歷史；重跑只補新資料不重抓；全市場活股覆蓋接近 100%。

### Phase 1：量化選股 + 回測環境 + WebUI ✅

技術指標層（MA/MACD/RSI/KD/ATR/布林）、多因子 Screener（動能/籌碼淨買/營收 YoY/站上季線，橫斷面 z-score 加權、無前視偏差）、TWEnv 事件驅動回測（台股成本模型、隔日開盤成交）、基準策略與績效指標（報酬/CAGR/Sharpe/Sortino/MDD/勝率/盈虧比）、WebUI 第一版。

**驗收**：純 WebUI 完成「改設定 → 更新資料 → 跑選股 → 跑回測 → 看結果」全流程。

### Phase 2：LLM 分析師 + 交易員 Agent + 驗證層 ✅

三位分析師 Agent（技術/籌碼/基本面）產出結構化 JSON 報告；驗證層將 LLM 引用數字逐項對照實算值（ADX/RSI/法人淨買/營收 YoY），不符則攔截並降信心；交易員 Agent 輸出 action score 與完整進出場計畫（進場區間/停損/目標/R:R）；LLM client 含結構化輸出、provider fallback chain 與呼叫記錄。

**驗收**：端到端實跑通過；驗證層實測能攔截人工注入的錯誤宣稱；R:R ≥ 1.5 生效。

### Phase 3：資金控管 + Guard pipeline 風控 ✅

九閘 pipeline：黑名單/處置股 → 回撤熔斷 → 計畫合理性 → R:R ≥ 1.5 → 停損冷卻 → 風險部位（單筆風險 ≤ 資金 1%）→ 單股上限 15% → 產業曝險 30% → 現金/持倉數上限。buy 計畫必過 Guard，核准附具體股數，駁回寫 friction log。含風控版回測策略（ATR 停損 + 冷卻 + 大盤濾網）。

**驗收**：2 年全市場回測 MDD −30.9% → −21.8%、波動 43.6% → 25.6%、Sharpe 持平。

### Phase 4：Reflection + 向量記憶 ✅

ChromaDB 三 collection（experiences/rules/blocked）；成果評估器以後續真實價格為過去交易計畫評分（含 hold/avoid 的假想報酬）；反思引擎由 LLM 歸納規則與反模式入庫；決策時自動檢索相似經驗與啟用規則注入交易員 prompt。

**驗收**：真實決策評估 → 反模式入庫 → 行為改變實證（同一標的注入規則前 buy、注入後 hold）。

### Phase 5：模擬交易上線 ✅（實跑驗收中）

PaperBroker 模擬帳本（真實價格撮合、隔日有效限價單、停損停利觸價、費稅損益、DB 持久化）；每日主流程（撮合 → 風控出場 → 權益快照 → 週反思 → LLM 決策 → Guard → 掛單）；後端內建排程器；盤中停損監控（30 秒輪詢）；Telegram 每日報告推播；績效報表（vs TAIEX alpha）。

**驗收標準**：連續 4 週無人工介入自動運行（進行中）。

### Phase 6：實盤（未開始）

- 前置條件：模擬績效達標（如 8 週正報酬且 MDD < 10%）
- LiveBroker（shioaji 實單 + CA 憑證簽署）、小資金漸進上線
- 額外保險：每日虧損上限即停、異常（API 斷線/資料缺漏）自動停機並通知
- 持續：每月策略檢討報告，因子與 prompt 迭代

## 六、每日自動化流程（穩態）

```
09:00 開盤   盤中停損監控啟動（30 秒輪詢持倉，觸及停損/停利即出場）
14:30 盤後   資料增量更新（股價/籌碼/估值/除權息/營收）
15:00       每日交易主流程：撮合昨日委託 → 停損停利 → 權益快照
            → (週五)反思 → Screener 初篩 → 分析師 → 交易員決策
            → Guard 風控 → 掛出隔日限價單 → Telegram 推播每日報告
```

## 七、風險與注意事項

- **LLM 成本**：每日候選 × 多分析師 + 決策 + 反思，估每日數十次呼叫；以快取與較小模型控制成本
- **前視偏差**：回測時新聞/財報必須嚴格用「當日已知」資料
- **LLM 不可直接下單**：所有輸出經 schema 驗證 + 風控規則閘門，規則層永遠有最終否決權
- **法規**：自有帳戶自動下單即可，勿代操；shioaji API 下單需簽署 API 服務條款

## 八、待辦與後續規劃

### 進行中
- **Phase 5 驗收**：連續 4 週無人工介入實跑（後端常駐 + 排程自動運行）

### 規劃內未實作
| 項目 | 出處 | 說明 |
|---|---|---|
| 移動停利（trailing stop） | Phase 3 | PaperBroker 目前為固定停損/停利 |
| A/B 回測（有無 reflection 績效對比） | Phase 4 | 待模擬交易累積足量樣本 |
| 新聞分析師 + RAG 過濾 | Phase 2 | 需新聞資料源（FinMind TaiwanStockNews） |
| 視覺 K 線雙模輸入（mplfinance） | Phase 2 | 技術分析師目前純數據輸入 |
| LLM 策略歷史回測 | Phase 2 | 成本高；已有成果評估器部分替代 |
| 全額交割股排除 | Phase 0 | config 旗標存在但無資料源（處置股已生效） |

### 低優先（優化項）
- 即時報價串流（自選清單/五檔/下單面板）——Phase 6 前接 shioaji 串流
- tick/分鐘級儲存（Parquet + DuckDB）——Phase 6 實盤需要再做
- 橫斷面索引 (date, stock_id)——選股查詢變慢時再加
- 財報三表/借券資料——基本面分析師火力升級（估值 PER/PBR/殖利率已接入）
