// Typed client for the dashboard query API.
//
// Auth is decoupled: the caller supplies a `getToken()` that returns the
// current Cognito access token (or null). Every request sends it as a Bearer
// token — the API's Cognito authorizer validates it and the Lambda enforces
// operator-group membership. A 401 is surfaced as ApiError with status 401 so
// the UI can trigger re-authentication (the token likely expired).

import type { FleetSettings, FleetStats, RepoConfig, Run, RunsPage, TraceResult } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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
      // The API returns {"error": "..."}; the authorizer's own 401/403 may not.
      let message = resp.statusText;
      try {
        const body = await resp.json();
        if (body && typeof body.error === "string") message = body.error;
      } catch {
        // non-JSON error body (e.g. an authorizer rejection) — keep statusText
      }
      throw new ApiError(resp.status, message);
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
  }): Promise<RepoConfig> {
    return this.request<RepoConfig>("POST", "/admin/repos", { body });
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
}
