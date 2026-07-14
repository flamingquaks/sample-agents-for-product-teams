// Trace view: every run that shares a trace dimension value — the multi-agent
// traceability join (e.g. all agents' runs on a branch, a Jira key, or an
// issue). Reached by clicking a trace chip anywhere in the app.

import { useCallback } from "react";
import { ApiError, type DashboardApi } from "./api";
import { StatusPill } from "./components";
import { fmtCost, fmtDuration, fmtTime, isActive } from "./format";
import { usePolling } from "./hooks";
import type { TraceResult } from "./types";

export function TraceView({
  api,
  dimension,
  value,
  onBack,
  onOpenRun,
  onAuthError,
}: {
  api: DashboardApi;
  dimension: string;
  value: string;
  onBack: () => void;
  onOpenRun: (assignmentId: string) => void;
  onAuthError: () => void;
}) {
  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );

  const poll = usePolling<TraceResult>(() => api.trace(dimension, value), {
    // Keep it live while any run in the group is still active.
    isActive: (res) => res.runs.some((r) => isActive(r.status)),
    deps: [api, dimension, value],
    onError: handleError,
  });

  const res = poll.data;
  const runs = res?.runs ?? [];

  return (
    <div>
      <button onClick={onBack}>← Back to fleet</button>
      <h2>
        Trace: <span className="mono">{dimension}</span> = <span className="mono">{value}</span>
      </h2>
      <p className="muted">
        {res ? `${runs.length} run${runs.length === 1 ? "" : "s"} share this reference` : "Loading…"}
        {res?.truncated && " (sampled from the most recent runs — fleet exceeds the scan cap)"}
      </p>

      {poll.error && !res && <div className="banner error">Failed to load trace: {poll.error}</div>}

      {res && runs.length === 0 ? (
        <p className="muted">No runs currently carry {dimension} = {value}.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>Agent</th>
              <th>Requester</th>
              <th>Source</th>
              <th>Started</th>
              <th>Duration</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.assignment_id}>
                <td>
                  <StatusPill status={r.status} />
                </td>
                <td>
                  <a
                    href="#"
                    onClick={(e) => {
                      e.preventDefault();
                      onOpenRun(r.assignment_id);
                    }}
                  >
                    {r.agent_id ?? "—"}
                  </a>
                </td>
                <td>{r.requester ?? "—"}</td>
                <td>{r.source ?? "—"}</td>
                <td>{fmtTime(r.created_at)}</td>
                <td>{fmtDuration(r.duration_seconds)}</td>
                <td>{fmtCost(r.cost_estimate_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
