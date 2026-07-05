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
