// Fleet view: the landing page. A stat-tile header (live fleet rollups) over a
// filterable, paginated, live-polling table of runs.

import { useCallback, useMemo, useState } from "react";
import type { DashboardApi } from "./api";
import { StatusPill, TraceChips } from "./components";
import { fmtCost, fmtDuration, fmtTime, isActive } from "./format";
import { usePolling } from "./hooks";
import type { FleetStats, Run, RunsPage } from "./types";

interface Filters {
  status: string;
  agent_id: string;
  source: string;
  requester: string;
}

const EMPTY_FILTERS: Filters = { status: "", agent_id: "", source: "", requester: "" };
const PAGE_LIMIT = 25;

export function FleetView({
  api,
  onOpenRun,
  onTrace,
}: {
  api: DashboardApi;
  onOpenRun: (assignmentId: string) => void;
  onTrace: (dimension: string, value: string) => void;
}) {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  // Pagination: a stack of page tokens so "prev" works. token[i] fetches page i;
  // page 0 uses no token.
  const [pageTokens, setPageTokens] = useState<(string | null)[]>([null]);
  const pageToken = pageTokens[pageTokens.length - 1];

  // Stats poll — cheap header, active while any run is dispatched.
  const statsPoll = usePolling<FleetStats>(
    () => api.stats(),
    (s) => s.active > 0,
    [api],
  );

  // Runs poll — the visible page. Active while any row on the page is in flight.
  const listFetcher = useCallback(
    () =>
      api.listRuns({
        limit: PAGE_LIMIT,
        next_token: pageToken ?? undefined,
        status: filters.status || undefined,
        agent_id: filters.agent_id || undefined,
        source: filters.source || undefined,
        requester: filters.requester || undefined,
      }),
    [api, pageToken, filters],
  );
  const runsPoll = usePolling<RunsPage>(
    listFetcher,
    (page) => page.runs.some((r) => isActive(r.status)),
    [listFetcher],
  );

  const setFilter = (key: keyof Filters, value: string) => {
    setFilters((f) => ({ ...f, [key]: value }));
    setPageTokens([null]); // any filter change resets to page 1
  };

  const nextPage = () => {
    const token = runsPoll.data?.next_token;
    if (token) setPageTokens((t) => [...t, token]);
  };
  const prevPage = () => {
    setPageTokens((t) => (t.length > 1 ? t.slice(0, -1) : t));
  };

  const runs = runsPoll.data?.runs ?? [];
  const pageNum = pageTokens.length;
  const hasNext = Boolean(runsPoll.data?.next_token);

  return (
    <div>
      <StatTiles stats={statsPoll.data} />

      <div className="filters">
        <FilterSelect
          label="Status"
          value={filters.status}
          options={statsPoll.data ? Object.keys(statsPoll.data.by_status) : []}
          onChange={(v) => setFilter("status", v)}
        />
        <FilterSelect
          label="Agent"
          value={filters.agent_id}
          options={statsPoll.data ? Object.keys(statsPoll.data.by_agent) : []}
          onChange={(v) => setFilter("agent_id", v)}
        />
        <FilterSelect
          label="Source"
          value={filters.source}
          options={statsPoll.data ? Object.keys(statsPoll.data.by_source) : []}
          onChange={(v) => setFilter("source", v)}
        />
        <input
          placeholder="requester…"
          value={filters.requester}
          onChange={(e) => setFilter("requester", e.target.value)}
        />
        <button onClick={runsPoll.refresh}>Refresh</button>
      </div>

      {runsPoll.error && <div className="banner error">Failed to load runs: {runsPoll.error}</div>}

      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>Agent</th>
            <th>Requester</th>
            <th>Source</th>
            <th>Trace</th>
            <th>Started</th>
            <th>Duration</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r: Run) => (
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
              <td>
                <TraceChips refs={r.trace_refs} onChipClick={onTrace} />
              </td>
              <td>{fmtTime(r.created_at)}</td>
              <td>{fmtDuration(r.duration_seconds)}</td>
              <td>{fmtCost(r.cost_estimate_usd)}</td>
            </tr>
          ))}
          {runs.length === 0 && !runsPoll.loading && (
            <tr>
              <td colSpan={8} className="muted">
                No runs match the current filters.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="pager">
        <button onClick={prevPage} disabled={pageNum <= 1}>
          ← Prev
        </button>
        <span className="muted">Page {pageNum}</span>
        <button onClick={nextPage} disabled={!hasNext}>
          Next →
        </button>
        {runsPoll.loading && <span className="muted">loading…</span>}
      </div>
    </div>
  );
}

function StatTiles({ stats }: { stats: FleetStats | null }) {
  const tiles = useMemo(
    () => [
      { label: "Total runs", value: stats ? stats.total : "—" },
      { label: "Active now", value: stats ? stats.active : "—" },
      { label: "Completed", value: stats?.by_status?.completed ?? "—" },
      {
        label: "Failed",
        value: stats
          ? (stats.by_status?.failed ?? 0) +
            (stats.by_status?.blocked_guardrail ?? 0) +
            (stats.by_status?.blocked_guardrail_error ?? 0)
          : "—",
      },
    ],
    [stats],
  );
  return (
    <div className="tiles">
      {tiles.map((t) => (
        <div className="tile" key={t.label}>
          <div className="label">{t.label}</div>
          <div className="value">{t.value}</div>
        </div>
      ))}
      {stats?.truncated && (
        <div className="tile">
          <div className="label">Note</div>
          <div className="muted" style={{ fontSize: 12 }}>
            Stats sampled from the most recent runs (fleet exceeds the scan cap).
          </div>
        </div>
      )}
    </div>
  );
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
      <option value="">{label}: all</option>
      {options.sort().map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}
