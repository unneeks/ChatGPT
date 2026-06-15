/**
 * Thin API client over the reqsmith reviewer endpoints.
 * The token is stored in sessionStorage; in dev mode the backend accepts any header.
 */

const BASE = import.meta.env.VITE_API_BASE ?? "";

function token(): string {
  return sessionStorage.getItem("reviewer_token") ?? "";
}

function headers(): HeadersInit {
  return {
    "Content-Type": "application/json",
    Authorization: token() ? `Bearer ${token()}` : "",
  };
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: headers() });
  if (!res.ok) throw new APIError(res.status, await res.text());
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: headers(),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new APIError(res.status, await res.text());
  return res.json() as Promise<T>;
}

export class APIError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "APIError";
  }
}

// --- Typed responses ---

export interface QueueItem {
  run_id: string;
  jira_issue_key: string;
  state: string;
  risk_tier: string;
  waiting_for: string;
  draft_version: number | null;
  draft_prompt_version: string | null;
  draft_model_id: string | null;
  updated_at: string;
}

export interface QueueResponse {
  queue: QueueItem[];
  total: number;
}

export interface ResolvedCitation {
  citation_id: string;
  claim_path: string;
  source_document_id: string;
  source_origin: string | null;
  source_external_ref: string | null;
  span_start: number;
  span_end: number;
  span_text: string;
  entailment_verdict: string | null;
  source_full_text: string | null;
}

export interface GateRow {
  layer: number;
  rule_id: string;
  verdict: string;
  score: number | null;
  reasoning: string | null;
  policy_version: string;
}

export interface ApprovalRow {
  role: string;
  decision: string;
  reviewer_identity: string;
  has_diff: boolean;
  at: string;
}

export interface BundleResponse {
  run: {
    id: string;
    jira_issue_key: string;
    state: string;
    risk_tier: string;
    prompt_pack_version: string;
    policy_version: string;
  };
  artifact: {
    id: string;
    kind: string;
    version: number;
    content_hash: string;
    prompt_version: string;
    model_id: string;
    policy_version: string;
    content: Record<string, unknown>;
  };
  citations: ResolvedCitation[];
  gates: GateRow[];
  judge_scores: string | null;
  approvals: ApprovalRow[];
  audit_event_count: number | null;
}

export interface AuditEvent {
  id: number;
  at: string;
  actor: string;
  action: string;
  prompt_version: string | null;
  model_id: string | null;
  policy_version: string | null;
  detail: Record<string, unknown> | null;
}

export interface AuditResponse {
  run_id: string;
  jira_issue_key: string;
  state: string;
  events: (AuditEvent & { seq: number; input_hash: string | null; output_hash: string | null })[];
}

export interface DecisionResponse {
  status: string;
  run_id: string;
  job_id?: string;
}

// --- API calls ---

export const api = {
  queue: () => get<QueueResponse>("/reviewer/queue"),

  bundle: (runId: string) =>
    get<BundleResponse>(`/reviewer/runs/${runId}/bundle`),

  auditReplay: (runId: string) =>
    get<AuditResponse>(`/runs/${runId}/audit`),

  decide: (runId: string, body: { decision: string; diff?: Record<string, unknown>; note?: string }) =>
    post<DecisionResponse>(`/reviewer/runs/${runId}/decision`, body),

  eventsUrl: (runId: string, sinceId = 0) =>
    `${BASE}/reviewer/runs/${runId}/events?since_id=${sinceId}${token() ? `&token=${token()}` : ""}`,

  outreach: () => get<OutreachMonitorResponse>("/reviewer/outreach"),

  pauseOutreach: (paused: boolean, reason = "manual") =>
    post<{ outreach_paused: boolean; reason: string }>("/outreach/pause", undefined)
      .then(() => fetch(`${BASE}/outreach/pause?paused=${paused}&reason=${encodeURIComponent(reason)}`, {
        method: "POST", headers: headers(),
      }).then(r => r.json())),

  setToken: (t: string) => sessionStorage.setItem("reviewer_token", t),
  getToken: () => sessionStorage.getItem("reviewer_token") ?? "",
};

// --- Outreach Monitor types ---

export interface KillSwitch {
  paused: boolean;
  reason: string | null;
}

export interface OutreachQuestion {
  question_id: string;
  run_id: string;
  issue_key: string | null;
  status: string;
  current_rung: number;
  sla_deadline: string | null;
  sla_overdue: boolean;
  stakeholder_aad_id: string | null;
}

export interface OutreachEvent {
  question_id: string;
  channel: string;
  direction: string;
  external_message_id: string | null;
  idempotency_key: string;
  created_at: string;
}

export interface StakeholderBudget {
  aad_id: string;
  chats_today: number;
  chats_limit: number;
  meetings_this_week: number;
  meetings_limit: number;
}

export interface OutreachMonitorResponse {
  kill_switch: KillSwitch;
  questions: OutreachQuestion[];
  recent_events: OutreachEvent[];
  budget: {
    stakeholders: StakeholderBudget[];
    global_sent_today: number;
    global_limit: number;
  };
}
