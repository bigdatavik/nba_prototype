import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, PenLine, RefreshCw, Check, Trash2, TrendingDown } from "lucide-react";
import clsx from "clsx";
import {
  api,
  RankedAction,
  Member,
  Reference,
  AppConfig,
  WhatIfResponse,
} from "../lib/api";
import { Drawer } from "./Drawer";
import { Button, Label, Select, Textarea, Input, Banner } from "./ui";
import { SeverityPill } from "./SeverityPill";
import { ScoreBar } from "./ScoreBar";
import { DataTable } from "./DataTable";
import { fmt2, fmtScore, fmtPct, fmtDate } from "../lib/format";

type Tab = "why" | "draft" | "whatif" | "approve";

const TABS: { id: Tab; label: string }[] = [
  { id: "why", label: "Why this action" },
  { id: "draft", label: "Draft outreach" },
  { id: "whatif", label: "What-if" },
  { id: "approve", label: "Approve & act" },
];

export function AssistDrawer({
  open,
  onClose,
  memberId,
  member,
  action,
  ranked,
  reference,
  config,
  threshold,
}: {
  open: boolean;
  onClose: () => void;
  memberId: string;
  member: Member;
  action: RankedAction | null;
  ranked: RankedAction[];
  reference: Reference;
  config: AppConfig;
  threshold: number;
}) {
  const [tab, setTab] = useState<Tab>("why");

  if (!action) return null;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width="max-w-2xl"
      title={action.action_name}
      subtitle={`${memberId} · ${action.category || "—"} · ${action.channel}`}
    >
      <div className="mb-4 flex gap-1 rounded-lg border border-line bg-panel-2 p-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={clsx(
              "flex-1 rounded-md px-2 py-1.5 text-xs font-medium transition-colors",
              tab === t.id ? "bg-teal text-ink" : "text-muted hover:text-text"
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "why" && (
        <WhyTab member={member} memberId={memberId} action={action} threshold={threshold} />
      )}
      {tab === "draft" && (
        <DraftTab memberId={memberId} action={action} reference={reference} config={config} />
      )}
      {tab === "whatif" && (
        <WhatIfTab memberId={memberId} member={member} threshold={threshold} currentTopId={ranked[0]?.action_id} />
      )}
      {tab === "approve" && (
        <ApproveTab memberId={memberId} ranked={ranked} reference={reference} config={config} />
      )}
    </Drawer>
  );
}

/* ------------------------------- Why ------------------------------------- */
function WhyTab({
  action,
  threshold,
}: {
  member: Member;
  memberId: string;
  action: RankedAction;
  threshold: number;
}) {
  const traj = action.trajectory;
  const above = action.score >= threshold;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-4 gap-3">
        <Metric label="Priority score" value={fmt2(action.score)} />
        <Metric label="Band" value={action.band} />
        <Metric label="Category" value={action.category || "—"} />
        <Metric label="Compliance" value={action.compliance ? "Yes" : "No"} />
      </div>

      <div>
        <ScoreBar value={action.score} threshold={threshold} />
        <p className="mt-2 text-sm text-muted">
          {above ? (
            <>
              Priority score <b className="text-text">{fmt2(action.score)}</b> exceeds the{" "}
              <b className="text-text">{fmt2(threshold)}</b> action threshold — recommend now.
            </>
          ) : (
            <>
              Priority score <b className="text-text">{fmt2(action.score)}</b> is below the{" "}
              {fmt2(threshold)} high-priority threshold; recommended as the best available fit.
            </>
          )}
        </p>
      </div>

      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Reasoning
        </div>
        <ul className="space-y-1.5">
          {action.explain.map((r, i) => (
            <li key={i} className="flex gap-2 text-sm text-text">
              <span className="text-teal">{i + 1}.</span>
              {r}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded-lg border border-line bg-panel-2 p-4">
        <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
          <TrendingDown size={14} className="text-good" /> Predicted trajectory
        </div>
        <div className="space-y-1.5 text-sm text-text">
          <div>
            Without intervention: {traj.label} likely drifts to{" "}
            <b>{fmt2(traj.without)}</b> (now {fmt2(traj.current)})
          </div>
          <div>
            With {action.action_name}: expected reduction ~
            <b className="text-good">{fmtPct(traj.lift_pct)}</b> → <b>{fmt2(traj.with_new)}</b>
          </div>
        </div>
        <p className="mt-2 text-xs text-muted">
          Trajectory is a heuristic projection for illustration, not a clinical prediction.
        </p>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-panel-2 p-2.5">
      <div className="text-[10px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className="tnum mt-1 truncate font-display text-lg font-semibold text-text">{value}</div>
    </div>
  );
}

/* ------------------------------ Draft ------------------------------------ */
function DraftTab({
  memberId,
  action,
  reference,
  config,
}: {
  memberId: string;
  action: RankedAction;
  reference: Reference;
  config: AppConfig;
}) {
  const defaultCh = reference.channels.includes(action.channel)
    ? action.channel
    : reference.channels[0];
  const [channel, setChannel] = useState(defaultCh);
  const [draft, setDraft] = useState("");

  const mutation = useMutation({
    mutationFn: () =>
      api.draftOutreach({ member_id: memberId, action_id: action.action_id, channel }),
    onSuccess: (r) => setDraft(r.message),
  });

  if (!config.llm_enabled) {
    return (
      <Banner tone="info">
        Drafting is disabled — set <code>LLM_ENDPOINT_NAME</code> to enable.
      </Banner>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <Label>Channel</Label>
        <Select value={channel} onChange={(e) => setChannel(e.target.value)}>
          {reference.channels.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </Select>
      </div>
      <Button variant="primary" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
        {mutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <PenLine size={16} />}
        Draft outreach message
      </Button>
      {mutation.isError && (
        <Banner tone="warn">
          {(mutation.error as Error).message || "Could not generate a draft."}
        </Banner>
      )}
      {draft && (
        <div>
          <Label>Draft (review &amp; edit before sending)</Label>
          <Textarea rows={7} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <p className="mt-1.5 text-xs text-muted">
            Compliance: review before sending. No PHI is included in the prompt.
          </p>
        </div>
      )}
    </div>
  );
}

/* ------------------------------ What-if ---------------------------------- */
function num(m: Member, k: string, d = 0): number {
  const v = Number(m[k]);
  return Number.isFinite(v) ? v : d;
}

function WhatIfTab({
  memberId,
  member,
  threshold,
  currentTopId,
}: {
  memberId: string;
  member: Member;
  threshold: number;
  currentTopId?: string;
}) {
  const [churn, setChurn] = useState(num(member, "churn_risk_score"));
  const [eng, setEng] = useState(num(member, "engagement_score"));
  const [clin, setClin] = useState(num(member, "clinical_risk_score"));
  const [gap, setGap] = useState(num(member, "has_preventive_gap") >= 1);
  const [result, setResult] = useState<WhatIfResponse | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      api.whatif(memberId, {
        churn_risk_score: churn,
        engagement_score: eng,
        clinical_risk_score: clin,
        has_preventive_gap: gap,
      }),
    onSuccess: (r) => setResult(r),
  });

  const newTop = result?.ranked[0];
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        Adjust member signals and re-score to see how the recommendation changes.
      </p>
      <Slider label="Churn risk" min={0} max={1} step={0.05} value={churn} onChange={setChurn} />
      <Slider label="Engagement" min={0} max={10} step={0.5} value={eng} onChange={setEng} />
      <Slider label="Clinical risk" min={0} max={1} step={0.05} value={clin} onChange={setClin} />
      <label className="flex items-center gap-2 text-sm text-text">
        <input
          type="checkbox"
          checked={gap}
          onChange={(e) => setGap(e.target.checked)}
          className="h-4 w-4 accent-teal"
        />
        Has preventive gap
      </label>

      <Button variant="primary" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
        {mutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />}
        Re-score with these values
      </Button>

      {newTop && (
        <>
          {newTop.action_id !== currentTopId ? (
            <Banner tone="good">
              New top action: <b>{newTop.action_name}</b>
            </Banner>
          ) : (
            <Banner tone="info">
              Top action unchanged: <b>{newTop.action_name}</b>
            </Banner>
          )}
          <DataTable
            columns={[
              { key: "rank", header: "#", mono: true },
              { key: "action_name", header: "Action" },
              {
                key: "score",
                header: "Score",
                align: "right",
                mono: true,
                render: (r) => fmtScore(r.score),
              },
              { key: "channel", header: "Channel" },
              { key: "category", header: "Category" },
            ]}
            rows={result!.ranked}
            rowKey={(r) => r.action_id}
          />
          <p className="text-xs text-muted">Threshold {fmt2(threshold)}.</p>
        </>
      )}
    </div>
  );
}

function Slider({
  label,
  min,
  max,
  step,
  value,
  onChange,
}: {
  label: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <Label>{label}</Label>
        <span className="tnum font-mono text-sm text-teal">{value}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-teal"
      />
    </div>
  );
}

/* ------------------------------ Approve ---------------------------------- */
function ApproveTab({
  memberId,
  ranked,
  reference,
  config,
}: {
  memberId: string;
  ranked: RankedAction[];
  reference: Reference;
  config: AppConfig;
}) {
  const qc = useQueryClient();
  const [actionName, setActionName] = useState(ranked[0]?.action_name ?? "");
  const selected = ranked.find((r) => r.action_name === actionName) ?? ranked[0];
  const defaultCh =
    selected && reference.channels.includes(selected.channel)
      ? selected.channel
      : reference.channels[0];
  const [channel, setChannel] = useState(defaultCh);
  const [disposition, setDisposition] = useState("Outreach scheduled");
  const [note, setNote] = useState("");
  const [approver, setApprover] = useState(config.user || "care_coordinator");
  const [msg, setMsg] = useState<{ tone: "good" | "info" | "fail"; text: string } | null>(null);

  const history = useQuery({
    queryKey: ["decisions", memberId],
    queryFn: () => api.decisions(memberId),
  });

  const commit = useMutation({
    mutationFn: (status: "Approved" | "Dismissed") =>
      api.recordDecision({
        member_id: memberId,
        action_id: selected?.action_id,
        action_name: actionName,
        channel,
        score: selected?.score ?? 0,
        status,
        disposition: status === "Dismissed" ? "Dismissed" : disposition,
        note,
        approver,
      }),
    onSuccess: (_r, status) => {
      setMsg(
        status === "Approved"
          ? { tone: "good", text: `Decision committed: ${memberId} → ${actionName} via ${channel}.` }
          : { tone: "info", text: "Recommendation dismissed and logged." }
      );
      qc.invalidateQueries({ queryKey: ["decisions"] });
    },
    onError: () => setMsg({ tone: "fail", text: "Could not write the decision to app-writes." }),
  });

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        Approve or adjust the recommendation, then commit. The decision is written to the
        app-writes branch and appears below (closed loop).
      </p>

      <div>
        <Label>Action to act on</Label>
        <Select
          value={actionName}
          onChange={(e) => {
            setActionName(e.target.value);
            const s = ranked.find((r) => r.action_name === e.target.value);
            if (s) setChannel(reference.channels.includes(s.channel) ? s.channel : reference.channels[0]);
          }}
        >
          {ranked.map((r) => (
            <option key={r.action_id} value={r.action_name}>
              {r.action_name}
            </option>
          ))}
        </Select>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>Channel</Label>
          <Select value={channel} onChange={(e) => setChannel(e.target.value)}>
            {reference.channels.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Disposition</Label>
          <Select value={disposition} onChange={(e) => setDisposition(e.target.value)}>
            {["Outreach scheduled", "Attempted", "Declined by member", "Deferred"].map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </Select>
        </div>
      </div>

      <div>
        <Label>Note (optional)</Label>
        <Input value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <div>
        <Label>Approver</Label>
        <Input value={approver} onChange={(e) => setApprover(e.target.value)} />
      </div>

      <div className="flex gap-2">
        <Button variant="primary" onClick={() => commit.mutate("Approved")} disabled={commit.isPending}>
          <Check size={16} /> Approve &amp; commit
        </Button>
        <Button variant="danger" onClick={() => commit.mutate("Dismissed")} disabled={commit.isPending}>
          <Trash2 size={16} /> Dismiss
        </Button>
      </div>

      {msg && <Banner tone={msg.tone === "info" ? "info" : msg.tone}>{msg.text}</Banner>}

      <div>
        <div className="mb-2 mt-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Decisions for this member
        </div>
        <DataTable
          columns={[
            { key: "created_at", header: "When", render: (r) => fmtDate(r.created_at) },
            { key: "action_name", header: "Action" },
            { key: "channel", header: "Channel" },
            {
              key: "status",
              header: "Status",
              render: (r) => (
                <SeverityPill tone={r.status === "Approved" ? "good" : "muted"}>
                  {r.status}
                </SeverityPill>
              ),
            },
            { key: "outcome", header: "Outcome" },
            { key: "approver", header: "Approver" },
          ]}
          rows={history.data ?? []}
          rowKey={(r) => r.decision_id}
          empty="No decisions yet for this member."
        />
      </div>
    </div>
  );
}
