// Admin view: fleet configuration (admins group only). Onboard/remove repos,
// toggle per-repo multi-repo eligibility, and flip the fleet-wide "restrict to
// allowlist" setting. The dashboard nav gates this cosmetically on the admins
// group; the admin API is the real authority (non-admins get 403).
//
// Two records back this view (see infra/dashboard/config_store.py): repo rows
// (enabled = dispatchable, multi_repo_eligible = cross-repo tool-call allowed,
// status = pending until the Gateway policy sync lands) and a settings
// singleton (restrict_repos). Every write re-fetches so the table reflects the
// server's post-sync truth rather than an optimistic guess.

import { useCallback, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { fmtTime } from "./format";
import { usePolling } from "./hooks";
import type { FleetSettings, RepoConfig } from "./types";

export function AdminView({
  api,
  onAuthError,
}: {
  api: DashboardApi;
  /** Called when the API rejects a request as unauthenticated (expired token). */
  onAuthError: () => void;
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

  // Wrap a write so the button disables, errors surface in one banner, and both
  // affected polls re-fetch on success (settings + repos can move together once
  // WS5 wires the policy sync).
  const run = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setActionError(null);
      try {
        await fn();
        reposPoll.refresh();
        settingsPoll.refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) onAuthError();
        setActionError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [reposPoll, settingsPoll, onAuthError],
  );

  const repos = reposPoll.data?.repos ?? [];
  const restrict = settingsPoll.data?.restrict_repos ?? false;

  return (
    <div>
      <h2>Fleet configuration</h2>
      <p className="muted">
        Onboard the repositories the fleet may act in. Mentions from a non-onboarded repo are
        rejected at dispatch; a repo that is onboarded but not multi-repo eligible is dispatchable
        but blocked from cross-repo tool actions at the Gateway.
      </p>

      {actionError && <div className="banner error">Action failed: {actionError}</div>}

      <SettingsPanel restrict={restrict} disabled={busy} onToggle={(v) => run(() => api.putSettings({ restrict_repos: v }))} />

      <OnboardForm disabled={busy} onOnboard={(body) => run(() => api.onboardRepo(body))} />

      {reposPoll.error && (
        <div className="banner error">Failed to load repos: {reposPoll.error}</div>
      )}

      <table>
        <thead>
          <tr>
            <th>Repository</th>
            <th>Dispatchable</th>
            <th>Multi-repo eligible</th>
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
              <td>{r.multi_repo_eligible ? "yes" : "no"}</td>
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
                      void run(() => api.deleteRepo(r.repo));
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
                No repositories onboarded yet. Add one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
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

function OnboardForm({
  disabled,
  onOnboard,
}: {
  disabled: boolean;
  onOnboard: (body: { repo: string; enabled: boolean; multi_repo_eligible: boolean }) => void;
}) {
  const [repo, setRepo] = useState("");
  const [eligible, setEligible] = useState(true);

  const submit = () => {
    const trimmed = repo.trim();
    if (!trimmed) return;
    onOnboard({ repo: trimmed, enabled: true, multi_repo_eligible: eligible });
    setRepo("");
    setEligible(true);
  };

  return (
    <div className="filters">
      <input
        placeholder="owner/repo"
        value={repo}
        disabled={disabled}
        onChange={(e) => setRepo(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        aria-label="Repository (owner/repo)"
      />
      <label>
        <input
          type="checkbox"
          checked={eligible}
          disabled={disabled}
          onChange={(e) => setEligible(e.target.checked)}
        />{" "}
        Multi-repo eligible
      </label>
      <button className="primary" disabled={disabled || !repo.trim()} onClick={submit}>
        Onboard repo
      </button>
    </div>
  );
}
