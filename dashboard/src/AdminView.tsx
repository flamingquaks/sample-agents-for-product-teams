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
          busy={busy}
          onClose={() => setOnboardOpen(false)}
          onOnboard={async (body) => {
            const ok = await run(`Onboard ${body.repo}`, () => api.onboardRepo(body));
            if (ok) setOnboardOpen(false);
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
                No repositories onboarded yet. Use “Onboard repository” to add one.
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
function OnboardModal({
  busy,
  onOnboard,
  onClose,
}: {
  busy: boolean;
  onOnboard: (body: {
    repo: string;
    enabled: boolean;
    multi_repo_eligible: boolean;
  }) => void | Promise<void>;
  onClose: () => void;
}) {
  const [repo, setRepo] = useState("");
  const [eligible, setEligible] = useState(true);
  const [hint, setHint] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus the field on open and allow Esc to dismiss, so the dialog is usable
  // from the keyboard the moment it appears.
  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const submit = () => {
    const trimmed = repo.trim();
    // Never a silent no-op: if the field is empty or malformed, say why.
    if (!trimmed) {
      setHint("Enter a repository as owner/repo (e.g. octocat/hello-world).");
      return;
    }
    if (!REPO_RE.test(trimmed)) {
      setHint(`"${trimmed}" isn't a valid owner/repo — one slash, letters/digits/._- only.`);
      return;
    }
    setHint(null);
    void onOnboard({ repo: trimmed, enabled: true, multi_repo_eligible: eligible });
  };

  return (
    <div
      className="modal-overlay"
      // Click on the backdrop (not the dialog) closes, matching common dialog UX.
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
              if (e.key === "Enter") submit();
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

        {hint && (
          <div className="banner error" role="alert">
            {hint}
          </div>
        )}

        <div className="modal-actions">
          <button disabled={busy} onClick={onClose}>
            Cancel
          </button>
          {/* Button is NOT disabled on empty input — submit() reports the reason
              instead of being an inert dead end. */}
          <button className="primary" disabled={busy} onClick={submit}>
            {busy ? "Onboarding…" : "Onboard repo"}
          </button>
        </div>
      </div>
    </div>
  );
}
