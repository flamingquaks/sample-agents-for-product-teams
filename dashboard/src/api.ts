// Typed client for the dashboard query API.
//
// Auth is decoupled: the caller supplies a `getToken()` that returns the
// current Cognito access token (or null). Every request sends it as a Bearer
// token — the API's Cognito authorizer validates it and the Lambda enforces
// operator-group membership. A 401 is surfaced as ApiError with status 401 so
// the UI can trigger re-authentication (the token likely expired).

import type { FleetStats, Run, RunsPage, TraceResult } from "./types";

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

  private async get<T>(path: string, query?: Record<string, string | number | undefined>): Promise<T> {
    const url = new URL(this.baseUrl + path);
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
      }
    }
    const token = this.getToken();
    let resp: Response;
    try {
      resp = await fetch(url.toString(), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
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
    return (await resp.json()) as T;
  }

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
}
