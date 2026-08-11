// Display helpers shared across views. All tolerate null/undefined because run
// records are frequently partial (in-flight runs, pre-enrichment rows).

import type { RunStatus } from "./types";

/** Epoch seconds → localized date-time, or "—" when absent. */
export function fmtTime(epochSeconds?: number | null): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

/** Seconds → compact human duration ("2m 5s", "1h 3m", "45s"), or "—". */
export function fmtDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** USD cost with 4 decimals, or "—". Zero is a real value, not "unknown". */
export function fmtCost(usd?: number | null): string {
  if (usd === null || usd === undefined) return "—";
  return `$${usd.toFixed(4)}`;
}

/** Token count with thousands separators, or "—". */
export function fmtTokens(tokens?: number | null): string {
  if (tokens === null || tokens === undefined) return "—";
  return tokens.toLocaleString();
}

/** A CSS class suffix for a status, used to color status pills. */
export function statusClass(status?: RunStatus): string {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
    case "blocked_guardrail":
    case "blocked_guardrail_error":
      return "err";
    case "dispatched":
    case "resuming":
      return "active";
    case "awaiting_input":
      return "warn"; // paused on the requester — a human should engage
    case "timed_out":
      return "unknown";
    default:
      return "unknown";
  }
}

/** Human-friendly label for a status pill (falls back to the raw value). */
export function statusLabel(status?: RunStatus): string {
  switch (status) {
    case "awaiting_input":
      return "awaiting reply";
    case "timed_out":
      return "timed out";
    default:
      return status || "unknown";
  }
}

/** Whether a run is still in flight (drives live-polling cadence). A paused
 * (awaiting_input) run is NOT active — it can sit for days; resuming is. */
export function isActive(status?: RunStatus): boolean {
  return status === "dispatched" || status === "resuming";
}

export interface SourceLink {
  label: string;
  url: string;
}

/**
 * Build a deep link back to the system that triggered a run, from the fields
 * the router stored on trace_refs / source_context. Returns null when there's
 * nothing linkable (e.g. an unknown source, or missing identifiers) rather than
 * a broken URL. GitHub links are derived from repo + issue/pr number; Asana
 * links from the task gid.
 */
export function sourceLink(
  source: string | undefined,
  refs: Record<string, string> | undefined,
  ctx: Record<string, unknown> | undefined,
): SourceLink | null {
  const r = refs ?? {};
  const c = (ctx ?? {}) as Record<string, string | undefined>;

  if (source === "github") {
    const repo = r.repo ?? c.repo;
    // A PR carries pr_number; otherwise it's an issue. Both live at the same
    // path on github.com (owner/repo/issues/N redirects PRs correctly).
    const num = r.pr_number ?? r.issue_number ?? c.issue_number;
    if (repo && num) {
      const kind = r.pr_number ? "pull" : "issues";
      return { label: `${repo}#${num}`, url: `https://github.com/${repo}/${kind}/${num}` };
    }
    return null;
  }

  if (source === "asana") {
    const taskGid = r.asana_task_gid ?? c.task_gid;
    if (taskGid) {
      return { label: `Asana task ${taskGid}`, url: `https://app.asana.com/0/0/${taskGid}` };
    }
    return null;
  }

  if (source === "slack") {
    // The router captures the triggering message's permalink at dispatch time
    // (the workspace domain only Slack knows). Older runs predate the capture —
    // they render without a link, like a permalink-API miss.
    const permalink = c.slack_permalink;
    if (permalink) {
      return { label: "Slack thread", url: permalink };
    }
    return null;
  }

  if (source === "jira") {
    // The receiver stores the site_url in source_context; the issue key is a
    // native trace ref (atlassian-connector §B3).
    const siteUrl = (c.site_url ?? "").replace(/\/$/, "");
    const key = r.jira_key ?? c.issue_key;
    if (siteUrl && key) {
      return { label: key, url: `${siteUrl}/browse/${key}` };
    }
    return null;
  }

  if (source === "confluence") {
    // <site_url>/wiki/spaces/<KEY>/pages/<id> (atlassian-connector §C4).
    const siteUrl = (c.site_url ?? "").replace(/\/$/, "");
    const space = r.confluence_space ?? c.space_key;
    const pageId = r.confluence_page ?? c.page_id;
    if (siteUrl && space && pageId) {
      const title = c.page_title ? String(c.page_title) : `page ${pageId}`;
      return { label: title, url: `${siteUrl}/wiki/spaces/${space}/pages/${pageId}` };
    }
    return null;
  }

  return null;
}
