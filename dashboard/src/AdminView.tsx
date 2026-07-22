// Admin view: fleet configuration (admins group only). Onboard/remove repos
// (picked from what the GitHub App can reach — never typed by hand) and flip
// the fleet-wide "restrict to allowlist" setting. The dashboard nav gates this
// cosmetically on the admins group; the admin API is the real authority
// (non-admins get 403).
//
// Two records back this view (see infra/dashboard/config_store.py): repo rows
// (enabled = dispatchable, co_repo_mode/repo_group = which other repos a
// dispatch may reach, status = pending until the Gateway policy sync lands)
// and a settings singleton (restrict_repos). Every write re-fetches so the
// table reflects the server's post-sync truth rather than an optimistic guess.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { CapabilitiesPanel } from "./CapabilitiesPanel";
import { SkillsPanel } from "./SkillsPanel";
import { fmtTime } from "./format";
import { usePolling } from "./hooks";
import type { FleetSettings, GitHubAvailableRepos, RepoConfig } from "./types";

export function AdminView({
  api,
  onAuthError,
  onOpenConnectors,
}: {
  api: DashboardApi;
  /** Called when the API rejects a request as unauthenticated (expired token). */
  onAuthError: () => void;
  /** Navigate to the Connectors section (inside the admin panel). */
  onOpenConnectors: () => void;
}) {
  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );

  // Config is static between admin actions, so poll on the idle beat only; a
  // write calls refresh() to reflect the change immediately.
  const reposPoll = usePolling<{ repos: RepoConfig[] }>(() => api.listRepos(), {
    isActive: () => false,
    deps: [api],
    onError: handleError,
  });
  const settingsPoll = usePolling<FleetSettings>(() => api.getSettings(), {
    isActive: () => false,
    deps: [api],
    onError: handleError,
  });

  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [onboardOpen, setOnboardOpen] = useState(false);

  // Wrap a write so the button disables, the outcome ALWAYS surfaces (success or
  // failure — never a silent no-op), and both affected polls re-fetch. `label`
  // names the action so feedback is specific.
  const run = useCallback(
    async (label: string, fn: () => Promise<unknown>): Promise<boolean> => {
      setBusy(true);
      setActionError(null);
      setActionMsg(null);
      try {
        const result = (await fn()) as { policy_sync_warning?: string } | undefined;
        reposPoll.refresh();
        settingsPoll.refresh();
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
    [reposPoll, settingsPoll, onAuthError],
  );

  // GitHub App manifest callback: GitHub redirects back to
  // #/admin/github-app/setup-callback?code=... — exchange the code, then clean
  // the hash to #/admin ONLY on success so a refresh doesn't re-redeem a used
  // code. The manifest code is single-use and short-lived, so on a transient
  // failure we LEAVE it in the hash: the surfaced error tells the admin to
  // reload, which retries the exchange rather than forcing a full re-creation.
  // A ref guards against React StrictMode's double effect-invoke (which would
  // otherwise double-redeem the code) now that the hash isn't cleaned up-front.
  const exchangeStarted = useRef(false);
  useEffect(() => {
    const hash = window.location.hash;
    if (!hash.includes("github-app/setup-callback")) return;
    if (exchangeStarted.current) return;
    exchangeStarted.current = true;
    const q = hash.split("?")[1] ?? "";
    const code = new URLSearchParams(q).get("code");
    if (!code) {
      window.location.hash = "#/admin";
      return;
    }
    setBusy(true);
    setActionError(null);
    api
      .gitHubAppExchange(code)
      .then((r) => {
        setActionMsg(`GitHub App "${r.slug}" registered.`);
        // Success: burn the code from the URL and land on the GitHub connector
        // page, where the App panel now lives and re-fetches fresh status on mount.
        window.location.hash = "#/admin/connectors/github";
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onAuthError();
        // Leave the code in the hash so a reload can retry (unless it's a 401,
        // where re-auth is the real fix and the code is likely still intact).
        setActionError(
          `GitHub App setup failed: ${(e as Error).message}. Reload to retry.`,
        );
        exchangeStarted.current = false;
      })
      .finally(() => setBusy(false));
    // Run once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const repos = reposPoll.data?.repos ?? [];
  const restrict = settingsPoll.data?.restrict_repos ?? false;

  return (
    <div>
      <div className="section-head">
        <div>
          <h2>Fleet configuration</h2>
          <p className="muted">
            Onboard the repositories the fleet may act in. Mentions from a non-onboarded repo are
            rejected at dispatch; “Runs with” controls which other repos a dispatch may reach.
          </p>
        </div>
        <button className="primary" disabled={busy} onClick={() => setOnboardOpen(true)}>
          Onboard repositories
        </button>
      </div>

      {actionError && <div className="banner error">{actionError}</div>}
      {actionMsg && <div className="banner ok">{actionMsg}</div>}

      <div className="filters" style={{ alignItems: "center" }}>
        <span>
          <b>Connectors</b> — Slack, Asana, and GitHub: connections, triggers, and access rules.
        </span>
        <button onClick={onOpenConnectors}>Manage connectors →</button>
      </div>

      <SettingsPanel
        restrict={restrict}
        disabled={busy}
        onToggle={(v) =>
          run(`Set restrict-to-allowlist ${v ? "on" : "off"}`, () =>
            api.putSettings({ restrict_repos: v }),
          )
        }
      />

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
                No repositories onboarded yet. Use “Onboard repositories” to add some.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <hr className="section-divider" />

      <CapabilitiesPanel api={api} onAuthError={onAuthError} />

      <hr className="section-divider" />

      <SkillsPanel api={api} onAuthError={onAuthError} />
    </div>
  );
}

function SettingsPanel({
  restrict,
  disabled,
  onToggle,
}: {
  restrict: boolean;
  disabled: boolean;
  onToggle: (value: boolean) => void;
}) {
  return (
    <div className="filters">
      <label>
        <input
          type="checkbox"
          checked={restrict}
          disabled={disabled}
          onChange={(e) => onToggle(e.target.checked)}
        />{" "}
        Restrict to allowlist
      </label>
      <span className="muted">
        {restrict
          ? "Only enabled + multi-repo-eligible repos are allowed for cross-repo tool actions."
          : "Any enabled repo is allowed."}
      </span>
    </div>
  );
}

// Modal onboarding workflow — a PICKER, not a form. The App already knows
// exactly which repos it can reach, so the admin never types owner/repo: the
// dialog loads GET /admin/github-app/repos and renders a filterable checkbox
// list (already-onboarded repos shown checked+disabled). Multiple repos onboard
// in one submit; the only access decision is how the batch runs together:
//   - isolated — each repo runs alone (dispatches can't cross repos), or
//   - shared   — the selected repos may act on each other (one mutual group).
// (The old "multi-repo eligible" checkbox is gone — eligibility is implied; a
// repo that runs isolated simply reaches no other repo.)
// If the App isn't installed anywhere (or a wanted repo is missing from the
// list), the dialog deep-links to GitHub's install page and POLLS the repo list
// so the moment the admin finishes installing in the other tab, the new repos
// appear here — no manual re-check, no dead end.
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
  // Poll while the install page is open in another tab so a fresh install/
  // selection change shows up without a manual refresh.
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
      // "Shared" = the batch forms one mutual group; a deterministic label from
      // the first repo keeps re-onboards of the same batch in the same group.
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

        {avail === null && !availPoll.error && <p className="muted">Loading repositories the GitHub App can access…</p>}
        {availPoll.error && (
          <div className="banner error">Could not list repositories: {availPoll.error}</div>
        )}

        {avail !== null && !avail.configured && (
          <p className="muted">
            The GitHub App isn’t set up yet — configure it under{" "}
            <b>Connectors → GitHub</b> first.
          </p>
        )}

        {avail?.configured && (
          <>
            <p className="muted">
              Pick the repositories the fleet may act in. Missing one? Install the App on its
              owner (or add it to the installation’s repository selection) and it appears here.
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
                  Install / add repositories on GitHub ↗
                </a>{" "}
                {watching && <span className="muted">watching for changes…</span>}
              </div>
            )}

            <input
              ref={filterRef}
              placeholder="Filter repositories…"
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
                  {r.private && <span className="muted"> · private</span>}
                  {r.onboarded && <span className="muted"> · already onboarded</span>}
                </label>
              ))}
              {noInstalls && (
                <p className="muted">
                  The App has no repositories yet — use the install link above, then they’ll
                  show up here automatically.
                </p>
              )}
              {rows.length === 0 && filter && (
                <p className="muted">No repositories match “{filter}”.</p>
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
                  ? "Onboarding…"
                  : `Onboard ${selected.size || ""} ${selected.size === 1 ? "repository" : "repositories"}`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
