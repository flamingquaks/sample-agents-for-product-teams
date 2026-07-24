// Small presentational components shared across views.

import type { Run, TraceRefs } from "./types";
import { statusClass, statusLabel } from "./format";

export function StatusPill({ status }: { status?: string }) {
  return <span className={`pill ${statusClass(status)}`}>{statusLabel(status)}</span>;
}

/**
 * Render a run's trace_refs as chips. Data-driven: whatever keys are present
 * are shown, so a new integration's ref type appears with no code change here.
 * `onChipClick` (optional) lets a chip deep-link into the trace view.
 */
export function TraceChips({
  refs,
  onChipClick,
}: {
  refs?: TraceRefs;
  onChipClick?: (dimension: string, value: string) => void;
}) {
  const entries = refs ? Object.entries(refs) : [];
  if (entries.length === 0) return <span className="muted">—</span>;
  return (
    <span className="chips">
      {entries.map(([k, v]) => {
        const body = (
          <>
            <b>{k}</b>: {v}
          </>
        );
        return onChipClick ? (
          <button
            key={k}
            className="chip"
            title={`Trace all runs with ${k} = ${v}`}
            onClick={() => onChipClick(k, v)}
          >
            {body}
          </button>
        ) : (
          <span key={k} className="chip">
            {body}
          </span>
        );
      })}
    </span>
  );
}

/** Participants as a compact "id (kind)" list. */
export function Participants({ run }: { run: Run }) {
  const parts = run.participants ?? [];
  if (parts.length === 0) {
    // Fall back to the bare requester when enrichment didn't populate the list.
    return <span className="muted">{run.requester ?? "—"}</span>;
  }
  return (
    <span>
      {parts.map((p, i) => (
        <span key={p.id}>
          {i > 0 ? ", " : ""}
          {p.id} <span className="muted">({p.kind})</span>
        </span>
      ))}
    </span>
  );
}
