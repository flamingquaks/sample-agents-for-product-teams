// AdminView is no longer used as a monolithic view. All its sections have been
// extracted into individual pages (ReposPage, AgentsPage, SkillsPage, SettingsPage)
// navigable from the sidebar. This file is kept as a stub to avoid breaking any
// lingering imports during the transition; it renders nothing.

import type { DashboardApi } from "./api";

export function AdminView(_props: {
  api: DashboardApi;
  onAuthError: () => void;
  onOpenConnectors?: () => void;
}) {
  // Redirect to the new repos page if somehow rendered
  if (typeof window !== "undefined") {
    window.location.hash = "#/repos";
  }
  return null;
}
