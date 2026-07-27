// Small presentational components shared across views.

import type { CommitRecord, Run, TimelineEvent, TraceRefs } from "./types";
import { fmtTime, statusClass, statusLabel } from "./format";

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

const TURN_META: Record<string, { icon: string; label: string }> = {
  dispatched: { icon: "📨", label: "Request" },
  question: { icon: "❓", label: "Agent asked" },
  reply: { icon: "💬", label: "User replied" },
  result: { icon: "✅", label: "Result" },
  error: { icon: "❌", label: "Error" },
  timed_out: { icon: "⌛", label: "Timed out" },
};

/**
 * The run's turn-by-turn conversation: dispatched → (question → reply)* →
 * result | error. Data-driven off the `timeline` list the router/agent/sweeper
 * append alongside their status writes; unknown kinds render generically.
 */
export function Timeline({ events }: { events?: TimelineEvent[] | null }) {
  const turns = events ?? [];
  if (turns.length === 0) {
    return <span className="muted">— (predates turn capture)</span>;
  }
  return (
    <ol className="timeline">
      {turns.map((t, i) => {
        const meta = TURN_META[t.kind] ?? { icon: "•", label: t.kind };
        return (
          <li key={i} className={`turn turn-${t.kind}`}>
            <div>
              {meta.icon} <b>{meta.label}</b>
              {t.actor && <span className="muted"> · {t.actor}</span>}
              <span className="muted"> · {fmtTime(t.ts)}</span>
            </div>
            <pre className="prewrap turn-text">{t.text || "—"}</pre>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * Every commit the run pushed to its wip branch, with the files each touched.
 */
export function Commits({ commits }: { commits?: CommitRecord[] | null }) {
  const list = commits ?? [];
  if (list.length === 0) return null;
  return (
    <section>
      <h3>Commits ({list.length})</h3>
      {list.map((c, i) => {
        const total = c.files_total ?? c.files.length;
        const truncated = total > c.files.length;
        return (
          <div key={`${c.sha}-${i}`} className="commit">
            <div>
              <a
                href={`https://github.com/${c.repo}/commit/${c.sha}`}
                target="_blank"
                rel="noreferrer"
                className="mono"
              >
                {c.sha.slice(0, 12)} ↗
              </a>{" "}
              <b>{c.message || "(no message)"}</b>
              <span className="muted">
                {" "}
                · {c.repo}@{c.branch} · {fmtTime(c.ts)} · {total} file{total === 1 ? "" : "s"}
              </span>
            </div>
            <ul className="commit-files mono">
              {c.files.map((f) => (
                <li key={f}>{f}</li>
              ))}
              {truncated && (
                <li className="muted">…and {total - c.files.length} more</li>
              )}
            </ul>
          </div>
        );
      })}
    </section>
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
