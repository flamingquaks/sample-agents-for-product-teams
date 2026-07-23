// Agents page: wraps CapabilitiesPanel with proper page header.

import { CapabilitiesPanel } from "../CapabilitiesPanel";
import type { DashboardApi } from "../api";

export function AgentsPage({
  api,
  onAuthError,
}: {
  api: DashboardApi;
  onAuthError: () => void;
}) {
  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Agents</h1>
          <p className="page-desc">
            Author, configure, and manage fleet agents. Built-in agents are read-only;
            custom agents are fully editable.
          </p>
        </div>
      </div>
      <CapabilitiesPanel api={api} onAuthError={onAuthError} />
    </div>
  );
}
