// Trace view: every run that shares a trace dimension value — the multi-agent
// traceability join (e.g. all agents' runs on a branch, a Jira key, or an
// issue). Reached by clicking a trace chip anywhere in the app.

import { Fragment, useCallback, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { Commits, StatusPill, Timeline } from "./components";
import { fmtCost, fmtDuration, fmtTime, isActive, sourceLink } from "./format";
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
  // Per-run expansion: the inline turns + commits panel under a row.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const res = poll.data;
  // Oldest-first: a trace is a story (original request → follow-ups), so read
  // it top-to-bottom in the order it happened. The API returns newest-first.
  const runs = [...(res?.runs ?? [])].sort(
    (a, b) => (a.created_at ?? 0) - (b.created_at ?? 0),
  );
  // Ids in this trace, for marking follow-ups whose parent is visible here.
  const inTrace = new Set(runs.map((r) => r.assignment_id));
  // The conversation link (same for every run in a slack_thread trace) —
  // surface it once in the header.
  const threadLink =
    dimension === "slack_thread"
      ? runs.map((r) => sourceLink(r.source, r.trace_refs, r.source_context)).find(Boolean)
      : null;

  return (
    <div>
      <button onClick={onBack}>← Back to fleet</button>
      <h2>
        Trace: <span className="mono">{dimension}</span> = <span className="mono">{value}</span>
      </h2>
      <p className="muted">
        {res ? `${runs.length} run${runs.length === 1 ? "" : "s"} share this reference` : "Loading…"}
        {res?.truncated && " (sampled from the most recent runs — fleet exceeds the scan cap)"}
        {threadLink && (
          <>
            {" · "}
            <a href={threadLink.url} target="_blank" rel="noreferrer">
              open in Slack ↗
            </a>
          </>
        )}
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
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const turns = (r.timeline ?? []).length;
              const commitCount = (r.commits ?? []).length;
              const isOpen = expanded.has(r.assignment_id);
              return (
                <Fragment key={r.assignment_id}>
                  <tr>
                    <td>
                      <StatusPill status={r.status} />
                    </td>
                    <td>
                      {/* D8 lineage marker: this run continued an earlier run in
                          this same trace (a reply on its completed thread). */}
                      {r.parent_assignment_id && inTrace.has(r.parent_assignment_id) && (
                        <span className="muted" title={`Follow-up of ${r.parent_assignment_id}`}>
                          ↳{" "}
                        </span>
                      )}
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
                    <td>
                      {turns > 0 || commitCount > 0 ? (
                        <button className="link-btn" onClick={() => toggle(r.assignment_id)}>
                          {isOpen ? "▾" : "▸"} {turns} turn{turns === 1 ? "" : "s"}
                          {commitCount > 0 &&
                            ` · ${commitCount} commit${commitCount === 1 ? "" : "s"}`}
                        </button>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="trace-detail-row">
                      {/* Inline story: every turn the agent took + every commit
                          it pushed, without leaving the trace. */}
                      <td colSpan={8}>
                        <Timeline events={r.timeline} />
                        <Commits commits={r.commits} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
