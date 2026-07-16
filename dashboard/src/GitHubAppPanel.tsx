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
import type { GitHubAppStatus } from "./types";

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

  const statusPoll = usePolling<GitHubAppStatus>(() => api.gitHubAppStatus(), {
    isActive: () => false,
    deps: [api],
    onError: handleError,
  });

  const [org, setOrg] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const status = statusPoll.data;

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

  // Only shown when the fleet is in App auth mode; PAT mode has no App to set up.
  if (status && status.auth_mode !== "app") {
    return (
      <div className="panel">
        <h3>GitHub access</h3>
        <p className="muted">
          Fleet is in <code>pat</code> mode (shared token). Set{" "}
          <code>GitHubAuthMode=app</code> on the stack to use per-owner GitHub App
          credentials, then set up the App here.
        </p>
      </div>
    );
  }

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
            >
              Install on GitHub ↗
            </a>
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
