import { Sparkles, ArrowRight } from "lucide-react";
import { RankedAction } from "../lib/api";
import { ScoreBar } from "./ScoreBar";
import { SeverityPill } from "./SeverityPill";
import { Button } from "./ui";
import { fmtScore } from "../lib/format";
import clsx from "clsx";

function bandTone(band: string): "fail" | "warn" | "muted" {
  const b = band.toLowerCase();
  if (b.includes("high")) return "fail";
  if (b.includes("medium")) return "warn";
  return "muted";
}

export function ActionCard({
  action,
  threshold,
  recommended = false,
  onAssist,
}: {
  action: RankedAction;
  threshold: number;
  recommended?: boolean;
  onAssist: (a: RankedAction) => void;
}) {
  return (
    <div
      className={clsx(
        "rounded-xl border bg-panel p-4 shadow-panel transition-colors",
        recommended ? "border-coral/50" : "border-line hover:border-teal/40"
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={clsx(
            "tnum mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-sm font-semibold",
            recommended ? "bg-coral text-ink" : "bg-panel-2 text-muted"
          )}
        >
          {action.rank}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-base font-semibold text-text">
              {action.action_name}
            </span>
            {recommended && (
              <SeverityPill tone="coral">
                <Sparkles size={12} /> RECOMMENDED
              </SeverityPill>
            )}
            <SeverityPill tone={bandTone(action.band)}>{action.band}</SeverityPill>
            {action.compliance && <SeverityPill tone="teal">Compliance</SeverityPill>}
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
            {action.category && <span>Category · {action.category}</span>}
            {action.team && <span>Team · {action.team}</span>}
            <span>Channel · {action.channel}</span>
            <span>Value · {action.value_score}</span>
          </div>

          {action.description && (
            <p className="mt-2 line-clamp-2 text-sm text-muted">{action.description}</p>
          )}

          <div className="mt-3 flex items-center gap-3">
            <ScoreBar value={action.score} threshold={threshold} className="flex-1" />
            <span className="tnum w-16 text-right font-mono text-sm text-text">
              {fmtScore(action.score)}
            </span>
          </div>
        </div>
      </div>

      <div className="mt-3 flex justify-end">
        <Button variant={recommended ? "coral" : "outline"} size="sm" onClick={() => onAssist(action)}>
          Assist <ArrowRight size={14} />
        </Button>
      </div>
    </div>
  );
}
