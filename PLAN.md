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
| Reflection Analyst | **反思 Agent**：每日/每週回顧交易結果，歸納有效與無效訊號，產出策略調整建議，寫入「經驗記憶庫」供後續決策引用 |
| eth_env.py 交易環境 | **TWEnv**：台股交易環境（回測 / 模擬 / 實盤同一介面），內含手續費 0.1425%、證交稅 0.3%、漲跌幅 10%、整股/零股、T+2 交割 |

### LLM_trader 補強的關鍵機制（CryptoTrade 沒有的）

| LLM_trader 機制 | 台股版採用方式 |
|---|---|
| **向量記憶庫**（ChromaDB，三個 collection：交易經驗 / 語意規則 / 被擋交易） | 取代單純的反思文字檔。每筆平倉交易存入「情境+決策+結果」；反思引擎每 N 筆平倉合成「有效規則」與「反模式」；決策時以語意相似度檢索近似歷史情境注入 prompt |
| **驗證層（falsification）**：LLM 的宣稱逐項與計算指標交叉比對，不盲信 | 交易員 Agent 說「趨勢強勁」→ 用實算 ADX 驗證；說「爆量」→ 與實際量能百分位比對；不符則駁回或降信心分數。台股再加：宣稱的籌碼面（法人連買）必須與資料庫實際數字一致 |
| **視覺分析**：產生 K 線圖 PNG 給多模態 LLM 看圖 | 技術分析師改為「指標數據 + 4K K 線圖（含均線/量/KD）」雙模輸入，用 Claude 視覺能力辨識型態，取代硬編碼型態邏輯 |
| **Guard pipeline**：白名單、冷卻期、friction tracking | 風控閘門擴充：同一標的停損後 N 日冷卻、被駁回交易記錄原因（friction report）供反思分析、標的白名單/黑名單 |
| **R:R 下限**：強制最低 1.5:1 報酬風險比 | 交易計畫的（目標價-進場價)/(進場價-停損價）< 1.5 一律不進場 |
| **Provider fallback chain** | LLM 呼叫失敗自動降級備援模型，排程不因 API 故障中斷 |
| **RAG 新聞相關性評分** | 新聞先經 embedding 相關性評分過濾，只把高相關新聞餵給新聞分析師，省 token 又降噪 |

台股特有差異（設計時必須處理）：
- 交易時段 09:00–13:30（+ 盤後零股/定價），非 24h → 決策節奏以「日」為主，盤中僅做出場監控
- 漲跌幅 ±10%、處置股/警示股、全額交割股需排除
- 交易成本高於幣圈，頻繁進出會被成本吃掉 → 波段（數日～數週）為主

## 二、系統架構

```
┌────────────────────────── Scheduler（每日排程）──────────────────────────┐
│                                                                          │
│  [1] Data Layer 資料層                                                    │
│      price/volume（shioaji 主源、FinMind 指數+備援）                         │
│      籌碼/除權息+預告/估值（TWSE/TPEx 官方主源）、月營收（MOPS 主源）            │
│      → SQLite 本地資料庫，每日盤後增量更新（內建排程 14:30）                    │
│                                                                          │
│  [2] Screener 智慧選股（量化初篩 → LLM 精選）                                │
│      量化漏斗：流動性/非處置股 → 動能、籌碼、營收多因子評分 → Top 30             │
│      LLM 分析師團隊對 Top 30 逐檔產出報告 → 精選 5–10 檔候選 + 選股報告          │
│                                                                          │
│  [3] Decision 決策層                                                      │
│      交易員 Agent：候選報告 + K線圖(視覺) + 持倉狀態 + 向量記憶檢索出的           │
│      相似歷史情境/規則 → 每檔 action score [-1,1]                            │
│      + 進出場計畫（進場價區間、停損價、目標價、加減碼條件）                       │
│      ↓ Validation 驗證層：LLM 宣稱逐項對照實算指標（ADX/量能/籌碼數字），        │
│        不符則駁回或降信心，杜絕幻覺決策                                        │
│                                                                          │
│  [4] Risk & Money Management 資金/風險控管（純規則 Guard pipeline，LLM 不可逾越）│
│      單筆風險 ≤ 總資金 1%、單一持股 ≤ 15%、總持倉上限、同產業曝險上限            │
│      R:R ≥ 1.5 才進場、停損後同標的冷卻 N 日、白名單/黑名單                     │
│      強制停損、移動停利、最大回撤熔斷（DD > X% 全面降倉/停機）                   │
│      被駁回交易記錄原因（friction report）→ 回饋給反思層                       │
│                                                                          │
│  [5] Execution 執行層（統一 Broker 介面）                                    │
│      BacktestBroker → PaperBroker（shioaji 模擬單）→ LiveBroker（shioaji 實盤）│
│                                                                          │
│  [6] Reflection + Memory 反思學習層（ChromaDB 向量記憶庫）                   │
│      collection A 交易經驗：每筆平倉的 情境+決策+理由+結果                     │
│      collection B 語意規則：每 N 筆平倉/每週 反思合成的規則與反模式              │
│      collection C 被擋交易：friction report（若當初成交會如何？）              │
│      決策時語意檢索相似情境與規則注入 prompt → 持續檢討→學習→優化 閉環            │
│                                                                          │
│  [7] Reporting 報表/展示層                                                 │
│      每日選股報告、交易日誌（含 LLM 理由）、績效統計（報酬率/Sharpe/勝率/MDD）     │
│      WebUI（Streamlit，Phase 1 起）：⚙️設定中心（讀寫 settings.yaml）           │
│      資料狀態 / 選股 / 回測 / 持倉績效 / 大腦活動 / 反思規則庫 / 緊急停止          │
└──────────────────────────────────────────────────────────────────────────┘
```

## 三、技術選型

| 項目 | 選擇 | 說明 |
|---|---|---|
| 語言 | Python 3.11+（已有 .venv） | |
| 資料庫 | SQLite + Parquet（DuckDB 查詢） | 單機即可，免運維 |
| 行情/籌碼/基本面 | **shioaji**（股價主源）、**TWSE/TPEx 官方**（籌碼/除權息+預告/估值主源）、**MOPS**（月營收主源）、FinMind（指數價格+全面備援，402 自動等待） | 官方源免額度、按日全市場；終局架構 2026-07-04 定案 |
| 券商 API | **shioaji**（永豐金證券） | 台股最成熟 Python API，有模擬環境，紙上交易→實盤無縫切換 |
| LLM | Claude API（analyst 用 Sonnet、trader/reflection 用進階模型），失敗自動 fallback | 結構化輸出 JSON；技術分析師走多模態（K 線圖 PNG） |
| 向量記憶 | ChromaDB（本地） | 交易經驗/語意規則/被擋交易 三個 collection |
| K 線圖產生 | mplfinance | 產生含均線/量/KD 的 PNG 供視覺分析 |
| 回測 | 自建 TWEnv（仿 eth_env.py 的 gym 式介面） | 事件驅動日線回測，含台股成本模型 |
| WebUI | Streamlit（多頁應用）+ Plotly | **Phase 1 就上線**，同時是「設定中心」與「監控儀表板」，每個階段長出對應頁面；設定表單直接讀寫 settings.yaml（含驗證），不用手改檔案 |
| 排程 | **後端內建排程器**（asyncio，src/scheduler.py；2026-07-05 取代 launchd） | 平日 14:30 資料 / 15:00 決策；WebUI ⚙️→⏰ 監控與調整；觸發走 jobs.py 子行程、API 重啟不中斷；晚啟動當天補跑 |
| 通知 | Telegram / LINE Notify | 每日報告與成交回報推播 |

## 四、專案結構（目標）

```
StockTradingSystem/
├── config/settings.yaml          # 資金、風險參數、股票池、LLM 模型
├── src/
│   ├── data/                     # fetchers（finmind/twse/mops/news）+ 資料庫
│   ├── screener/                 # 量化多因子初篩
│   ├── agents/                   # technical/chips/fundamental/news 分析師、trader、reflection
│   ├── validation/               # LLM 宣稱 vs 實算指標 交叉驗證層
│   ├── charting/                 # mplfinance K 線圖產生（供視覺分析）
│   ├── memory/                   # ChromaDB 向量記憶（經驗/規則/被擋交易）
│   ├── risk/                     # Guard pipeline：部位規模、R:R、冷卻期、停損停利、熔斷
│   ├── env/                      # TWEnv 回測環境、成本模型
│   ├── broker/                   # base / backtest / paper(shioaji) / live(shioaji)
│   ├── report/                   # 日報、績效統計
│   └── pipeline/                 # daily_pipeline.py（每日主流程）
├── webui/
│   ├── app.py                    # Streamlit 入口
│   └── pages/                    # 1_設定.py、2_資料狀態.py、3_選股報告.py、
│                                 # 4_回測.py、5_持倉績效.py、6_大腦活動.py、7_反思規則庫.py
├── scripts/                      # backfill、run_backtest、run_daily
└── tests/
```

## 五、分階段執行計畫

### Phase 0：地基（約 1 週）✅ 已完成
- [x] 專案骨架、config（settings.yaml + .env）、logging、SQLite schema（6 張表）
- [x] FinMind 抓取器：股票清單、日K、三大法人、融資券、月營收
- [x] 股票池篩選（市場別/代號/ETF 排除；處置/全額交割旗標保留待後續事件資料強化）
- [x] `scripts/backfill.py` 增量 + 冪等回補；`scripts/init_db.py`；查詢 API `src/data/query.py`
- [x] 單元測試 3 passed；3 檔冒煙測試通過（日K/法人/融資券/月營收皆入庫可查）
- ✅ 驗收通過：可查任一股票歷史；重跑只補新資料不重抓
- [x] **資料層 P0/P1 完善**（參照 MT5/看盤軟體）：除權息還原價（backward adj，選股/分析師已切換）、
  查詢時清洗（零價列剔除+open夾取）、品質檢查器（TAIEX 日曆缺日偵測+OHLC 異常）、
  大盤指數 TAIEX/TPEx、週K/月K 即時聚合、處置股官方名單（select_universe 排除生效）
- 📌 資料層後續：~~TWSE 官方源 fallback~~（已升主源）、~~盤後自動排程~~（內建排程器 dataupdate）、
  財報三表/借券/新聞、橫斷面索引、tick 級儲存（Parquet+DuckDB，Phase 5）
- [x] 全市場回補完成（shioaji 股價 + 官方籌碼/除權息/估值，活股覆蓋接近 100%，
  顯示 <100% 部分為 stock_info 內含已下市股票的分母虛胖）

### Phase 1：量化選股 + 回測環境 + WebUI v1（約 2 週）✅ 已完成
- [x] 技術指標層 `src/indicators.py`（MA/MACD/RSI/KD/ATR/布林，純 pandas）
- [x] 多因子 Screener（動能20/60、籌碼淨買、營收YoY、站上季線 + 流動性過濾；橫斷面 z-score 加權，無前視偏差）
- [x] TWEnv 事件驅動回測環境（台股成本模型：手續費0.1425%+證交稅0.3%；隔日開盤成交、無前視）
- [x] 基準策略（買進持有、均線）+ Screener 月調倉策略 + 績效指標（報酬/CAGR/Sharpe/Sortino/MDD/勝率/盈虧比）
- [x] **WebUI v1（Streamlit 多頁）**：⚙️設定中心（ruamel 保留註解讀寫 settings.yaml）、📦資料狀態（含手動回補）、🔍智慧選股（Top N + 因子拆解 + K線圖）、📈回測（權益曲線 + 指標 + 逐筆成交）
- [x] 測試 8 passed（含成本模型、無前視執行、指標正確性）；WebUI 啟動 HTTP 200 無錯誤
- ✅ 驗收通過：純 WebUI 即可完成「改設定 → 更新資料 → 跑選股 → 跑回測 → 看結果」全流程
- ⚠️ 資料限制：FinMind 免費匿名額度約 70 檔即用罄（HTTP 402）→ **全市場回補需註冊免費 FinMind token 填入 .env**

### Phase 2：LLM 分析師 + 交易員 Agent + 驗證層（約 2–3 週）→ 對應需求 1、2 ✅ 核心完成
- [x] 三位資料型分析師 Agent（技術/籌碼/基本面，全用現成 DB 資料）→ 結構化 JSON 報告（`src/agents/analysts.py`）
- [x] **Validation 驗證層**（`src/agents/validator.py`）：LLM 引用數字逐項對照實算值（ADX/RSI/外資淨買/營收YoY），不符則攔截並降信心——**已驗證能攔截人工注入的錯誤宣稱**
- [x] 交易員 Agent（Opus 4.8 + adaptive thinking）→ action score + 進出場計畫（進場區間/停損/目標價/R:R≥1.5）（`src/agents/trader.py`）
- [x] LLM client（`src/llm/client.py`）：官方 anthropic SDK + `messages.parse` 結構化輸出 + provider fallback chain（Sonnet→Haiku）+ 呼叫記錄
- [x] 決策管線 `src/agents/pipeline.py`：初篩候選 → 3 分析師 → 驗證 → 交易員 → 存 trade_plan
- [x] WebUI 新增：📋 選股報告頁（分析師報告全文 + 交易計畫 + 驗證攔截標記）、🧠 大腦活動頁（各 Agent prompt/回應、驗證層攔截記錄）；設定中心加 ANTHROPIC_API_KEY 表單
- [x] **端到端實跑驗證通過**：1101 全空頭→交易員 avoid(-0.72)；1295 強動能但超買→交易員 hold + 完整計畫(進場130~136/停損124/目標158/R:R 2.36)
- [x] 修正:分數/信心正規化(LLM 誤把 [-1,1] 當百分比)；空 ANTHROPIC_AUTH_TOKEN 導致 SDK 壞掉的防護
- ✅ 驗收全通過：features 實算正確、驗證層能攔截錯誤宣稱、R:R≥1.5 生效、fallback chain 運作、WebUI 無錯誤
- 📌 後續增強（標記，非本階段阻塞）：新聞分析師 + RAG 過濾、mplfinance 視覺 K 線雙模輸入、LLM 策略歷史回測

### Phase 3：資金控管 + Guard pipeline 風控（約 1–1.5 週）→ 對應需求 3、4 ✅ 完成
- [x] `src/risk/guard.py` 九閘 pipeline：黑名單/處置股 → 回撤熔斷 → 計畫合理性 → R:R≥1.5 →
  停損冷卻 → 風險部位（1%資金/停損距離，整張優先高價退零股）→ 單股15%（縮減）→ 產業曝險30% → 現金/持倉數
- [x] LLM 管線整合：buy 計畫必過 Guard；核准附具體股數/投入/最大風險；駁回寫 friction_log
- [x] 回測策略 screener_risk：風險部位權重 + ATR×2 停損 + 冷卻 + **大盤濾網**（TAIEX<MA60→曝險×0.5）
- [x] UI：報告卡顯示 🛡️核准部位/駁回原因；API /api/friction
- ✅ 驗收全過：23 tests；2 年全市場實測 MDD -30.9%→-21.8%、波動 43.6%→25.6%、Sharpe 幾乎持平；
  端到端 2330 過閘核准 61 股（風險 <1% 資金）
- ⏳ 移動停利與「駁回若成交會如何」的追蹤 → 併入 Phase 4 反思 / Phase 5 實倉

### Phase 4：Reflection + 向量記憶（約 1.5–2 週）→「持續檢討、學習、優化」核心 ✅ 完成
- [x] ChromaDB 三 collection（experiences/rules/blocked），中文語意檢索實測可用
- [x] 成果評估器：過去 trade_plan 用後續真實價格評分（限價成交模擬/觸損觸標/20日horizon；
  hold/avoid 記假如報酬）——實倉前就能轉動檢討閉環
- [x] 反思引擎：統計+樣本 → LLM 歸納規則/反模式+風格建議 → 規則庫
- [x] 決策注入：trader prompt 自動帶入相似經驗+啟用規則（memory_context）
- [x] UI 📚 反思規則庫面板（一鍵反思、規則停用切換）；API /memory/* /reflect/run
- ✅ 驗收：8 筆真實決策評估→4 條反模式入庫→行為改變實證（2330 注入前 buy→注入後 hold，
  理由對照規則檢查）；30 tests passed
- ⏳ A/B 回測（有無 reflection 績效對比）：需累積更多樣本，Phase 5 實倉數據足夠後執行

### Phase 5：模擬交易上線（約 2–4 週實跑）→ 對應需求 5、6 ✅ 機制完成（實跑驗證期進行中）
- [x] PaperBroker 模擬帳本（真實價格撮合、隔日有效限價單、停損停利觸價、費稅損益、DB 持久化）
- [x] 每日主流程 run_daily：撮合→風控出場→權益快照→週五反思→LLM決策→Guard（真實持倉）→掛單
- [x] 排程（平日 15:00；2026-07-05 起為後端內建排程器）+ scripts/run_daily.py CLI + WebUI 一鍵執行
- [x] 績效報表：報酬/Sharpe/Sortino/MDD/勝率/盈虧比 + vs TAIEX alpha
- [x] UI 💰 持倉績效面板（權益曲線 vs 大盤、持倉/成交、緊急停止、重置帳本）
- ✅ 機制驗證：確定性週期分毫不差（掛單→成交→停損→-4,978 含費稅）；兩週循環實跑
- ⏳ 驗收中：連續 4 週無人工介入自動運行（內建排程器已就緒，需後端常駐實跑）
- 📌 後續：日報推播（Telegram/LINE）、盤中即時停損（shioaji 串流）、五檔/下單面板

### Phase 6：實盤（小資金漸進）
- 模擬績效達標（例：8 週正報酬且 MDD < 10%）後，LiveBroker 小資金上線
- 額外保險：每日虧損上限即停、異常（API 斷線/資料缺漏）自動停機並通知
- 持續：每月策略檢討報告，因子與 prompt 迭代

## 六、每日自動化流程（Phase 5 後的穩態）

```
15:00 盤後  更新行情/籌碼/新聞 → 每日反思（檢討昨日）→ Screener 初篩
16:00       分析師團隊分析候選 + 持倉 → 交易員決策 → 風控閘門 → 產生委託計畫
16:30       產出日報（選股報告+交易計畫+績效）→ Telegram 推播
08:30 開盤前 掛出委託單（限價）
盤中        監控停損/停利觸發（shioaji 即時行情回呼）
```

## 七、風險與注意事項
- **LLM 成本**：每日 30 檔 × 4 分析師 + 決策 + 反思，估每日數十次呼叫；回測期用快取與較小模型控制成本
- **前視偏差**：回測時新聞/財報必須嚴格用「當日已知」資料
- **LLM 不可直接下單**：所有輸出經 schema 驗證 + 風控規則閘門，規則層永遠有最終否決權
- **法規**：自有帳戶自動下單即可，勿代操；shioaji API 下單需簽署 API 服務條款

---

## 八、實作缺口總表（2026-07-04 全盤稽核）

盤點「規劃承諾 vs 實際程式」後的完整缺口清單，依影響排序：

### 🔴 高優先（影響決策品質/驗收）
| 缺口 | 現況 | 建議 |
|---|---|---|
| ~~月營收覆蓋僅 7%~~ | ✅ 已解（2026-07-04）：MOPS 官方月報升主源，7%→86% | 完成 |
| ~~除權息覆蓋僅 17%~~ | ✅ 完全解決（2026-07-04）：挖出 TPEx 隱藏歷史端點 POST `bulletin/exDailyQ`（回溯 2008、日期區間），上櫃 2020 起 +6733 筆一次補齊，覆蓋 45%→79%（其餘為未配息公司＝自然上限）；FinMind 徹底退出除權息。附帶抓到真兇：先前「TPEx 連線被拒」= Python 3.13 預設 `VERIFY_X509_STRICT` 撞上 TPEx 憑證缺 SKI 擴展，已用放寬 strict 的 session 修復（TPEx 籌碼源同步復活）。加碼亦完成：估值指標（本益比/殖利率/股價淨值比）已接入——TWSE BWIBBU_d + TPEx peQryDate（歷史按日可查，valuation 表 + 逐日標記回補），基本面分析師（含 per_percentile_1y 歷史位階、cited_per 驗證）、股票瀏覽器、資料健康報告全串上 | 完成 |
| **Phase 5 驗收：4 週無人工介入實跑** | 機制完成、內建排程器就緒（⚙️→⏰ 可監控），尚未起跑 | 重置帳本 → 後端常駐 → 開始計時 |

### 🟡 中優先（規劃內未實作的功能）
| 缺口 | 出處 | 說明 |
|---|---|---|
| 移動停利（trailing stop） | Phase 3 | PaperBroker 目前只有固定停損/停利 |
| 日報推播（Telegram/LINE） | Phase 5 | 每日流程結果目前只在 UI/log 可見 |
| 盤中即時停損 | Phase 5 | 現為收盤觸價檢查；需 shioaji 即時串流 |
| A/B 回測（有無 reflection 對比） | Phase 4 | 待模擬交易累積足量樣本 |
| 新聞分析師 + RAG 過濾 | Phase 2 | 需新聞資料源（FinMind TaiwanStockNews） |
| mplfinance 視覺 K 線雙模輸入 | Phase 2 | 技術分析師目前純數據輸入 |
| LLM 策略歷史回測 | Phase 2 | 成本高；已有成果評估器部分替代 |
| 全額交割股排除 | Phase 0 | config 旗標存在但無資料源（處置股已生效） |

### 🟢 低優先（優化項）
| 缺口 | 說明 |
|---|---|
| 即時報價串流（自選清單/五檔/下單面板） | 現為收盤基礎；Phase 6 前接 shioaji SSE |
| tick/分鐘級儲存（Parquet+DuckDB） | Phase 6 實盤需要再做 |
| 橫斷面索引 (date, stock_id) | 選股查詢變慢時再加 |
| 財報三表/借券資料 | 基本面分析師火力升級（估值 PER/PBR/殖利率已接入，財報三表/借券仍缺） |
| Streamlit legacy UI 移除 | React 已全面取代，留參考無害 |

### 🎁 稽核後加碼完成（2026-07-04 ~ 07-05）
- **除權息預告**：TWSE TWT48U_ALL + TPEx prepost 快照 → `dividend_forecast` 表，
  回補時同步更新；基本面分析師 feats 帶 next_ex_date（避開/預期除權息跳空）
- **股票總覽升級**：列表含開高低收/漲跌幅、當月除權息黃標（含金額）+ 專屬篩選；
  點股票開整頁詳情（總覽/籌碼/基本面/除權息/資料覆蓋 五分頁 + 走勢/法人買賣超/
  融資餘額/月營收/PER 圖表，/stocks/{id}/series 端點）
- **K 線還原↔原始價切換**；資料健康報告事件型資料（除權息）不再誤報覆蓋率
- **TLS 修正**：Python 3.13 VERIFY_X509_STRICT × TPEx 憑證缺 SKI = 先前所有
  「TPEx 連線被拒」的真兇；twse_source._session + fetchers 處置股全數修復

### Phase 6（未開始）
- LiveBroker（shioaji 實單 + CA 憑證簽署）、每日虧損上限即停、異常自動停機通知
- 前置：Phase 5 模擬績效達標（如 8 週正報酬且 MDD < 10%）
