import clsx from "clsx";

type Tone = "good" | "warn" | "fail" | "teal" | "coral" | "muted";

const TONES: Record<Tone, string> = {
  good: "border-good/40 bg-good/10 text-good",
  warn: "border-warn/40 bg-warn/10 text-warn",
  fail: "border-fail/40 bg-fail/10 text-fail",
  teal: "border-teal/40 bg-teal/10 text-teal",
  coral: "border-coral/40 bg-coral/10 text-coral",
  muted: "border-line bg-panel-2 text-muted",
};

export function SeverityPill({
  children,
  tone = "muted",
  className,
}: {
  children: React.ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        TONES[tone],
        className
      )}
    >
      {children}
    </span>
  );
}
