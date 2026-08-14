// Shapes returned by the dashboard query API (infra/dashboard). These mirror
// the assignment record the Dispatch Router writes plus the enrichment fields
// (trace_refs, participants). Every field is optional/nullable because records
// predating a given enrichment, or in-flight runs, legitimately omit them — the
// UI must render partial rows gracefully.

/** Terminal + active statuses the router/agents write. awaiting_input /
 * resuming / timed_out are the durable pause lifecycle (an agent asked the
 * requester a question and checkpointed; see durable-repo-work spec). */
export type RunStatus =
  | "dispatched"
  | "completed"
  | "failed"
  | "blocked_guardrail"
  | "blocked_guardrail_error"
  | "awaiting_input"
  | "resuming"
  | "timed_out"
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
  // Durable pause fields (status === "awaiting_input"; durable-repo-work spec):
  // the question the agent asked and where its work is checkpointed.
  pending_question?: string | null;
  workspace_snapshot?: { repo: string; branch: string; sha: string }[] | null;
  paused_at?: number | null;
  // A follow-up started by replying on a completed thread links its prior run.
  parent_assignment_id?: string | null;
  // Turn-by-turn record of the run: dispatched → (question → reply)* →
  // result | error. Appended by the router/agent/sweeper alongside the status
  // writes that produce each turn.
  timeline?: TimelineEvent[] | null;
  // Every commit the run pushed to its wip branch, with the files it touched.
  commits?: CommitRecord[] | null;
}

export interface TimelineEvent {
  ts: number;
  kind: "dispatched" | "question" | "reply" | "result" | "error" | string;
  actor: string;
  text: string;
}

export interface CommitRecord {
  ts: number;
  repo: string;
  branch: string;
  sha: string;
  message: string;
  files: string[];
  // files is capped at 100 entries; this preserves the true count.
  files_total?: number;
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

/** A repo an App installation can reach (the onboarding picker's rows). */
export interface GitHubAvailableRepo {
  repo: string;
  private: boolean;
  /** Already onboarded to the fleet — shown but not re-selectable. */
  onboarded: boolean;
}

/** One App installation + its reachable repos. */
export interface GitHubInstallation {
  installation_id: number;
  owner: string;
  owner_type: string;
  repos: GitHubAvailableRepo[];
}

/** GET /admin/github-app/repos — everything the App can reach right now. */
export interface GitHubAvailableRepos {
  configured: boolean;
  installations: GitHubInstallation[];
  install_url?: string | null;
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
  connector: "slack" | "asana" | "github" | "jira" | "confluence";
  subject_type: "user" | "group";
  subject_id: string;
  /** Human label for subject_id (person name / #channel / group name), server-resolved. */
  subject_label?: string;
  agent_id: string;
  workspace: string;
  /** Human workspace name for a concrete (non-"*") workspace, server-resolved. */
  workspace_label?: string;
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
  /** #channel-name fallback when channel_name is unset, server-resolved. */
  channel_label?: string;
  requested_by: string;
  /** Human name for requested_by (display name / email), server-resolved. */
  requested_by_label?: string;
  /** Human workspace name, server-resolved. */
  workspace_label?: string;
  requested_agents: string[];
  /** Repos the requester asked to work on from this channel (spec §19). */
  requested_repos?: string[];
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
  /** #channel-name, server-resolved (falls back to the raw id). */
  channel_label?: string;
  /** Human workspace name, server-resolved. */
  workspace_label?: string;
  repos: string[];
  /** Jira project / Confluence space scopes (atlassian-connector §A9.1). */
  projects?: string[];
  spaces?: string[];
  tiers: { actionable?: string[]; informative?: string[]; error?: string[] };
  min_severity: "informative" | "actionable" | "error";
  created_by?: string;
  created_at?: number;
  updated_at?: number;
}

// --- Atlassian connector (docs/specs/atlassian-connector-spec.md) ------------

/** One Atlassian site (cloud id) covering both products (§A6.1). */
export interface AtlassianSite {
  site_id: string;
  site_url: string;
  site_name?: string;
  enabled: boolean;
  products: { jira?: boolean; confluence?: boolean };
  bot_account_id?: string;
  bot_email?: string;
  forge_app_id?: string;
  webhook_last_seen?: { jira?: number | null; confluence?: number | null };
  token_expires_at?: number | null;
  default_project_policy: "allowlist" | "denylist";
  default_space_policy: "allowlist" | "denylist";
  status: "pending" | "active" | "disabled";
  onboarded_by?: string;
  onboarded_at?: number;
}

/** A Jira project allow/deny row + its linked-repo co-scope (§B1.2). */
export interface JiraProject {
  site_id: string;
  project_key: string;
  project_name?: string;
  mode: "allow" | "deny";
  repos: string[];
  note?: string;
  created_by?: string;
  created_at?: number;
}

/** A Confluence space row — the WHERE axis AND the write-safety axis (§C1.2). */
export interface ConfluenceSpace {
  site_id: string;
  space_key: string;
  space_name?: string;
  mode: "allow" | "deny";
  write_mode: "direct" | "propose";
  write_agents: string[];
  repos: string[];
  note?: string;
  created_by?: string;
  created_at?: number;
}

/** A data-driven event → agent automation rule (§A8.1). */
export interface AutomationRule {
  rule_id: string;
  connector: "jira" | "confluence" | "github";
  enabled: boolean;
  event: string;
  match: Record<string, unknown>;
  action: { agent_id: string; instruction_template: string };
  cooldown_seconds: number;
  created_by?: string;
  created_at?: number;
  updated_at?: number;
  last_fired_at?: number | null;
  fire_count?: number;
}

/** A person's DM notification preference (§A9.2). */
export interface NotifPref {
  identity_id: string;
  tiers: { actionable?: string[]; informative?: string[]; error?: string[] };
  min_tier: "informative" | "actionable" | "error";
  created_at?: number;
  updated_at?: number;
}
