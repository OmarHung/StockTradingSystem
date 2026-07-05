import { useEffect, useState } from "react";
import GridLayout, { useContainerWidth, type Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import "./theme.css";

import { api } from "./api";
import { TopBar } from "./components/TopBar";
import { Watchlist } from "./components/Watchlist";
import { KChart } from "./components/KChart";
import { ScreenerPanel } from "./components/ScreenerPanel";
import { ReportPanel } from "./components/ReportPanel";
import { BacktestModal } from "./components/BacktestPanel";
import { BrainPanel } from "./components/BrainPanel";
import { RankingPanel } from "./components/RankingPanel";
import { SettingsModal } from "./components/SettingsModal";
import { DataModal } from "./components/DataModal";
import { StockBrowserModal } from "./components/StockBrowserModal";
import { MemoryPanel } from "./components/MemoryPanel";
import { PortfolioPanel } from "./components/PortfolioPanel";

// 預設面板佈局（12 欄）。使用者可拖曳/縮放；把手在標題列。
// 調整結果存 localStorage（LS_LAYOUT_KEY），重新整理不會跑掉；TopBar ↺ 可重置。
const LS_LAYOUT_KEY = "sts.layout.v1";
const LAYOUT: Layout = [
  { i: "watchlist", x: 0, y: 0, w: 2, h: 7, minW: 2 },
  { i: "ranking", x: 0, y: 7, w: 2, h: 5, minW: 2 },
  { i: "kchart", x: 2, y: 0, w: 6, h: 7, minH: 4 },
  { i: "screener", x: 8, y: 0, w: 4, h: 7 },
  { i: "report", x: 2, y: 7, w: 10, h: 5 },
  { i: "portfolio", x: 0, y: 12, w: 12, h: 7 },
  { i: "brain", x: 0, y: 19, w: 7, h: 5 },
  { i: "memory", x: 7, y: 19, w: 5, h: 5 },
];

export default function App() {
  const [selected, setSelected] = useState("2330");
  const [name, setName] = useState("台積電");
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [brokerEnv, setBrokerEnv] = useState<"simulation" | "production" | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showData, setShowData] = useState(false);
  const [showBrowser, setShowBrowser] = useState(false);
  const [showBacktest, setShowBacktest] = useState(false);
  const [watchIds, setWatchIds] = useState<string[]>([]);
  const [layout, setLayout] = useState<Layout>(() => {
    try {
      const saved = localStorage.getItem(LS_LAYOUT_KEY);
      if (saved) {
        const parsed = JSON.parse(saved) as Layout;
        // 面板組成有變（新增/移除面板）→ 佈局過期，回預設
        const keys = new Set(parsed.map((l) => l.i));
        if (LAYOUT.every((l) => keys.has(l.i)) && parsed.length === LAYOUT.length) return parsed;
      }
    } catch { /* 壞資料回預設 */ }
    return LAYOUT;
  });
  const resetLayout = () => { localStorage.removeItem(LS_LAYOUT_KEY); setLayout([...LAYOUT]); };

  const { width, containerRef } = useContainerWidth();

  const refreshKey = () => api.health().then((h) => { setHasKey(h.has_api_key); setBrokerEnv(h.broker_env); }).catch(() => setHasKey(false));
  useEffect(() => { refreshKey(); }, []);

  // ESC 關閉最上層視窗（專業終端慣例）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (showBacktest) setShowBacktest(false);
      else if (showBrowser) setShowBrowser(false);
      else if (showData) setShowData(false);
      else if (showSettings) { setShowSettings(false); refreshKey(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showBacktest, showBrowser, showData, showSettings]);
  useEffect(() => { api.watchlist().then(setWatchIds).catch(() => {}); }, []);
  useEffect(() => { api.quote(selected).then((q) => setName(q.name)).catch(() => {}); }, [selected]);

  // 自選加入/移除（後端落庫，回傳更新後清單）
  const isWatched = (id: string) => watchIds.includes(id);
  const toggleWatch = async (id: string) => {
    try {
      const next = isWatched(id) ? await api.watchlistRemove(id) : await api.watchlistAdd(id);
      setWatchIds(next);
    } catch (e) { console.error(e); }
  };

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <TopBar hasKey={hasKey} brokerEnv={brokerEnv}
        onOpenSettings={() => setShowSettings(true)}
        onOpenData={() => setShowData(true)}
        onOpenBrowser={() => setShowBrowser(true)}
        onOpenBacktest={() => setShowBacktest(true)}
        onResetLayout={resetLayout} />
      {showSettings && <SettingsModal onClose={() => { setShowSettings(false); refreshKey(); }} />}
      {/* 回測視窗常駐掛載（display 切換）：關閉不重置參數/結果/進行中的輪詢 */}
      <div style={{ display: showBacktest ? "contents" : "none" }}>
        <BacktestModal onClose={() => setShowBacktest(false)} />
      </div>
      {showData && <DataModal onClose={() => setShowData(false)} />}
      {showBrowser && <StockBrowserModal onClose={() => setShowBrowser(false)} onSelect={setSelected} />}
      <div ref={containerRef} style={{ flex: 1, overflow: "auto", padding: 8 }}>
        <GridLayout
          className="layout"
          layout={layout}
          onLayoutChange={(l: Layout) => {
            setLayout(l);
            localStorage.setItem(LS_LAYOUT_KEY, JSON.stringify(l));
          }}
          width={width || 1400}
          gridConfig={{ cols: 12, rowHeight: 48, margin: [8, 8] }}
          dragConfig={{ handle: ".panel-drag-handle" }}
        >
          <div key="watchlist">
            <Watchlist ids={watchIds} selected={selected}
              onSelect={setSelected} onToggleWatch={toggleWatch} />
          </div>
          <div key="kchart">
            <KChart stockId={selected} name={name}
              watched={isWatched(selected)} onToggleWatch={() => toggleWatch(selected)} />
          </div>
          <div key="screener">
            <ScreenerPanel onSelect={setSelected}
              isWatched={isWatched} onToggleWatch={toggleWatch} />
          </div>
          <div key="report"><ReportPanel hasKey={!!hasKey} onSelect={setSelected} /></div>
          <div key="ranking"><RankingPanel onSelect={setSelected} /></div>
          <div key="portfolio"><PortfolioPanel onSelect={setSelected} /></div>
          <div key="brain"><BrainPanel /></div>
          <div key="memory"><MemoryPanel /></div>
        </GridLayout>
      </div>
    </div>
  );
}
