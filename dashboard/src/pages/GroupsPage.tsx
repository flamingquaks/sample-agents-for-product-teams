// Groups page: wraps GroupsPanel with proper page header.

import { GroupsPanel } from "../connectors/GroupsPanel";
import type { DashboardApi } from "../api";

export function GroupsPage({
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
          <h1>Groups</h1>
          <p className="page-desc">
            Permission groups — the recommended access mechanism. Members inherit
            group-scoped trigger rules across every event source.
          </p>
        </div>
      </div>
      <GroupsPanel api={api} onAuthError={onAuthError} />
    </div>
  );
}
