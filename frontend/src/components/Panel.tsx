import type { ReactNode } from "react";

/** 面板外框：標題列（含拖曳把手 class）+ 內容區。 */
export function Panel({
  title, icon, sub, right, children,
}: {
  title: string; icon?: string; sub?: string; right?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="panel">
      <div className="panel-header panel-drag-handle">
        {icon && <span className="icon">{icon}</span>}
        <span>{title}</span>
        {sub && <span className="sub">{sub}</span>}
        <div style={{ flex: 1 }} />
        {right}
      </div>
      <div className="panel-body">{children}</div>
    </div>
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
