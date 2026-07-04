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
import { BacktestPanel } from "./components/BacktestPanel";
import { BrainPanel } from "./components/BrainPanel";
import { SettingsModal } from "./components/SettingsModal";
import { DataModal } from "./components/DataModal";

// 預設面板佈局（12 欄）。使用者可拖曳/縮放；把手在標題列。
// 資料狀態改為頂部按鈕開窗（同設定），騰出版面給大腦活動。
const LAYOUT: Layout = [
  { i: "watchlist", x: 0, y: 0, w: 2, h: 12, minW: 2 },
  { i: "kchart", x: 2, y: 0, w: 6, h: 7, minH: 4 },
  { i: "screener", x: 8, y: 0, w: 4, h: 7 },
  { i: "report", x: 2, y: 7, w: 6, h: 5 },
  { i: "backtest", x: 8, y: 7, w: 4, h: 5 },
  { i: "brain", x: 0, y: 12, w: 12, h: 5 },
];

export default function App() {
  const [selected, setSelected] = useState("2330");
  const [name, setName] = useState("台積電");
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [brokerEnv, setBrokerEnv] = useState<"simulation" | "production" | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showData, setShowData] = useState(false);
  const [watchIds, setWatchIds] = useState<string[]>([]);

  const { width, containerRef } = useContainerWidth();

  const refreshKey = () => api.health().then((h) => { setHasKey(h.has_api_key); setBrokerEnv(h.broker_env); }).catch(() => setHasKey(false));
  useEffect(() => { refreshKey(); }, []);
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
        onOpenData={() => setShowData(true)} />
      {showSettings && <SettingsModal onClose={() => { setShowSettings(false); refreshKey(); }} />}
      {showData && <DataModal onClose={() => setShowData(false)} />}
      <div ref={containerRef} style={{ flex: 1, overflow: "auto", padding: 8 }}>
        <GridLayout
          className="layout"
          layout={LAYOUT}
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
          <div key="backtest"><BacktestPanel /></div>
          <div key="brain"><BrainPanel /></div>
        </GridLayout>
      </div>
    </div>
  );
}
