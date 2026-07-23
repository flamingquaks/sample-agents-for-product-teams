// Users page: wraps UsersPanel with proper page header.

import { UsersPanel } from "../connectors/UsersPanel";
import type { DashboardApi } from "../api";

export function UsersPage({
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
          <h1>Users</h1>
          <p className="page-desc">
            Cross-source identity directory and the user-onboarding approval queue.
            First-touch users appear here for review.
          </p>
        </div>
      </div>
      <UsersPanel api={api} onAuthError={onAuthError} />
    </div>
  );
}
