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

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { CapabilitiesPanel } from "./CapabilitiesPanel";
import { SkillsPanel } from "./SkillsPanel";
import { fmtTime } from "./format";
import { usePolling } from "./hooks";
import type { FleetSettings, RepoConfig } from "./types";

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
            rejected at dispatch; a repo that is onboarded but not multi-repo eligible is
            dispatchable but blocked from cross-repo tool actions at the Gateway.
          </p>
        </div>
        <button className="primary" disabled={busy} onClick={() => setOnboardOpen(true)}>
          Onboard repository
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
          onSuccess={(repo, warning) => {
            setOnboardOpen(false);
            setActionError(null);
            setActionMsg(warning ? `Onboarded ${repo} — ${warning}` : `Onboarded ${repo}.`);
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
            <th>Multi-repo eligible</th>
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
              <td>{r.multi_repo_eligible ? "yes" : "no"}</td>
              <td>
                {!r.multi_repo_eligible
                  ? "—"
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
              <td colSpan={8} className="muted">
                No repositories onboarded yet. Use “Onboard repository” to add one.
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

// owner/repo with GitHub-legal segment chars — mirrors admin._valid_repo so the
// client rejects the same inputs the server would, with an inline reason.
const REPO_RE = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;

// Modal onboarding workflow. The old inline form buried a two-step interaction
// (type owner/repo, THEN click a separate button) in a filter row, which read
// as ambient page furniture — it wasn't obvious the field and button were one
// action. A modal makes the flow explicit: click "Onboard repository", fill the
// form in a focused dialog, submit. The dialog only closes on success (the
// parent's onOnboard resolves after the write); a validation or API failure
// keeps it open with the reason shown so the input isn't lost.
// The modal owns its submit so it can handle the GitHub-App verification loop:
// a 409 "not installed" / "not covered" carries an `install_url` the dialog
// surfaces as an install button + a Re-check (retry) — a guided loop, not a
// dead end. On success it calls onSuccess (which closes the dialog + refreshes
// the parent's list).
function OnboardModal({
  api,
  onSuccess,
  onClose,
}: {
  api: DashboardApi;
  onSuccess: (repo: string, warning?: string) => void;
  onClose: () => void;
}) {
  const [repo, setRepo] = useState("");
  const [eligible, setEligible] = useState(true);
  const [coRepoMode, setCoRepoMode] = useState<"isolated" | "group" | "all">("isolated");
  const [repoGroup, setRepoGroup] = useState("");
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [installUrl, setInstallUrl] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const submit = async () => {
    const trimmed = repo.trim();
    if (!trimmed) {
      setHint("Enter a repository as owner/repo (e.g. octocat/hello-world).");
      return;
    }
    if (!REPO_RE.test(trimmed)) {
      setHint(`"${trimmed}" isn't a valid owner/repo — one slash, letters/digits/._- only.`);
      return;
    }
    if (coRepoMode === "group" && !repoGroup.trim()) {
      setHint("Enter a group name when co-repo mode is “group”.");
      return;
    }
    setHint(null);
    setInstallUrl(null);
    setBusy(true);
    try {
      const rec = (await api.onboardRepo({
        repo: trimmed,
        enabled: true,
        multi_repo_eligible: eligible,
        co_repo_mode: coRepoMode,
        repo_group: coRepoMode === "group" ? repoGroup.trim() : undefined,
      })) as { policy_sync_warning?: string };
      onSuccess(trimmed, rec.policy_sync_warning);
    } catch (e) {
      if (e instanceof ApiError) {
        setHint(e.message);
        // 409 with an install deep-link → offer to install + re-check.
        const link = e.body?.install_url;
        if (typeof link === "string") setInstallUrl(link);
      } else {
        setHint((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

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
        <h3 id="onboard-title">Onboard repository</h3>
        <p className="muted">
          Enter the repository as <code>owner/repo</code>. It becomes dispatchable immediately;
          leave “multi-repo eligible” checked to also allow cross-repo tool actions at the Gateway.
        </p>

        <label className="field">
          <span>Repository</span>
          <input
            ref={inputRef}
            placeholder="owner/repo"
            value={repo}
            disabled={busy}
            onChange={(e) => {
              setRepo(e.target.value);
              if (hint) setHint(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
            aria-label="Repository (owner/repo)"
          />
        </label>

        <label className="field-inline">
          <input
            type="checkbox"
            checked={eligible}
            disabled={busy}
            onChange={(e) => setEligible(e.target.checked)}
          />{" "}
          Multi-repo eligible
        </label>

        {eligible && (
          <>
            <label className="field">
              <span>Approved to run with</span>
              <select
                value={coRepoMode}
                disabled={busy}
                onChange={(e) =>
                  setCoRepoMode(e.target.value as "isolated" | "group" | "all")
                }
                aria-label="Co-repo mode"
              >
                <option value="isolated">Only itself (isolated)</option>
                <option value="group">Repos in a named group</option>
                <option value="all">All eligible repos</option>
              </select>
            </label>
            {coRepoMode === "group" && (
              <label className="field">
                <span>Group name</span>
                <input
                  placeholder="e.g. acme-platform"
                  value={repoGroup}
                  disabled={busy}
                  onChange={(e) => setRepoGroup(e.target.value)}
                  aria-label="Repo group name"
                />
              </label>
            )}
            <p className="muted" style={{ marginTop: 4 }}>
              Controls which OTHER repos a dispatch from this repo may act on. A
              group is mutual — repos sharing a group name (and set to “group”)
              may operate on each other, across owners/orgs.
            </p>
          </>
        )}

        {hint && (
          <div className="banner error" role="alert">
            {hint}
            {installUrl && (
              <div style={{ marginTop: 8 }}>
                <a className="button-link" href={installUrl} target="_blank" rel="noreferrer">
                  Install the GitHub App ↗
                </a>{" "}
                then Re-check.
              </div>
            )}
          </div>
        )}

        <div className="modal-actions">
          <button disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={busy} onClick={() => void submit()}>
            {busy ? "Onboarding…" : installUrl ? "Re-check" : "Onboard repo"}
          </button>
        </div>
      </div>
    </div>
  );
}
