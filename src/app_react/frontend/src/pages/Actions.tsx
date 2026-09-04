import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Save, Trash2, Info } from "lucide-react";
import { api, ActionRow, Reference } from "../lib/api";
import { DataTable } from "../components/DataTable";
import { KpiCard } from "../components/KpiCard";
import { SeverityPill } from "../components/SeverityPill";
import { Card, Button, Label, Input, Select, Textarea, Banner, SectionTitle } from "../components/ui";
import { Skeleton } from "../components/Skeleton";

export function Actions() {
  const qc = useQueryClient();
  const staged = useQuery({ queryKey: ["actions-staged"], queryFn: api.actionsStaged });
  const reference = useQuery({ queryKey: ["reference"], queryFn: api.reference });
  const [msg, setMsg] = useState<{ tone: "good" | "fail"; text: string } | null>(null);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["actions-staged"] });
  const actions = staged.data ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold text-text">Manage Action Catalog</h1>
        <p className="mt-1 text-sm text-muted">
          Add, edit, or remove actions. Changes are staged here and sync to production after
          reconciliation.
        </p>
      </div>

      <Banner tone="info">
        <Info className="mr-1 inline" size={14} /> This page shows the <b>staging</b> branch
        (app-writes). Changes made here do NOT affect Member Lookup scoring until reconciliation
        runs.
      </Banner>

      {msg && <Banner tone={msg.tone}>{msg.text}</Banner>}

      {staged.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : staged.isError ? (
        <Banner tone="fail">Cannot connect to the app-writes branch.</Banner>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 sm:max-w-xs">
            <KpiCard label="Total actions" value={actions.length} accent="teal" />
          </div>

          <DataTable
            columns={[
              { key: "action_id", header: "ID", mono: true },
              { key: "action_name", header: "Name" },
              { key: "action_category", header: "Category" },
              { key: "team_owner", header: "Team" },
              { key: "value_score", header: "Value", align: "right", mono: true },
              {
                key: "compliance_flag",
                header: "Compliance",
                render: (r) =>
                  r.compliance_flag ? <SeverityPill tone="teal">Yes</SeverityPill> : "No",
              },
              { key: "strategic_priority", header: "Priority", align: "right", mono: true },
              { key: "min_spacing_days", header: "Spacing", align: "right", mono: true },
            ]}
            rows={actions}
            rowKey={(r) => r.action_id}
          />

          {reference.data && (
            <div className="grid gap-6 lg:grid-cols-2">
              <AddAction
                reference={reference.data}
                onDone={(ok) => {
                  setMsg(ok ? { tone: "good", text: "Action added." } : { tone: "fail", text: "Failed to add action." });
                  if (ok) invalidate();
                }}
              />
              <EditAction
                actions={actions}
                reference={reference.data}
                onDone={(text, ok) => {
                  setMsg({ tone: ok ? "good" : "fail", text });
                  if (ok) invalidate();
                }}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ------------------------------ Add -------------------------------------- */
function AddAction({ reference, onDone }: { reference: Reference; onDone: (ok: boolean) => void }) {
  const [actionId, setActionId] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState(reference.categories[0]);
  const [team, setTeam] = useState(reference.teams[0]);
  const [value, setValue] = useState(75);
  const [priority, setPriority] = useState(3);
  const [compliance, setCompliance] = useState(false);
  const [spacing, setSpacing] = useState(30);
  const [description, setDescription] = useState("");
  const [channels, setChannels] = useState<string[]>(["Digital", "Call center"]);

  const mut = useMutation({
    mutationFn: () =>
      api.addAction({
        action_id: actionId,
        action_name: name,
        action_category: category,
        team_owner: team,
        description,
        value_score: value,
        compliance_flag: compliance,
        strategic_priority: priority,
        min_spacing_days: spacing,
        eligible_channels: JSON.stringify(channels),
      }),
    onSuccess: () => {
      onDone(true);
      setActionId("");
      setName("");
      setDescription("");
    },
    onError: () => onDone(false),
  });

  const toggleChannel = (c: string) =>
    setChannels((cs) => (cs.includes(c) ? cs.filter((x) => x !== c) : [...cs, c]));

  return (
    <Card className="p-5">
      <SectionTitle>
        <span className="flex items-center gap-2">
          <Plus size={18} className="text-teal" /> Add new action
        </span>
      </SectionTitle>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>Action ID</Label>
          <Input value={actionId} onChange={(e) => setActionId(e.target.value)} placeholder="ACT013" />
        </div>
        <div>
          <Label>Name</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Annual Wellness Visit" />
        </div>
        <div>
          <Label>Category</Label>
          <Select value={category} onChange={(e) => setCategory(e.target.value)}>
            {reference.categories.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Team owner</Label>
          <Select value={team} onChange={(e) => setTeam(e.target.value)}>
            {reference.teams.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Value score ({value})</Label>
          <input type="range" min={0} max={100} value={value} onChange={(e) => setValue(Number(e.target.value))} className="w-full accent-teal" />
        </div>
        <div>
          <Label>Strategic priority</Label>
          <Select value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
            {[1, 2, 3, 4, 5].map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Min spacing days</Label>
          <Input type="number" min={7} max={365} value={spacing} onChange={(e) => setSpacing(Number(e.target.value))} />
        </div>
        <label className="flex items-end gap-2 pb-2 text-sm text-text">
          <input type="checkbox" checked={compliance} onChange={(e) => setCompliance(e.target.checked)} className="h-4 w-4 accent-teal" />
          Compliance / regulatory
        </label>
      </div>
      <div className="mt-3">
        <Label>Description</Label>
        <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div className="mt-3">
        <Label>Eligible channels</Label>
        <div className="flex flex-wrap gap-2">
          {reference.channels.map((c) => (
            <button
              key={c}
              onClick={() => toggleChannel(c)}
              className={
                "rounded-full border px-3 py-1 text-xs " +
                (channels.includes(c)
                  ? "border-teal/50 bg-teal/10 text-teal"
                  : "border-line bg-panel-2 text-muted")
              }
            >
              {c}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-4">
        <Button
          variant="primary"
          onClick={() => mut.mutate()}
          disabled={mut.isPending || !actionId || !name}
        >
          <Plus size={16} /> Add action
        </Button>
      </div>
    </Card>
  );
}

/* ------------------------------ Edit ------------------------------------- */
function EditAction({
  actions,
  reference,
  onDone,
}: {
  actions: ActionRow[];
  reference: Reference;
  onDone: (text: string, ok: boolean) => void;
}) {
  const [id, setId] = useState("");
  const current = actions.find((a) => a.action_id === id) ?? actions[0];

  const [name, setName] = useState("");
  const [category, setCategory] = useState(reference.categories[0]);
  const [team, setTeam] = useState(reference.teams[0]);
  const [value, setValue] = useState(75);
  const [priority, setPriority] = useState(3);
  const [compliance, setCompliance] = useState(false);
  const [spacing, setSpacing] = useState(30);

  // Sync form when selection changes
  useEffect(() => {
    if (!current) return;
    setId(current.action_id);
    setName(current.action_name ?? "");
    setCategory(current.action_category ?? reference.categories[0]);
    setTeam(current.team_owner ?? reference.teams[0]);
    setValue(Number(current.value_score ?? 75));
    setPriority(Number(current.strategic_priority ?? 3));
    setCompliance(Boolean(current.compliance_flag));
    setSpacing(Number(current.min_spacing_days ?? 30));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.action_id]);

  const save = useMutation({
    mutationFn: () =>
      api.updateAction(current.action_id, {
        action_name: name,
        action_category: category,
        team_owner: team,
        value_score: value,
        strategic_priority: priority,
        compliance_flag: compliance,
        min_spacing_days: spacing,
      }),
    onSuccess: () => onDone(`Action ${current.action_id} updated.`, true),
    onError: () => onDone("Failed to update.", false),
  });

  const del = useMutation({
    mutationFn: () => api.deleteAction(current.action_id),
    onSuccess: () => onDone(`Action ${current.action_id} deleted.`, true),
    onError: () => onDone("Failed to delete.", false),
  });

  if (!current) return null;

  return (
    <Card className="p-5">
      <SectionTitle>
        <span className="flex items-center gap-2">
          <Save size={18} className="text-coral" /> Edit action
        </span>
      </SectionTitle>
      <div className="mb-3">
        <Label>Select action</Label>
        <Select value={current.action_id} onChange={(e) => setId(e.target.value)}>
          {actions.map((a) => (
            <option key={a.action_id} value={a.action_id}>
              {a.action_id} — {a.action_name}
            </option>
          ))}
        </Select>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="col-span-2">
          <Label>Name</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Label>Category</Label>
          <Select value={category} onChange={(e) => setCategory(e.target.value)}>
            {reference.categories.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Team owner</Label>
          <Select value={team} onChange={(e) => setTeam(e.target.value)}>
            {reference.teams.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Value score ({value})</Label>
          <input type="range" min={0} max={100} value={value} onChange={(e) => setValue(Number(e.target.value))} className="w-full accent-teal" />
        </div>
        <div>
          <Label>Strategic priority</Label>
          <Select value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
            {[1, 2, 3, 4, 5].map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Min spacing days</Label>
          <Input type="number" min={7} max={365} value={spacing} onChange={(e) => setSpacing(Number(e.target.value))} />
        </div>
        <label className="flex items-end gap-2 pb-2 text-sm text-text">
          <input type="checkbox" checked={compliance} onChange={(e) => setCompliance(e.target.checked)} className="h-4 w-4 accent-teal" />
          Compliance
        </label>
      </div>
      <div className="mt-4 flex gap-2">
        <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>
          <Save size={16} /> Save changes
        </Button>
        <Button variant="danger" onClick={() => del.mutate()} disabled={del.isPending}>
          <Trash2 size={16} /> Delete
        </Button>
      </div>
    </Card>
  );
}
