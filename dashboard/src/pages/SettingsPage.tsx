// Settings page: fleet-wide configuration (restrict-to-allowlist toggle,
// GitHub App setup panel for fleet-wide connection config).

import { useCallback, useState } from "react";
import { ApiError, type DashboardApi } from "../api";
import { GitHubAppPanel } from "../GitHubAppPanel";
import { usePolling } from "../hooks";
import type { FleetSettings } from "../types";

export function SettingsPage({
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

  const settingsPoll = usePolling<FleetSettings>(() => api.getSettings(), {
    isActive: () => false,
    deps: [api],
    onError: handleError,
  });

  const [busy, setBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const restrict = settingsPoll.data?.restrict_repos ?? false;

  const onToggle = async (value: boolean) => {
    setBusy(true);
    setActionError(null);
    setActionMsg(null);
    try {
      await api.putSettings({ restrict_repos: value });
      settingsPoll.refresh();
      setActionMsg(`Restrict-to-allowlist ${value ? "enabled" : "disabled"}.`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        onAuthError();
        return;
      }
      setActionError(`Failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p className="page-desc">
            Fleet-wide configuration and connection settings.
          </p>
        </div>
      </div>

      {actionError && <div className="banner error">{actionError}</div>}
      {actionMsg && <div className="banner ok">{actionMsg}</div>}

      <div className="settings-section">
        <h3>Repository access</h3>
        <div className="settings-row">
          <label className="settings-toggle">
            <input
              type="checkbox"
              checked={restrict}
              disabled={busy}
              onChange={(e) => void onToggle(e.target.checked)}
            />
            <span className="settings-toggle-label">Restrict to allowlist</span>
          </label>
          <p className="muted" style={{ marginTop: 4, marginBottom: 0 }}>
            {restrict
              ? "Only repos that run with others (shared/group/all) can be acted on cross-repo."
              : "Any enabled repo can be acted on by a dispatch from another repo."}
          </p>
        </div>
      </div>

      <div className="settings-section">
        <h3>GitHub App</h3>
        <GitHubAppPanel api={api} onAuthError={onAuthError} />
      </div>
    </div>
  );
}
