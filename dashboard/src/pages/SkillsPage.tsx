// Skills page: wraps SkillsPanel with proper page header.

import { SkillsPanel } from "../SkillsPanel";
import type { DashboardApi } from "../api";

export function SkillsPage({
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
          <h1>Skills</h1>
          <p className="page-desc">
            Upload, manage, and delete SKILL.md packages. Skills are referenced by
            agent capabilities and stored in S3.
          </p>
        </div>
      </div>
      <SkillsPanel api={api} onAuthError={onAuthError} />
    </div>
  );
}
