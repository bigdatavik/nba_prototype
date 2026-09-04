// Typed fetch layer. All config/hosts/ids come from /api/config at runtime —
// nothing about the workspace is baked into this bundle.

export interface AppConfig {
  lakebase_project: string;
  lakebase_database: string;
  lakebase_schema: string;
  branch_production: string;
  branch_app_writes: string;
  model_endpoint_name: string;
  genie_space_id: string;
  genie_enabled: boolean;
  llm_endpoint_name: string;
  llm_enabled: boolean;
  user: string;
}

export interface Reference {
  categories: string[];
  teams: string[];
  channels: string[];
  priority_threshold: number;
  ask_nba_suggestions: string[];
}

export type Member = Record<string, any>;

export interface Trajectory {
  label: string;
  current: number;
  without: number;
  with_new: number;
  lift_pct: number;
}

export interface RankedAction {
  rank: number;
  action_id: string;
  action_name: string;
  category: string;
  team: string;
  score: number;
  channel: string;
  value_score: number;
  compliance: boolean;
  description: string;
  band: string;
  explain: string[];
  trajectory: Trajectory;
}

export interface ScoreResponse {
  member: Member;
  ranked: RankedAction[];
  priority_threshold: number;
}

export interface WhatIfResponse {
  ranked: RankedAction[];
  priority_threshold: number;
}

export interface ActionRow {
  action_id: string;
  action_name: string;
  action_category?: string;
  team_owner?: string;
  description?: string;
  value_score?: number;
  compliance_flag?: boolean;
  strategic_priority?: number;
  min_spacing_days?: number;
  eligible_channels?: string | string[];
  [k: string]: any;
}

export interface Decision {
  decision_id: number;
  member_id: string;
  action_id?: string;
  action_name?: string;
  channel?: string;
  recommended_score?: number;
  status?: string;
  disposition?: string;
  outcome?: string;
  note?: string;
  approver?: string;
  created_at?: string;
}

export interface AskResponse {
  conversation_id: string | null;
  answer: string;
  sql: string;
  columns: string[] | null;
  rows: any[][] | null;
  error: string | null;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* ignore */
    }
    const err = new Error(detail) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return res.json() as Promise<T>;
}

export const api = {
  config: () => req<AppConfig>("/api/config"),
  reference: () => req<Reference>("/api/reference"),
  members: () => req<{ members: string[] }>("/api/members").then((r) => r.members),
  member: (id: string) => req<Member>(`/api/members/${encodeURIComponent(id)}`),
  score: (id: string) => req<ScoreResponse>(`/api/members/${encodeURIComponent(id)}/score`),
  whatif: (id: string, body: Record<string, unknown>) =>
    req<WhatIfResponse>(`/api/members/${encodeURIComponent(id)}/whatif`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  draftOutreach: (body: { member_id: string; action_id: string; channel: string }) =>
    req<{ message: string }>("/api/draft-outreach", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  actions: () => req<{ actions: ActionRow[] }>("/api/actions").then((r) => r.actions),
  actionsStaged: () =>
    req<{ actions: ActionRow[] }>("/api/actions/staged").then((r) => r.actions),
  addAction: (data: Record<string, unknown>) =>
    req<{ ok: boolean }>("/api/actions", { method: "POST", body: JSON.stringify(data) }),
  updateAction: (id: string, updates: Record<string, unknown>) =>
    req<{ ok: boolean }>(`/api/actions/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(updates),
    }),
  deleteAction: (id: string) =>
    req<{ ok: boolean }>(`/api/actions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  changeLog: () => req<{ rows: Record<string, any>[] }>("/api/change-log").then((r) => r.rows),
  decisions: (memberId?: string) =>
    req<{ decisions: Decision[] }>(
      `/api/decisions${memberId ? `?member_id=${encodeURIComponent(memberId)}` : ""}`
    ).then((r) => r.decisions),
  recordDecision: (body: Record<string, unknown>) =>
    req<{ ok: boolean }>("/api/decisions", { method: "POST", body: JSON.stringify(body) }),
  updateOutcome: (id: number, outcome: string) =>
    req<{ ok: boolean }>(`/api/decisions/${id}/outcome`, {
      method: "PATCH",
      body: JSON.stringify({ outcome }),
    }),
  askNba: (question: string, conversationId?: string | null) =>
    req<AskResponse>("/api/ask-nba", {
      method: "POST",
      body: JSON.stringify({ question, conversation_id: conversationId ?? null }),
    }),
};
