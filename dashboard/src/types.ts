// Shapes returned by the dashboard query API (infra/dashboard). These mirror
// the assignment record the Dispatch Router writes plus the enrichment fields
// (trace_refs, participants). Every field is optional/nullable because records
// predating a given enrichment, or in-flight runs, legitimately omit them — the
// UI must render partial rows gracefully.

/** Terminal + active statuses the router/agents write. */
export type RunStatus =
  | "dispatched"
  | "completed"
  | "failed"
  | "blocked_guardrail"
  | "blocked_guardrail_error"
  | string; // tolerate unknown/future statuses

export interface Participant {
  id: string;
  kind: "requester" | "assignee" | "commenter" | string;
  source: string;
}

/** Open map of traceable identifiers (repo/branch/pr/issue/jira/asana…). */
export type TraceRefs = Record<string, string>;

export interface Run {
  assignment_id: string;
  agent_id?: string;
  source?: string;
  trigger_type?: string;
  requester?: string;
  instruction?: string;
  status?: RunStatus;
  source_context?: Record<string, unknown>;
  trace_refs?: TraceRefs;
  participants?: Participant[];
  created_at?: number;
  completed_at?: number | null;
  duration_seconds?: number | null;
  result_summary?: string | null;
  token_usage?: number | null;
  cost_estimate_usd?: number | null;
}

export interface RunsPage {
  runs: Run[];
  next_token: string | null;
}

export interface TraceResult {
  dimension: string;
  value: string;
  runs: Run[];
  truncated: boolean;
}

export interface FleetStats {
  total: number;
  active: number;
  by_status: Record<string, number>;
  by_agent: Record<string, number>;
  by_source: Record<string, number>;
  truncated: boolean;
}

// --- admin config (fleet-config table, admin API) ---------------------------

/** An onboarded repo. `enabled` = dispatchable; `multi_repo_eligible` = allowed
 *  for cross-repo tool actions (the Gateway policy allowlist). `status` is
 *  "pending" until the tool-call policy sync lands, then "active". */
export interface RepoConfig {
  repo: string;
  enabled: boolean;
  multi_repo_eligible: boolean;
  status?: string;
  onboarded_by?: string;
  onboarded_at?: number;
}

export interface FleetSettings {
  /** When true, only enabled+eligible repos are allowed; when false, any
   *  enabled repo is. */
  restrict_repos: boolean;
}

/** GitHub App registration status for the admin setup panel. */
export interface GitHubAppStatus {
  configured: boolean;
  auth_mode: "pat" | "app" | string;
  slug?: string | null;
  install_url?: string | null;
}

/** The manifest + the GitHub form URL the SPA POSTs it to. */
export interface GitHubManifest {
  manifest: Record<string, unknown>;
  post_url: string;
}
