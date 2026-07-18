import { Newspaper } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { safeUrl } from "./Panel";

interface NewsRow {
  stock_id: string; name: string; date: string;
  published_at: string; title: string; source: string; url: string;
}
interface ScoutDate { as_of: string; source: string; headlines: number; candidates: number; }
type Scout = Awaited<ReturnType<typeof api.scout>>;

/** 新聞中心：個股新聞（news 表全部庫存）+ 題材掃描（scout_log 各日快照）。 */
export function NewsModal({ onClose, onSelect }: {
  onClose: () => void; onSelect: (id: string) => void;
}) {
  const [tab, setTab] = useState<"stock" | "scout">("stock");

  // 個股新聞
  const [rows, setRows] = useState<NewsRow[]>([]);
  const [kw, setKw] = useState("");
  const [sid, setSid] = useState("");
  const [loading, setLoading] = useState(false);

  // 題材掃描
  const [dates, setDates] = useState<ScoutDate[]>([]);
  const [pickedDate, setPickedDate] = useState("");
  const [scout, setScout] = useState<Scout>(null);

  const loadNews = (q = kw, s = sid) => {
    setLoading(true);
    api.newsAll(q, s).then(setRows).catch((e) => alert(String(e))).finally(() => setLoading(false));
  };
  useEffect(() => { loadNews("", ""); }, []);
  useEffect(() => {
    api.scoutDates().then((d) => {
      setDates(d);
      if (d.length > 0) setPickedDate(d[0].as_of);
    }).catch(() => {});
  }, []);
  useEffect(() => {
    if (!pickedDate) return;
    api.scout(pickedDate).then(setScout).catch(() => setScout(null));
  }, [pickedDate]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" style={{ width: 860, maxHeight: "88vh" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Newspaper size={15} /> 新聞中心
          </span>
          <span style={{ marginLeft: 10, fontSize: 11, color: "var(--text-dim)" }}>
            {tab === "stock" ? `庫存 ${rows.length} 則` : `${dates.length} 次掃描`}
          </span>
          <div style={{ flex: 1 }} />
          <span className="close" onClick={onClose}>✕</span>
        </div>

        {/* 分頁列 */}
        <div style={{ display: "flex", gap: 2, borderBottom: "1px solid var(--border)", padding: "0 16px" }}>
          {[["stock", "📰 個股新聞"], ["scout", "🛰️ 題材掃描"]].map(([k, label]) => (
            <div key={k} onClick={() => setTab(k as any)}
              style={{
                padding: "8px 18px", cursor: "pointer", fontSize: 12,
                color: tab === k ? "var(--text)" : "var(--text-dim)",
                borderBottom: tab === k ? "2px solid #2962ff" : "2px solid transparent",
                fontWeight: tab === k ? 600 : 400,
              }}>{label}</div>
          ))}
        </div>

        <div className="modal-body" style={{ padding: 12 }}>
          {tab === "stock" && (
            <>
              <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
                <input placeholder="🔍 標題關鍵字" value={kw} onChange={(e) => setKw(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && loadNews()} style={{ width: 200 }} autoFocus />
                <input placeholder="代號（如 2330）" value={sid} onChange={(e) => setSid(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && loadNews()} style={{ width: 120 }} />
                <button className="btn primary" onClick={() => loadNews()} disabled={loading}>搜尋</button>
                {(kw || sid) && <button className="btn" onClick={() => { setKw(""); setSid(""); loadNews("", ""); }}>清除</button>}
              </div>
              {loading && <div className="spinner">載入中…</div>}
              <div style={{ overflow: "auto", maxHeight: "64vh" }}>
                {rows.map((n, i) => (
                  <div key={i} style={{ padding: "7px 2px", fontSize: 12, borderBottom: "1px solid var(--border)" }}>
                    <span className="mono" style={{ color: "var(--text-dim)", marginRight: 8 }}>{n.date}</span>
                    <b className="mono" style={{ cursor: "pointer", marginRight: 6 }} title="切換主圖"
                      onClick={() => { onSelect(n.stock_id); onClose(); }}>
                      {n.stock_id}{n.name ? ` ${n.name}` : ""}
                    </b>
                    {safeUrl(n.url)
                      ? <a href={safeUrl(n.url)} target="_blank" rel="noreferrer" style={{ color: "var(--text)" }}>{n.title}</a>
                      : n.title}
                    {n.source && <span style={{ marginLeft: 6, fontSize: 11, color: "var(--text-dim)" }}>（{n.source}）</span>}
                  </div>
                ))}
                {!loading && rows.length === 0 && (
                  <div className="empty-hint">
                    尚無庫存個股新聞。個股新聞在進入 AI 深度分析時按需抓取（FinMind），
                    跑過「分析」或每日流程後這裡就會累積。
                  </div>
                )}
              </div>
            </>
          )}

          {tab === "scout" && (
            <>
              <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
                <select value={pickedDate} onChange={(e) => setPickedDate(e.target.value)}>
                  {dates.map((d) => (
                    <option key={d.as_of} value={d.as_of}>
                      {d.as_of}　{d.headlines} 則新聞 → {d.candidates} 檔候選（{d.source === "rss" ? "RSS" : "Web"}）
                    </option>
                  ))}
                </select>
              </div>
              {dates.length === 0 && (
                <div className="empty-hint">
                  尚無題材掃描記錄。政策題材偵察在「分析」或每日流程時執行，跑過後這裡就會有快照。
                </div>
              )}
              {scout && (
                <div style={{ overflow: "auto", maxHeight: "64vh" }}>
                  {scout.summary && (
                    <div style={{ fontSize: 12, lineHeight: 1.5, padding: 8, marginBottom: 8,
                      background: "rgba(240,185,11,0.06)", borderRadius: 4 }}>{scout.summary}</div>
                  )}
                  {scout.candidates.length > 0 && (
                    <div style={{ marginBottom: 10 }}>
                      {scout.candidates.map((c) => (
                        <div key={c.stock_id} style={{ padding: "6px 2px", fontSize: 12, borderBottom: "1px solid var(--border)" }}>
                          <b className="mono" style={{ cursor: "pointer" }} title="切換主圖"
                            onClick={() => { onSelect(c.stock_id); onClose(); }}>
                            📰 {c.stock_id} {c.name}
                          </b>
                          <span className="tag" style={{ marginLeft: 6, background: "rgba(240,185,11,0.15)", color: "var(--warning)" }}>{c.theme}</span>
                          <div style={{ color: "var(--text-dim)", marginTop: 3, lineHeight: 1.4 }}>{c.reason}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>
                    掃描到的新聞標題（{scout.headlines.length} 則）
                  </div>
                  {scout.headlines.map((h, i) => (
                    <div key={i} style={{ padding: "4px 2px", fontSize: 11, borderBottom: "1px solid var(--border)" }}>
                      <span className="mono" style={{ color: "var(--text-dim)", marginRight: 6 }}>{h.date.slice(5)}</span>
                      {safeUrl(h.url)
                        ? <a href={safeUrl(h.url)} target="_blank" rel="noreferrer" style={{ color: "var(--text)" }}>{h.title}</a>
                        : <span style={{ color: "var(--text)" }}>{h.title}</span>}
                      <span style={{ marginLeft: 6, color: "var(--text-dim)" }}>（{h.source}）</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
