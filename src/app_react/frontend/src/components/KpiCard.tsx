import clsx from "clsx";

// Big tabular number + label + accent stripe (deck's KPI tiles).
export function KpiCard({
  label,
  value,
  sub,
  accent = "teal",
  icon,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  accent?: "teal" | "coral" | "good" | "warn" | "fail";
  icon?: React.ReactNode;
}) {
  const stripe = {
    teal: "bg-teal",
    coral: "bg-coral",
    good: "bg-good",
    warn: "bg-warn",
    fail: "bg-fail",
  }[accent];

  return (
    <div className="relative overflow-hidden rounded-xl border border-line bg-panel p-4 shadow-panel">
      <div className={clsx("absolute inset-y-0 left-0 w-1", stripe)} />
      <div className="flex items-start justify-between">
        <div className="text-xs font-medium uppercase tracking-wide text-muted">
          {label}
        </div>
        {icon && <div className="text-muted">{icon}</div>}
      </div>
      <div className="tnum mt-2 font-display text-3xl font-semibold text-text">
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  );
}
