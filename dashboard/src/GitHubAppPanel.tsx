// GitHub App setup panel (admin). Drives the manifest flow:
//   1. "Set up GitHub App" → GET the manifest from the API, then POST it to
//      github.com via a real form submit (GitHub requires a form POST of the
//      `manifest` field; a fetch won't do — it navigates the browser to GitHub).
//   2. GitHub creates the App and redirects back to
//      #/admin/github-app/setup-callback?code=... ; App.tsx routes that hash and
//      the callback view calls gitHubAppExchange(code) to persist the App creds.
//   3. Once configured, show the App slug + an "Install on GitHub" deep-link so
//      the admin installs it on the owners whose repos they'll onboard.
//
// Optionally scope the App to an organization (org-owned) vs the signed-in
// user's account, matching the two GitHub manifest form targets.

import { useCallback, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { usePolling } from "./hooks";
import type { GitHubAppStatus, GitHubAvailableRepos } from "./types";

/** POST the manifest to GitHub via a transient auto-submitting form. GitHub's
 *  manifest flow requires an HTML form POST of a `manifest` field — it then
 *  navigates the browser to GitHub, which redirects back to our callback. */
function postManifestToGitHub(postUrl: string, manifest: Record<string, unknown>) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = postUrl;
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "manifest";
  input.value = JSON.stringify(manifest);
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
}

export function GitHubAppPanel({
  api,
  onAuthError,
  refreshKey = 0,
}: {
  api: DashboardApi;
  onAuthError: () => void;
  /** Bumped by the parent after a successful registration to force an immediate
   *  status re-fetch (instead of waiting for the idle poll). */
  refreshKey?: number;
}) {
  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );

  const statusPoll = usePolling<GitHubAppStatus>(() => api.gitHubAppStatus(), {
    isActive: () => false,
    // refreshKey in deps → a bump re-runs the fetcher, so a just-registered App
    // shows as configured immediately rather than after the ~20s idle beat.
    deps: [api, refreshKey],
    onError: handleError,
  });

  const [org, setOrg] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // After "Install on GitHub" opens the other tab, poll installations fast so
  // the panel reflects the finished install without a manual refresh.
  const [watching, setWatching] = useState(false);
  const installsPoll = usePolling<GitHubAvailableRepos>(() => api.gitHubAppRepos(), {
    // Fast only while waiting for a first install to land; once installations
    // exist, drop back to the idle beat.
    isActive: (d) => watching && d.installations.length === 0,
    deps: [api, refreshKey],
    onError: handleError,
  });

  const status = statusPoll.data;
  const installations = installsPoll.data?.installations ?? [];

  const startSetup = async () => {
    setBusy(true);
    setErr(null);
    try {
      const { manifest, post_url } = await api.gitHubAppManifest(org.trim() || undefined);
      // Navigates away to GitHub; the callback view resumes the flow on return.
      postManifestToGitHub(post_url, manifest);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onAuthError();
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h3>GitHub App</h3>
      {err && <div className="banner error">{err}</div>}

      {status?.configured ? (
        <>
          <p className="muted">
            App <code>{status.slug}</code> is registered. Install it on each user
            or organization whose repositories you’ll onboard.
          </p>
          {status.install_url && (
            <a
              className="button-link"
              href={status.install_url}
              target="_blank"
              rel="noreferrer"
              onClick={() => setWatching(true)}
            >
              Install on GitHub ↗
            </a>
          )}
          {installations.length > 0 ? (
            <table style={{ marginTop: 12 }}>
              <thead>
                <tr>
                  <th>Installed on</th>
                  <th>Type</th>
                  <th>Repositories</th>
                </tr>
              </thead>
              <tbody>
                {installations.map((inst) => (
                  <tr key={inst.installation_id}>
                    <td>
                      <code>{inst.owner}</code>
                    </td>
                    <td>{inst.owner_type}</td>
                    <td>
                      {inst.repos.length}
                      {inst.repos.some((r) => r.onboarded) &&
                        ` (${inst.repos.filter((r) => r.onboarded).length} onboarded)`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted" style={{ marginTop: 12 }}>
              Not installed anywhere yet
              {watching && " — watching for the install to complete…"}
            </p>
          )}
        </>
      ) : (
        <>
          <p className="muted">
            No GitHub App yet. Set one up — you’ll be taken to GitHub to confirm
            the App’s permissions, then returned here.
          </p>
          <div className="filters">
            <input
              placeholder="organization (optional — blank = your account)"
              value={org}
              disabled={busy}
              onChange={(e) => setOrg(e.target.value)}
              aria-label="GitHub organization (optional)"
            />
            <button className="primary" disabled={busy} onClick={startSetup}>
              {busy ? "Opening GitHub…" : "Set up GitHub App"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
