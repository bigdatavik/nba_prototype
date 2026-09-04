import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sparkles, AlertCircle } from "lucide-react";
import { api, RankedAction } from "../lib/api";
import { useGenie } from "../lib/store";
import { KpiCard } from "../components/KpiCard";
import { ActionCard } from "../components/ActionCard";
import { AssistDrawer } from "../components/AssistDrawer";
import { SeverityPill } from "../components/SeverityPill";
import { Card, Select, Banner, SectionTitle } from "../components/ui";
import { Skeleton, SkeletonCard } from "../components/Skeleton";
import { fmt1, fmt2, fmtInt, fmtScore } from "../lib/format";

export function Lookup() {
  const [member, setMember] = useState<string>("");
  const [assist, setAssist] = useState<RankedAction | null>(null);
  const setContextMember = useGenie((s) => s.setContextMember);

  const members = useQuery({ queryKey: ["members"], queryFn: api.members });
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const reference = useQuery({ queryKey: ["reference"], queryFn: api.reference });

  // default to first member once loaded
  useEffect(() => {
    if (!member && members.data?.length) setMember(members.data[0]);
  }, [members.data, member]);

  useEffect(() => {
    setContextMember(member || null);
  }, [member, setContextMember]);

  const score = useQuery({
    queryKey: ["score", member],
    queryFn: () => api.score(member),
    enabled: !!member,
  });

  const m = score.data?.member;
  const ranked = score.data?.ranked ?? [];
  const threshold = score.data?.priority_threshold ?? reference.data?.priority_threshold ?? 0.66;
  const top = ranked[0];

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">Next Best Action</h1>
          <p className="mt-1 text-sm text-muted">
            Score &amp; rank actions for a member, then act on the recommendation.
          </p>
        </div>
        <div className="w-full sm:w-72">
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-muted">
            Member ID
          </label>
          {members.isLoading ? (
            <Skeleton className="h-10 w-full" />
          ) : members.isError ? (
            <Banner tone="fail">Cannot connect to Lakebase.</Banner>
          ) : (
            <Select value={member} onChange={(e) => setMember(e.target.value)}>
              {members.data?.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </Select>
          )}
        </div>
      </div>

      {/* KPI row */}
      {score.isLoading ? (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      ) : score.isError ? (
        <Banner tone="fail">{(score.error as Error).message}</Banner>
      ) : m ? (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <KpiCard label="Age" value={fmtInt(m.age)} accent="teal" />
          <KpiCard label="Churn Risk" value={fmt2(Number(m.churn_risk_score))} accent="coral" />
          <KpiCard label="Engagement" value={`${fmt1(Number(m.engagement_score))}/10`} accent="good" />
          <KpiCard label="Claims (12m)" value={fmtInt(m.total_claims_12m)} accent="warn" />
        </div>
      ) : null}

      {/* Recommended hero */}
      {score.isLoading ? (
        <Skeleton className="h-32 w-full" />
      ) : top ? (
        <Card className="relative overflow-hidden border-coral/40 p-5">
          <div className="absolute inset-y-0 left-0 w-1 bg-coral" />
          <div className="flex flex-wrap items-center gap-2">
            <SeverityPill tone="coral">
              <Sparkles size={12} /> RECOMMENDED ACTION
            </SeverityPill>
            <SeverityPill
              tone={
                top.band.toLowerCase().includes("high")
                  ? "fail"
                  : top.band.toLowerCase().includes("medium")
                  ? "warn"
                  : "muted"
              }
            >
              {top.band}
            </SeverityPill>
          </div>
          <h2 className="mt-3 font-display text-2xl font-semibold text-text">{top.action_name}</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">{top.description}</p>
          <div className="mt-4 flex flex-wrap items-center gap-x-8 gap-y-2 text-sm">
            <Stat label="Channel" value={top.channel} />
            <Stat label="Category" value={top.category || "—"} />
            <Stat label="Score" value={fmtScore(top.score)} mono />
            <Stat label="Value score" value={String(top.value_score)} mono />
          </div>
        </Card>
      ) : (
        m && (
          <Banner tone="warn">
            <AlertCircle className="mr-1 inline" size={14} /> No actions scored for this member.
          </Banner>
        )
      )}

      {/* Ranked actions */}
      {ranked.length > 0 && (
        <div>
          <SectionTitle sub="Model-scored and orchestrated. Open Assist to explain, draft, re-score, or approve.">
            Ranked actions
          </SectionTitle>
          <div className="space-y-3">
            {ranked.map((a) => (
              <ActionCard
                key={a.action_id}
                action={a}
                threshold={threshold}
                recommended={a.rank === 1}
                onAssist={setAssist}
              />
            ))}
          </div>
        </div>
      )}

      {/* Assist drawer */}
      {m && config.data && reference.data && (
        <AssistDrawer
          open={!!assist}
          onClose={() => setAssist(null)}
          memberId={member}
          member={m}
          action={assist}
          ranked={ranked}
          reference={reference.data}
          config={config.data}
          threshold={threshold}
        />
      )}
    </div>
  );
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-0.5 text-text ${mono ? "tnum font-mono" : "font-medium"}`}>{value}</div>
    </div>
  );
}
