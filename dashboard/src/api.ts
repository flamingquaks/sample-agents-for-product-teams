// Typed client for the dashboard query API.
//
// Auth is decoupled: the caller supplies a `getToken()` that returns the
// current Cognito access token (or null). Every request sends it as a Bearer
// token — the API's Cognito authorizer validates it and the Lambda enforces
// operator-group membership. A 401 is surfaced as ApiError with status 401 so
// the UI can trigger re-authentication (the token likely expired).

import type {
  CapabilityConfig,
  ChannelPolicy,
  ChannelRequest,
  FleetSettings,
  FleetStats,
  GitHubAppStatus,
  GitHubAvailableRepos,
  GitHubManifest,
  Identity,
  NotifSub,
  PermGroup,
  RepoConfig,
  Run,
  RunsPage,
  SkillRef,
  SlackWorkspace,
  ToolCatalogEntry,
  TraceResult,
  TriggerRule,
  UserRequest,
} from "./types";

export class ApiError extends Error {
  status: number;
  /** The parsed error response body, when the server returned JSON. Carries
   *  extra fields like `install_url` on a 409 the UI can act on. */
  body: Record<string, unknown> | null;
  constructor(status: number, message: string, body: Record<string, unknown> | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export type TokenGetter = () => string | null | undefined;

export interface ListRunsParams {
  limit?: number;
  next_token?: string;
  agent_id?: string;
  status?: string;
  source?: string;
  requester?: string;
}

export class DashboardApi {
  constructor(
    private readonly baseUrl: string,
    private readonly getToken: TokenGetter,
  ) {}

  /** Core request: attaches the Bearer token, sends an optional JSON body, and
   *  normalizes errors to ApiError (status 0 = network/CORS). Shared by the
   *  read GETs and the admin write verbs. */
  private async request<T>(
    method: string,
    path: string,
    opts: { query?: Record<string, string | number | undefined>; body?: unknown } = {},
  ): Promise<T> {
    const url = new URL(this.baseUrl + path);
    if (opts.query) {
      for (const [k, v] of Object.entries(opts.query)) {
        if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
      }
    }
    const token = this.getToken();
    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    let resp: Response;
    try {
      resp = await fetch(url.toString(), {
        method,
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      });
    } catch (e) {
      // Network/CORS failure — distinct from an HTTP error status.
      throw new ApiError(0, `network error: ${(e as Error).message}`);
    }
    if (!resp.ok) {
      // The API returns {"error": "..."}; API Gateway / the Cognito authorizer's
      // own 401/403 instead return {"message": "..."}. statusText is empty over
      // HTTP/2 (CloudFront/API Gateway), so never fall back to it alone — always
      // end with a non-empty, status-bearing message the UI can show.
      let message = "";
      let body: Record<string, unknown> | null = null;
      try {
        body = await resp.json();
        if (body && typeof body.error === "string") message = body.error;
        else if (body && typeof body.message === "string") message = body.message;
      } catch {
        // non-JSON error body (e.g. an authorizer rejection) — fall through.
      }
      if (!message) message = resp.statusText || `HTTP ${resp.status}`;
      throw new ApiError(resp.status, message, body);
    }
    // 204/empty bodies (rare) → undefined cast; JSON otherwise.
    const text = await resp.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  private get<T>(path: string, query?: Record<string, string | number | undefined>): Promise<T> {
    return this.request<T>("GET", path, { query });
  }

  // --- read API --------------------------------------------------------------

  listRuns(params: ListRunsParams = {}): Promise<RunsPage> {
    return this.get<RunsPage>("/runs", { ...params });
  }

  getRun(assignmentId: string): Promise<Run> {
    return this.get<Run>(`/runs/${encodeURIComponent(assignmentId)}`);
  }

  trace(dimension: string, value: string): Promise<TraceResult> {
    return this.get<TraceResult>("/trace", { dim: dimension, value });
  }

  stats(): Promise<FleetStats> {
    return this.get<FleetStats>("/stats");
  }

  // --- admin API (admins group; write) --------------------------------------

  listRepos(): Promise<{ repos: RepoConfig[] }> {
    return this.get<{ repos: RepoConfig[] }>("/admin/repos");
  }

  onboardRepo(body: {
    repo: string;
    enabled?: boolean;
    multi_repo_eligible?: boolean;
    co_repo_mode?: "isolated" | "group" | "all";
    repo_group?: string;
  }): Promise<RepoConfig> {
    return this.request<RepoConfig>("POST", "/admin/repos", { body });
  }

  /** Batch onboard: all repos share the same access mode; ONE policy sync
   *  server-side, and the batch is atomic (a verify failure writes nothing). */
  onboardRepos(body: {
    repos: string[];
    co_repo_mode?: "isolated" | "group" | "all";
    repo_group?: string;
  }): Promise<{ repos: RepoConfig[]; policy_sync_warning?: string }> {
    return this.request("POST", "/admin/repos", { body });
  }

  /** Everything the GitHub App can reach right now — the onboarding picker's
   *  source. Live from GitHub (installations + per-installation repo lists). */
  gitHubAppRepos(): Promise<GitHubAvailableRepos> {
    return this.get<GitHubAvailableRepos>("/admin/github-app/repos");
  }

  deleteRepo(repo: string): Promise<{ repo: string; deleted: boolean }> {
    // The delete route is a greedy {repo+} path param, so the slash in
    // "owner/repo" must stay a literal path separator — encode each segment but
    // keep the "/" between them (encodeURIComponent on the whole string would
    // send "%2F", which a single/greedy path param does not match reliably).
    const path = repo
      .split("/")
      .map(encodeURIComponent)
      .join("/");
    return this.request("DELETE", `/admin/repos/${path}`);
  }

  getSettings(): Promise<FleetSettings> {
    return this.get<FleetSettings>("/admin/settings");
  }

  putSettings(body: FleetSettings): Promise<FleetSettings> {
    return this.request<FleetSettings>("PUT", "/admin/settings", { body });
  }

  // --- capabilities (UI-onboarded agents) ------------------------------------

  listCapabilities(): Promise<{ capabilities: CapabilityConfig[] }> {
    return this.get<{ capabilities: CapabilityConfig[] }>("/admin/capabilities");
  }

  onboardCapability(body: {
    agent_id: string;
    description?: string;
    aliases?: string[];
    triggers?: Record<string, string[]>;
    limits?: Record<string, number>;
    env?: Record<string, string>;
    // Config-driven (custom) agent fields (§3.2). Ignored for built-ins, which
    // accept only `enabled`.
    system_prompt?: string;
    requirements?: string[];
    tool_grants?: string[];
    skills?: SkillRef[];
    enabled?: boolean;
  }): Promise<CapabilityConfig> {
    return this.request<CapabilityConfig>("POST", "/admin/capabilities", { body });
  }

  /** Delete a CUSTOM agent: de-routes immediately and hands teardown to the
   *  deployer (§8.2), so the row lands in `deleting` rather than being gone. */
  deleteCapability(
    agentId: string,
  ): Promise<{ agent_id: string; status: string }> {
    return this.request("DELETE", `/admin/capabilities/${encodeURIComponent(agentId)}`);
  }

  /** Clone any capability (built-in or custom) into a new editable custom agent
   *  (§8.2). The new id comes from `newAgentId`. */
  cloneCapability(sourceId: string, newAgentId: string): Promise<CapabilityConfig> {
    return this.request<CapabilityConfig>(
      "POST",
      `/admin/capabilities/${encodeURIComponent(sourceId)}/clone`,
      { body: { new_agent_id: newAgentId } },
    );
  }

  /** Second-admin approval of a pending_review custom agent (§7.5). The caller
   *  must differ from the author (enforced server-side). */
  approveCapability(agentId: string): Promise<CapabilityConfig> {
    return this.request<CapabilityConfig>(
      "POST",
      `/admin/capabilities/${encodeURIComponent(agentId)}/approve`,
      { body: {} },
    );
  }

  /** The grantable tool catalog for the authoring picker (§3.5). Read/write only
   *  — destructive tools are excluded server-side. */
  toolCatalog(): Promise<{ tools: ToolCatalogEntry[] }> {
    return this.get<{ tools: ToolCatalogEntry[] }>("/admin/tool-catalog");
  }

  // --- skills (spec §6) ------------------------------------------------------

  listSkills(): Promise<{ skills: SkillRef[] }> {
    return this.get<{ skills: SkillRef[] }>("/admin/skills");
  }

  /** Upload a raw SKILL.md. */
  uploadSkillMd(content: string, scope = "shared"): Promise<SkillRef> {
    return this.request<SkillRef>("POST", "/admin/skills", { body: { content, scope } });
  }

  /** Upload a .zip skill package (base64). Validated + expanded by the isolated
   *  unpacker Lambda server-side (§6.3). */
  uploadSkillZip(zipBase64: string, scope = "shared"): Promise<SkillRef> {
    return this.request<SkillRef>("POST", "/admin/skills", {
      body: { zip_base64: zipBase64, scope },
    });
  }

  deleteSkill(scope: string, name: string): Promise<{ name: string; scope: string; deleted: boolean }> {
    return this.request(
      "DELETE",
      `/admin/skills/${encodeURIComponent(scope)}/${encodeURIComponent(name)}`,
    );
  }

  // --- GitHub App setup (manifest flow) --------------------------------------

  gitHubAppStatus(): Promise<GitHubAppStatus> {
    return this.get<GitHubAppStatus>("/admin/github-app/status");
  }

  gitHubAppManifest(org?: string): Promise<GitHubManifest> {
    // Sent as query so a plain GET works; org optional (org- vs user-owned app).
    return this.get<GitHubManifest>("/admin/github-app/setup/manifest", {
      org: org || undefined,
    });
  }

  gitHubAppExchange(code: string): Promise<{ app_id: string; slug: string }> {
    return this.request("POST", "/admin/github-app/setup/callback", {
      body: { code },
    });
  }

  // --- Slack connector -------------------------------------------------------

  listSlackWorkspaces(): Promise<{ workspaces: SlackWorkspace[] }> {
    return this.get<{ workspaces: SlackWorkspace[] }>("/admin/slack/workspaces");
  }

  onboardSlackWorkspace(body: {
    team_id: string;
    team_name?: string;
    default_channel_policy?: "allowlist" | "denylist";
    enabled?: boolean;
  }): Promise<SlackWorkspace> {
    return this.request<SlackWorkspace>("POST", "/admin/slack/workspaces", { body });
  }

  deleteSlackWorkspace(teamId: string): Promise<{ team_id: string; deleted: boolean }> {
    return this.request("DELETE", `/admin/slack/workspaces/${encodeURIComponent(teamId)}`);
  }

  /** One-step connect: verify token + store secrets + onboard workspace. */
  connectSlackWorkspace(body: {
    bot_token: string;
    signing_secret: string;
    default_channel_policy?: "allowlist" | "denylist";
  }): Promise<SlackWorkspace> {
    return this.request<SlackWorkspace>("POST", "/admin/slack/workspaces/connect", { body });
  }

  slackManifest(appName?: string): Promise<{ manifest: unknown }> {
    // The manifest is app-level (not per-workspace); `app` is a path placeholder
    // so the route parallels github-app/setup/manifest.
    return this.get<{ manifest: unknown }>("/admin/slack/workspaces/app/manifest", {
      app_name: appName,
    });
  }

  listChannels(teamId: string): Promise<{ channels: ChannelPolicy[] }> {
    return this.get<{ channels: ChannelPolicy[] }>("/admin/slack/channels", { team_id: teamId });
  }

  putChannelPolicy(body: {
    team_id: string;
    channel_id: string;
    mode: "allow" | "deny";
    channel_name?: string;
    note?: string;
  }): Promise<ChannelPolicy> {
    return this.request<ChannelPolicy>("POST", "/admin/slack/channels", { body });
  }

  deleteChannelPolicy(teamId: string, channelId: string): Promise<{ deleted: boolean }> {
    return this.request(
      "DELETE",
      `/admin/slack/channels/${encodeURIComponent(teamId)}/${encodeURIComponent(channelId)}`,
    );
  }

  // --- trigger rules (WHO grants) + access simulator -------------------------

  listTriggerRules(connector?: string): Promise<{ rules: TriggerRule[] }> {
    return this.get<{ rules: TriggerRule[] }>("/admin/trigger-rules", { connector });
  }

  createTriggerRule(body: {
    connector: string;
    subject_type: "user" | "group";
    subject_id: string;
    agent_id?: string;
    workspace?: string;
    effect?: "permit" | "forbid";
  }): Promise<TriggerRule> {
    return this.request<TriggerRule>("POST", "/admin/trigger-rules", { body });
  }

  deleteTriggerRule(ruleId: string): Promise<{ rule_id: string; deleted: boolean }> {
    return this.request("DELETE", `/admin/trigger-rules/${encodeURIComponent(ruleId)}`);
  }

  simulateAccess(body: {
    principal: string;
    agent_id: string;
    workspace?: string;
    channel_id?: string;
    principal_groups?: string[];
  }): Promise<{ decision: "ALLOW" | "DENY"; reason: string }> {
    return this.request("POST", "/admin/trigger-rules/simulate", { body });
  }

  // --- channel onboarding requests (approve/deny queue) ----------------------

  listChannelRequests(status?: string): Promise<{ requests: ChannelRequest[] }> {
    return this.get<{ requests: ChannelRequest[] }>("/admin/channel-requests", { status });
  }

  approveChannelRequest(
    requestId: string,
    approvedAgents?: string[],
  ): Promise<{ request: ChannelRequest; created_rules: string[] }> {
    return this.request("POST", `/admin/channel-requests/${encodeURIComponent(requestId)}/approve`, {
      body: approvedAgents ? { approved_agents: approvedAgents } : {},
    });
  }

  denyChannelRequest(requestId: string): Promise<{ request: ChannelRequest }> {
    return this.request("POST", `/admin/channel-requests/${encodeURIComponent(requestId)}/deny`, {
      body: {},
    });
  }

  // --- identities (cross-source user map, spec §16) --------------------------

  listIdentities(): Promise<{ identities: Identity[] }> {
    return this.get<{ identities: Identity[] }>("/admin/identities");
  }

  createIdentity(body: Partial<Identity>): Promise<Identity> {
    return this.request<Identity>("POST", "/admin/identities", { body });
  }

  updateIdentity(
    identityId: string,
    body: { groups?: string[]; status?: string },
  ): Promise<Identity> {
    return this.request<Identity>(
      "PUT",
      `/admin/identities/${encodeURIComponent(identityId)}`,
      { body },
    );
  }

  deleteIdentity(identityId: string): Promise<{ deleted: boolean }> {
    return this.request("DELETE", `/admin/identities/${encodeURIComponent(identityId)}`);
  }

  // --- user-onboarding requests (approve/deny queue, spec §16.4) -------------

  listUserRequests(status?: string): Promise<{ requests: UserRequest[] }> {
    return this.get<{ requests: UserRequest[] }>("/admin/user-requests", { status });
  }

  approveUserRequest(
    requestId: string,
    groups: string[],
  ): Promise<{ request: UserRequest; identity_id: string; groups: string[] }> {
    return this.request("POST", `/admin/user-requests/${encodeURIComponent(requestId)}/approve`, {
      body: { groups },
    });
  }

  denyUserRequest(requestId: string): Promise<{ request: UserRequest }> {
    return this.request("POST", `/admin/user-requests/${encodeURIComponent(requestId)}/deny`, {
      body: {},
    });
  }

  // --- permission groups (spec §17) ------------------------------------------

  listGroups(): Promise<{ groups: PermGroup[] }> {
    return this.get<{ groups: PermGroup[] }>("/admin/groups");
  }

  createGroup(body: Partial<PermGroup>): Promise<PermGroup> {
    return this.request<PermGroup>("POST", "/admin/groups", { body });
  }

  getGroup(groupId: string): Promise<PermGroup> {
    return this.get<PermGroup>(`/admin/groups/${encodeURIComponent(groupId)}`);
  }

  deleteGroup(groupId: string): Promise<{ deleted: boolean }> {
    return this.request("DELETE", `/admin/groups/${encodeURIComponent(groupId)}`);
  }

  // --- notification subscriptions (read/edit, spec §18.5) --------------------

  listNotifSubs(teamId?: string): Promise<{ subscriptions: NotifSub[] }> {
    return this.get<{ subscriptions: NotifSub[] }>("/admin/notif-subs", { team_id: teamId });
  }

  upsertNotifSub(body: Partial<NotifSub>): Promise<NotifSub> {
    return this.request<NotifSub>("POST", "/admin/notif-subs", { body });
  }

  deleteNotifSub(teamId: string, channelId: string): Promise<{ deleted: boolean }> {
    return this.request(
      "DELETE",
      `/admin/notif-subs/${encodeURIComponent(teamId)}/${encodeURIComponent(channelId)}`,
    );
  }
}
