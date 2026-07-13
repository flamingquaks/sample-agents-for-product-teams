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
      return "active";
    default:
      return "unknown";
  }
}

/** Whether a run is still in flight (drives live-polling cadence). */
export function isActive(status?: RunStatus): boolean {
  return status === "dispatched";
}
