import clsx from "clsx";

// Horizontal meter for a 0..1 NBA priority score. Colour tracks the band so the
// bar reads at a glance (teal = high, warn = medium, muted = watch).
export function ScoreBar({
  value,
  threshold,
  className,
}: {
  value: number;
  threshold?: number;
  className?: string;
}) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const tone =
    value >= 0.66 ? "bg-teal" : value >= 0.4 ? "bg-warn" : "bg-muted";
  return (
    <div className={clsx("relative h-2 w-full rounded-full bg-panel-2", className)}>
      <div
        className={clsx("h-full rounded-full transition-all", tone)}
        style={{ width: `${pct}%` }}
      />
      {threshold !== undefined && (
        <div
          className="absolute top-[-2px] h-3 w-px bg-coral"
          style={{ left: `${threshold * 100}%` }}
          title={`Action threshold ${threshold.toFixed(2)}`}
        />
      )}
    </div>
  );
}
