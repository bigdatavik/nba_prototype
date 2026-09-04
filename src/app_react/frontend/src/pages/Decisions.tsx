import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, CheckCircle2 } from "lucide-react";
import { api } from "../lib/api";
import { KpiCard } from "../components/KpiCard";
import { DataTable } from "../components/DataTable";
import { SeverityPill } from "../components/SeverityPill";
import { Card, Button, Label, Select, Banner, SectionTitle } from "../components/ui";
import { Skeleton, SkeletonCard } from "../components/Skeleton";
import { fmtDate, fmtScore } from "../lib/format";

const OUTCOMES = ["Gap Closed", "Enrolled", "Retained", "No Response", "None"];

export function Decisions() {
  const qc = useQueryClient();
  const decisions = useQuery({ queryKey: ["decisions"], queryFn: () => api.decisions() });
  const [sel, setSel] = useState<number | "">("");
  const [outcome, setOutcome] = useState(OUTCOMES[0]);
  const [msg, setMsg] = useState<string | null>(null);

  const rows = decisions.data ?? [];
  useEffect(() => {
    if (sel === "" && rows.length) setSel(rows[0].decision_id);
  }, [rows, sel]);

  const save = useMutation({
    mutationFn: () => api.updateOutcome(Number(sel), outcome),
    onSuccess: () => {
      setMsg("Outcome saved — reflected on next read.");
      qc.invalidateQueries({ queryKey: ["decisions"] });
    },
    onError: () => setMsg("Could not update the outcome."),
  });

  const approved = rows.filter((r) => r.status === "Approved").length;
  const withOutcome = rows.filter((r) => r.outcome).length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold text-text">Decisions</h1>
        <p className="mt-1 text-sm text-muted">
          Closed-loop audit of committed NBA decisions (app-writes branch). Record outcomes as
          they land — reflected on the next read.
        </p>
      </div>

      {decisions.isLoading ? (
        <div className="grid grid-cols-3 gap-4">
          {[0, 1, 2].map((i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-3 gap-4">
          <KpiCard label="Total decisions" value={rows.length} accent="teal" />
          <KpiCard label="Approved" value={approved} accent="good" />
          <KpiCard label="Outcomes recorded" value={withOutcome} accent="coral" />
        </div>
      )}

      {decisions.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : rows.length === 0 ? (
        <Banner tone="info">
          No decisions yet. Approve one on Member Lookup → Assist → Approve &amp; act.
        </Banner>
      ) : (
        <>
          <DataTable
            columns={[
              { key: "created_at", header: "When", render: (r) => fmtDate(r.created_at) },
              { key: "member_id", header: "Member", mono: true },
              { key: "action_name", header: "Action" },
              { key: "channel", header: "Channel" },
              {
                key: "recommended_score",
                header: "Score",
                align: "right",
                mono: true,
                render: (r) => fmtScore(r.recommended_score),
              },
              {
                key: "status",
                header: "Status",
                render: (r) => (
                  <SeverityPill tone={r.status === "Approved" ? "good" : "muted"}>
                    {r.status}
                  </SeverityPill>
                ),
              },
              { key: "disposition", header: "Disposition" },
              {
                key: "outcome",
                header: "Outcome",
                render: (r) =>
                  r.outcome ? <SeverityPill tone="teal">{r.outcome}</SeverityPill> : "—",
              },
              { key: "approver", header: "Approver" },
            ]}
            rows={rows}
            rowKey={(r) => r.decision_id}
          />

          <Card className="p-5">
            <SectionTitle>
              <span className="flex items-center gap-2">
                <CheckCircle2 size={18} className="text-good" /> Record an outcome
              </span>
            </SectionTitle>
            <div className="grid gap-3 sm:grid-cols-[2fr_1fr_auto] sm:items-end">
              <div>
                <Label>Decision</Label>
                <Select value={sel} onChange={(e) => setSel(Number(e.target.value))}>
                  {rows.map((r) => (
                    <option key={r.decision_id} value={r.decision_id}>
                      #{r.decision_id} · {r.member_id} · {r.action_name} ({r.status})
                    </option>
                  ))}
                </Select>
              </div>
              <div>
                <Label>Outcome</Label>
                <Select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
                  {OUTCOMES.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </Select>
              </div>
              <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending || sel === ""}>
                <Save size={16} /> Save outcome
              </Button>
            </div>
            {msg && (
              <div className="mt-3">
                <Banner tone={msg.startsWith("Could not") ? "fail" : "good"}>{msg}</Banner>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
