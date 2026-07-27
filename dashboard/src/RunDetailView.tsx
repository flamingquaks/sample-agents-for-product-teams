// Run detail: the full record for one assignment, live-polled while it's still
// in flight. Reached from a run row or the trace view.

import { useCallback } from "react";
import { ApiError, type DashboardApi } from "./api";
import { Commits, Participants, StatusPill, Timeline, TraceChips } from "./components";
import { fmtCost, fmtDuration, fmtTime, fmtTokens, isActive, sourceLink } from "./format";
import { usePolling } from "./hooks";
import type { Run } from "./types";

export function RunDetailView({
  api,
  assignmentId,
  onBack,
  onTrace,
  onOpenRun,
  onAuthError,
}: {
  api: DashboardApi;
  assignmentId: string;
  onBack: () => void;
  onTrace: (dimension: string, value: string) => void;
  onOpenRun: (assignmentId: string) => void;
  onAuthError: () => void;
}) {
  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );

  const poll = usePolling<Run>(() => api.getRun(assignmentId), {
    // Keep polling while the run is still active so its terminal state, cost,
    // and duration fill in live.
    isActive: (run) => isActive(run.status),
    deps: [api, assignmentId],
    onError: handleError,
  });

  const run = poll.data;
  const notFound = poll.error && poll.error.includes("not found");

  return (
    <div>
      <button onClick={onBack}>← Back to fleet</button>

      {notFound ? (
        <div className="banner error">Run {assignmentId} not found (it may have expired).</div>
      ) : poll.error && !run ? (
        <div className="banner error">Failed to load run: {poll.error}</div>
      ) : !run ? (
        <p className="muted">Loading…</p>
      ) : (
        <RunDetail run={run} onTrace={onTrace} onOpenRun={onOpenRun} />
      )}
    </div>
  );
}

function RunDetail({
  run,
  onTrace,
  onOpenRun,
}: {
  run: Run;
  onTrace: (d: string, v: string) => void;
  onOpenRun: (assignmentId: string) => void;
}) {
  const link = sourceLink(run.source, run.trace_refs, run.source_context);

  return (
    <>
      <h2>
        {run.agent_id ?? "run"} <StatusPill status={run.status} />
      </h2>
      <div className="mono muted" style={{ marginBottom: 16 }}>
        {run.assignment_id}
      </div>

      <dl className="detail-grid">
        <dt>Requester</dt>
        <dd>{run.requester ?? "—"}</dd>

        <dt>Participants</dt>
        <dd>
          <Participants run={run} />
        </dd>

        <dt>Source</dt>
        <dd>
          {run.source ?? "—"}
          {run.trigger_type ? ` · ${run.trigger_type}` : ""}
          {link && (
            <>
              {" — "}
              <a href={link.url} target="_blank" rel="noreferrer">
                {link.label} ↗
              </a>
            </>
          )}
        </dd>

        <dt>Trace</dt>
        <dd>
          <TraceChips refs={run.trace_refs} onChipClick={onTrace} />
        </dd>

        {run.parent_assignment_id && (
          <>
            <dt>Continues</dt>
            <dd>
              {/* D8 lineage: this run is a follow-up started by a reply on the
                  parent's completed Slack thread. */}
              <a
                href="#"
                className="mono"
                onClick={(e) => {
                  e.preventDefault();
                  onOpenRun(run.parent_assignment_id!);
                }}
              >
                {run.parent_assignment_id}
              </a>
            </dd>
          </>
        )}

        <dt>Started</dt>
        <dd>{fmtTime(run.created_at)}</dd>

        <dt>Completed</dt>
        <dd>{fmtTime(run.completed_at)}</dd>

        <dt>Duration</dt>
        <dd>{fmtDuration(run.duration_seconds)}</dd>

        <dt>Tokens</dt>
        <dd>{fmtTokens(run.token_usage)}</dd>

        <dt>Est. cost</dt>
        <dd>{fmtCost(run.cost_estimate_usd)}</dd>
      </dl>

      {run.status === "awaiting_input" && (
        <div className="banner">
          ⏸️ Paused — the agent asked the requester a question and is waiting for
          an in-thread reply. Work so far is checkpointed on{" "}
          {(run.workspace_snapshot ?? []).length > 0
            ? (run.workspace_snapshot ?? [])
                .map((w) => `${w.repo}@${w.branch}`)
                .join(", ")
            : "the conversation session"}
          .
          {run.pending_question && (
            <pre className="prewrap" style={{ marginTop: 8 }}>
              {run.pending_question}
            </pre>
          )}
        </div>
      )}

      {(run.timeline ?? []).length > 0 ? (
        // The full conversation supersedes the bare instruction/result blocks:
        // every turn (request, agent questions, user replies, outcome) in order.
        <section>
          <h3>Conversation</h3>
          <Timeline events={run.timeline} />
        </section>
      ) : (
        <>
          {run.instruction && (
            <section>
              <h3>Instruction</h3>
              <pre className="prewrap">{run.instruction}</pre>
            </section>
          )}

          {run.result_summary && (
            <section>
              <h3>Result</h3>
              <pre className="prewrap">{run.result_summary}</pre>
            </section>
          )}
        </>
      )}

      <Commits commits={run.commits} />
    </>
  );
}
