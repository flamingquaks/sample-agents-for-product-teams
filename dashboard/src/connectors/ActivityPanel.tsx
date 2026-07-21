// Activity tab (spec §9.3/§9.4): recent dispatches from this connector's source
// plus the block/deny signal (blocked_guardrail* + failed runs surface here, so
// an admin sees rejections without leaving the connector). Read-only.

import { useCallback } from "react";
import { ApiError, type DashboardApi } from "../api";
import { usePolling } from "../hooks";
import type { RunsPage } from "../types";

export function ActivityPanel({
  api,
  source,
  onAuthError,
}: {
  api: DashboardApi;
  source: string;
  onAuthError: () => void;
}) {
  const handleErr = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );
  // Active while any recent run is still dispatched — poll faster then.
  const poll = usePolling<RunsPage>(() => api.listRuns({ source, limit: 25 }), {
    isActive: (d) => (d?.runs ?? []).some((r) => r.status === "dispatched"),
    deps: [api, source],
    onError: handleErr,
  });

  const runs = poll.data?.runs ?? [];
  const denied = runs.filter(
    (r) => r.status === "blocked_guardrail" || r.status === "blocked_guardrail_error",
  ).length;

  const when = (ts?: number) =>
    ts ? new Date(ts * 1000).toLocaleString() : "—";

  return (
    <div>
      <p className="muted">
        Recent dispatches from <b>{source}</b>. Blocked/guardrail rows are the
        rejection signal — {denied} of the last {runs.length} were blocked.
      </p>
      <table>
        <thead>
          <tr><th>When</th><th>Agent</th><th>Requester</th><th>Status</th><th>Summary</th></tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.assignment_id}>
              <td>{when(r.created_at)}</td>
              <td>{r.agent_id || "—"}</td>
              <td><code>{r.requester || "—"}</code></td>
              <td>
                <span className={`pill ${r.status === "completed" ? "ok" : r.status?.startsWith("blocked") || r.status === "failed" ? "err" : "active"}`}>
                  {r.status || "—"}
                </span>
              </td>
              <td>{r.result_summary || "—"}</td>
            </tr>
          ))}
          {runs.length === 0 && !poll.loading && (
            <tr><td colSpan={5} className="muted">No {source} activity yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
