import type { CSSProperties, ReactNode } from "react";

/** 面板外框：標題列（含拖曳把手 class）+ 內容區。 */
export function Panel({
  title, icon, sub, right, children,
}: {
  title: string; icon?: ReactNode; sub?: string; right?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="panel">
      <div className="panel-header panel-drag-handle">
        {icon && <span className="icon" style={{ display: "inline-flex", alignItems: "center" }}>{icon}</span>}
        <span>{title}</span>
        {sub && <span className="sub">{sub}</span>}
        <div style={{ flex: 1 }} />
        {right}
      </div>
      <div className="panel-body">{children}</div>
    </div>
  );
}

/** 自選星星切換按鈕：實心=已加入、空心=未加入。點擊不冒泡到列的 onClick。 */
export function StarButton({
  active, onToggle, size = 15,
}: { active: boolean; onToggle: () => void; size?: number }) {
  return (
    <span
      role="button"
      title={active ? "移除自選" : "加入自選"}
      onClick={(e) => { e.stopPropagation(); onToggle(); }}
      style={{
        cursor: "pointer", fontSize: size, lineHeight: 1,
        color: active ? "#f5c518" : "var(--text-dim)", userSelect: "none",
      }}
    >
      {active ? "★" : "☆"}
    </span>
  );
}

export function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function cls(n: number | null | undefined): string {
  if (n === null || n === undefined) return "flat";
  return n > 0 ? "up" : n < 0 ? "down" : "flat";
}

/**
 * 台北時間（Asia/Taipei）當下的分鐘數與星期——所有交易時段判斷的單一來源，
 * 避免各元件各自用瀏覽器本地時間（非台灣時區的瀏覽器會判斷錯誤）。
 */
export function taipeiClock(): { day: number; mins: number } {
  const tw = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Taipei" }));
  return { day: tw.getDay(), mins: tw.getHours() * 60 + tw.getMinutes() };
}

/** 台北時間是否在交易時段內（預設 08:55–13:35，週一～五）。 */
export function marketOpen(startMin = 8 * 60 + 55, endMin = 13 * 60 + 35): boolean {
  const { day, mins } = taipeiClock();
  return day >= 1 && day <= 5 && mins >= startMin && mins <= endMin;
}

/**
 * 外部連結白名單：只放行 http/https，擋掉 javascript:/data: 等可執行 scheme。
 * 新聞 URL 來自第三方抓取（FinMind/RSS/Web），是唯一把外部不可信資料當 href
 * sink 的位置；不合法時回 undefined（呼叫端退為純文字）。
 */
export function safeUrl(u: string | null | undefined): string | undefined {
  if (!u) return undefined;
  try {
    const proto = new URL(u, window.location.origin).protocol;
    return proto === "http:" || proto === "https:" ? u : undefined;
  } catch {
    return undefined;
  }
}

/**
 * 千分位金額輸入框：以逗號分隔顯示（1,234,567），輸入時即時格式化。
 * 用 type="text" 是因為原生 type="number" 不會顯示千分位分隔。
 * 僅接受非負整數（台股金額皆為整數新台幣）。
 */
export function MoneyInput({
  value, onChange, style, className,
}: {
  value: number | null | undefined;
  onChange: (v: number) => void;
  style?: CSSProperties;
  className?: string;
}) {
  const display =
    value === null || value === undefined || Number.isNaN(value)
      ? ""
      : value.toLocaleString("en-US");
  return (
    <input
      type="text"
      inputMode="numeric"
      value={display}
      className={className}
      style={style}
      onChange={(e) => {
        const digits = e.target.value.replace(/[^\d]/g, "");
        onChange(digits === "" ? 0 : Number(digits));
      }}
    />
  );
}
