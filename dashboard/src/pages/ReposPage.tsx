// Repositories page: the repo onboarding table extracted from AdminView.
// Keeps ALL the original business logic intact (OnboardModal, settings toggle, etc.)

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, type DashboardApi } from "../api";
import { fmtTime } from "../format";
import { usePolling } from "../hooks";
import type { GitHubAvailableRepos, RepoConfig } from "../types";

export function ReposPage({
  api,
  onAuthError,
}: {
  api: DashboardApi;
  onAuthError: () => void;
}) {
  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );

  const reposPoll = usePolling<{ repos: RepoConfig[] }>(() => api.listRepos(), {
    isActive: () => false,
    deps: [api],
    onError: handleError,
  });

  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [onboardOpen, setOnboardOpen] = useState(false);

  const run = useCallback(
    async (label: string, fn: () => Promise<unknown>): Promise<boolean> => {
      setBusy(true);
      setActionError(null);
      setActionMsg(null);
      try {
        const result = (await fn()) as { policy_sync_warning?: string } | undefined;
        reposPoll.refresh();
        setActionMsg(
          result?.policy_sync_warning
            ? `${label} — ${result.policy_sync_warning}`
            : `${label} succeeded.`,
        );
        return true;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          onAuthError();
          setActionError("Session expired — signing you in again.");
          return false;
        }
        const status = e instanceof ApiError ? ` (HTTP ${e.status})` : "";
        setActionError(`${label} failed${status}: ${(e as Error).message}`);
        return false;
      } finally {
        setBusy(false);
      }
    },
    [reposPoll, onAuthError],
  );

  const repos = reposPoll.data?.repos ?? [];

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Repositories</h1>
          <p className="page-desc">
            Onboard the repositories the fleet may act in. Mentions from a non-onboarded
            repo are rejected at dispatch.
          </p>
        </div>
        <button className="primary" disabled={busy} onClick={() => setOnboardOpen(true)}>
          Onboard repositories
        </button>
      </div>

      {actionError && <div className="banner error">{actionError}</div>}
      {actionMsg && <div className="banner ok">{actionMsg}</div>}

      {onboardOpen && (
        <OnboardModal
          api={api}
          onClose={() => setOnboardOpen(false)}
          onSuccess={(repos, warning) => {
            setOnboardOpen(false);
            setActionError(null);
            const what = repos.length === 1 ? repos[0] : `${repos.length} repositories`;
            setActionMsg(warning ? `Onboarded ${what} — ${warning}` : `Onboarded ${what}.`);
            reposPoll.refresh();
          }}
        />
      )}

      {reposPoll.error && (
        <div className="banner error">Failed to load repos: {reposPoll.error}</div>
      )}

      <table>
        <thead>
          <tr>
            <th>Repository</th>
            <th>Dispatchable</th>
            <th>Runs with</th>
            <th>Status</th>
            <th>Onboarded by</th>
            <th>Onboarded</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {repos.map((r) => (
            <tr key={r.repo}>
              <td>
                <code>{r.repo}</code>
              </td>
              <td>{r.enabled ? "yes" : "no"}</td>
              <td>
                {!r.multi_repo_eligible
                  ? "itself only"
                  : r.co_repo_mode === "all"
                    ? "all repos"
                    : r.co_repo_mode === "group"
                      ? `group: ${r.repo_group ?? "?"}`
                      : "itself only"}
              </td>
              <td>
                <span className={`pill ${r.status === "active" ? "ok" : "unknown"}`}>
                  {r.status ?? "unknown"}
                </span>
              </td>
              <td>{r.onboarded_by || "—"}</td>
              <td>{fmtTime(r.onboarded_at)}</td>
              <td>
                <button
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm(`Remove ${r.repo} from the fleet?`)) {
                      void run(`Remove ${r.repo}`, () => api.deleteRepo(r.repo));
                    }
                  }}
                >
                  Remove
                </button>
              </td>
            </tr>
          ))}
          {repos.length === 0 && !reposPoll.loading && (
            <tr>
              <td colSpan={7} className="muted">
                No repositories onboarded yet. Use "Onboard repositories" to add some.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// -- OnboardModal (identical logic from AdminView) --

function OnboardModal({
  api,
  onSuccess,
  onClose,
}: {
  api: DashboardApi;
  onSuccess: (repos: string[], warning?: string) => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [access, setAccess] = useState<"isolated" | "shared">("isolated");
  const [watching, setWatching] = useState(false);
  const availPoll = usePolling<GitHubAvailableRepos>(() => api.gitHubAppRepos(), {
    isActive: () => watching,
    deps: [api],
  });
  const avail = availPoll.data;
  const filterRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    filterRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const rows = useMemo(() => {
    const all = (avail?.installations ?? []).flatMap((inst) =>
      inst.repos.map((r) => ({ ...r, owner: inst.owner })),
    );
    const q = filter.trim().toLowerCase();
    return q ? all.filter((r) => r.repo.toLowerCase().includes(q)) : all;
  }, [avail, filter]);

  const toggle = (repo: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(repo)) next.delete(repo);
      else next.add(repo);
      return next;
    });
    if (hint) setHint(null);
  };

  const submit = async () => {
    const repos = [...selected];
    if (repos.length === 0) {
      setHint("Select at least one repository.");
      return;
    }
    setHint(null);
    setBusy(true);
    try {
      const result = await api.onboardRepos({
        repos,
        co_repo_mode: access === "shared" && repos.length > 1 ? "group" : "isolated",
        repo_group:
          access === "shared" && repos.length > 1
            ? repos[0].replace("/", "-")
            : undefined,
      });
      onSuccess(repos, result.policy_sync_warning);
    } catch (e) {
      if (e instanceof ApiError) {
        setHint(e.message);
      } else {
        setHint((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  const installUrl = avail?.install_url;
  const noInstalls = avail !== null && avail.configured && rows.length === 0 && !filter;

  return (
    <div
      className="modal-overlay"
      onClick={() => {
        if (!busy) onClose();
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboard-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="onboard-title">Onboard repositories</h3>

        {avail === null && !availPoll.error && <p className="muted">Loading repositories the GitHub App can access...</p>}
        {availPoll.error && (
          <div className="banner error">Could not list repositories: {availPoll.error}</div>
        )}

        {avail !== null && !avail.configured && (
          <p className="muted">
            The GitHub App isn't set up yet — configure it under{" "}
            <b>Connectors &rarr; GitHub</b> first.
          </p>
        )}

        {avail?.configured && (
          <>
            <p className="muted">
              Pick the repositories the fleet may act in. Missing one? Install the App on its
              owner (or add it to the installation's repository selection) and it appears here.
            </p>
            {installUrl && (
              <div style={{ marginBottom: 8 }}>
                <a
                  className="button-link"
                  href={installUrl}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => setWatching(true)}
                >
                  Install / add repositories on GitHub
                </a>{" "}
                {watching && <span className="muted">watching for changes...</span>}
              </div>
            )}

            <input
              ref={filterRef}
              placeholder="Filter repositories..."
              value={filter}
              disabled={busy}
              onChange={(e) => setFilter(e.target.value)}
              aria-label="Filter repositories"
              style={{ width: "100%", marginBottom: 8 }}
            />

            <div className="repo-picker" role="listbox" aria-multiselectable="true">
              {rows.map((r) => (
                <label key={r.repo} className="field-inline" style={{ display: "block" }}>
                  <input
                    type="checkbox"
                    checked={r.onboarded || selected.has(r.repo)}
                    disabled={busy || r.onboarded}
                    onChange={() => toggle(r.repo)}
                  />{" "}
                  <code>{r.repo}</code>
                  {r.private && <span className="muted"> - private</span>}
                  {r.onboarded && <span className="muted"> - already onboarded</span>}
                </label>
              ))}
              {noInstalls && (
                <p className="muted">
                  The App has no repositories yet — use the install link above, then they'll
                  show up here automatically.
                </p>
              )}
              {rows.length === 0 && filter && (
                <p className="muted">No repositories match "{filter}".</p>
              )}
            </div>

            {selected.size > 1 && (
              <div className="field" style={{ marginTop: 8 }}>
                <span>How these {selected.size} repositories run</span>
                <label className="field-inline">
                  <input
                    type="radio"
                    name="access"
                    checked={access === "isolated"}
                    disabled={busy}
                    onChange={() => setAccess("isolated")}
                  />{" "}
                  Isolated — each repo runs alone
                </label>
                <label className="field-inline">
                  <input
                    type="radio"
                    name="access"
                    checked={access === "shared"}
                    disabled={busy}
                    onChange={() => setAccess("shared")}
                  />{" "}
                  Shared — these repos may act on each other
                </label>
              </div>
            )}

            {hint && (
              <div className="banner error" role="alert">
                {hint}
              </div>
            )}

            <div className="modal-actions">
              <button disabled={busy} onClick={onClose}>
                Cancel
              </button>
              <button
                className="primary"
                disabled={busy || selected.size === 0}
                onClick={() => void submit()}
              >
                {busy
                  ? "Onboarding..."
                  : `Onboard ${selected.size || ""} ${selected.size === 1 ? "repository" : "repositories"}`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
