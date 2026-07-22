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
  /** Which OTHER repos a dispatch originating here may act on:
   *  "isolated" (only itself), "group" (its repo_group peers), "all". */
  co_repo_mode?: "isolated" | "group" | "all" | string;
  repo_group?: string;
  status?: string;
  onboarded_by?: string;
  onboarded_at?: number;
}

export interface FleetSettings {
  /** When true, only enabled+eligible repos are allowed; when false, any
   *  enabled repo is. */
  restrict_repos: boolean;
}

/** A UI-onboarded agent. The declarative fields are admin-edited; the deploy
 *  fields (image_tag/runtime_arn/build_id/status) are written by the build →
 *  runtime lifecycle. Only enabled + active capabilities route dispatches. */
export interface CapabilityConfig {
  agent_id: string;
  description?: string;
  aliases?: string[];
  /** {source: [event, ...]} — source ∈ github|asana|slack. */
  triggers?: Record<string, string[]>;
  authorization_users?: string[];
  limits?: Record<string, number>;
  /** Per-agent runtime env (e.g. ASANA_PROJECT_GID); values injected at deploy. */
  env?: Record<string, string>;
  /** Config-driven (custom) agent fields (spec §3.2). Built-ins ignore these —
   *  their prompt/deps/grants are code-defined and shown read-only. */
  system_prompt?: string;
  requirements?: string[];
  /** Per-TOOL allowlist, Target___tool ids (§3.5). */
  tool_grants?: string[];
  skills?: SkillRef[];
  /** approved | pending_review — set when the approval gate is on (§7.5). */
  review_status?: string;
  enabled?: boolean;
  /** Seeded system agent — fixed config, enable/disable-only, undeletable (§3.1). */
  builtin?: boolean;
  /** pending | building | active | failed | disabled | deleting. */
  status?: string;
  status_detail?: string;
  image_tag?: string;
  runtime_arn?: string;
  build_id?: string;
  onboarded_by?: string;
  onboarded_at?: number;
  updated_at?: number;
}

/** A skill package reference on a capability (spec §6.1) / in the skills library. */
export interface SkillRef {
  name: string;
  s3_prefix: string;
  sha256?: string;
  scope?: "capability" | "shared" | string;
}

/** A grantable tool in the fleet catalog (spec §3.5), for the authoring picker.
 *  Destructive tools are excluded server-side — never grantable. */
export interface ToolCatalogEntry {
  action_id: string;
  target: string;
  tool: string;
  klass: "read" | "write";
}

/** GitHub App registration status for the admin setup panel. */
export interface GitHubAppStatus {
  configured: boolean;
  slug?: string | null;
  install_url?: string | null;
}

/** The manifest + the GitHub form URL the SPA POSTs it to. */
export interface GitHubManifest {
  manifest: Record<string, unknown>;
  post_url: string;
}

// --- Connectors: Slack workspaces, channel policy, trigger rules, requests ---

/** A Slack workspace onboarded for the fleet. */
export interface SlackWorkspace {
  team_id: string;
  team_name?: string;
  enabled: boolean;
  default_channel_policy: "allowlist" | "denylist";
  status: "pending" | "active" | "disabled";
  onboarded_by?: string;
  onboarded_at?: number;
}

/** A per-workspace channel allow/deny row (the WHERE axis). */
export interface ChannelPolicy {
  team_id: string;
  channel_id: string;
  channel_name?: string;
  mode: "allow" | "deny";
  note?: string;
}

/** A WHO grant rule: subject → agent → workspace, permit or forbid. */
export interface TriggerRule {
  rule_id: string;
  connector: "slack" | "asana" | "github";
  subject_type: "user" | "group";
  subject_id: string;
  agent_id: string;
  workspace: string;
  effect: "permit" | "forbid";
  created_by?: string;
  created_at?: number;
}

/** A channel onboarding request awaiting admin approval. */
export interface ChannelRequest {
  request_id: string;
  team_id: string;
  channel_id: string;
  channel_name?: string;
  requested_by: string;
  requested_agents: string[];
  status: "pending" | "approved" | "denied";
  created_at?: number;
  decided_by?: string;
  decided_at?: number | null;
}

// --- Part II: identity map, permission groups, notifications (spec §16–§18) ---

/** A cross-source person record. Email is the golden join id; handles per source. */
export interface Identity {
  identity_id: string;
  email: string;
  display_name?: string;
  handles: {
    github?: string;
    asana?: string;
    slack?: Record<string, string>; // team_id -> user_id
    sdlc?: string;
  };
  handle_keys?: string[];
  groups: string[];
  verified?: Record<string, boolean>;
  status: "pending" | "active" | "disabled";
  onboarded_by?: string;
  created_from?: { source?: string; handle?: string; at?: number };
  created_at?: number;
  updated_at?: number;
  merged_from?: string[];
}

/** A user-onboarding request awaiting admin approval (first-touch gate). */
export interface UserRequest {
  request_id: string;
  identity_id: string;
  source: string;
  source_context?: Record<string, unknown>;
  proposed_email?: string;
  display_name?: string;
  status: "pending" | "approved" | "denied";
  created_at?: number;
  decided_by?: string;
  decided_at?: number | null;
}

/** A permission group — named metadata; access via group-scoped trigger rules. */
export interface PermGroup {
  group_id: string;
  name: string;
  description?: string;
  recommended?: boolean;
  member_count?: number;
  members?: { identity_id: string; email: string; display_name?: string }[];
  created_by?: string;
  created_at?: number;
}

/** A channel's notification subscription (self-served via /sdlc-notify). */
export interface NotifSub {
  team_id: string;
  channel_id: string;
  repos: string[];
  tiers: { actionable?: string[]; informative?: string[]; error?: string[] };
  min_severity: "informative" | "actionable" | "error";
  created_by?: string;
  created_at?: number;
  updated_at?: number;
}
