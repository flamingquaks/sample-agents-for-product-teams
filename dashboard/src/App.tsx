// Top-level app: gate on Cognito auth + operator role, then render the
// AppShell with sidebar navigation. All navigation is hash-based so each
// view has a real URL you can bookmark, deep-link, and refresh.

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "react-oidc-context";
import type { AppConfig } from "./config";
import { cognitoLogoutUrl } from "./auth";
import { useApi } from "./hooks";
import { AppShell, type NavItem } from "./AppShell";
import { FleetView } from "./FleetView";
import { RunDetailView } from "./RunDetailView";
import { TraceView } from "./TraceView";
import { AgentsPage } from "./pages/AgentsPage";
import { ReposPage } from "./pages/ReposPage";
import { SkillsPage } from "./pages/SkillsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { UsersPage } from "./pages/UsersPage";
import { GroupsPage } from "./pages/GroupsPage";
import { SlackConnectorPage } from "./connectors/SlackConnectorPage";
import { GitHubConnectorPage } from "./connectors/GitHubConnectorPage";
import { AsanaConnectorPage } from "./connectors/AsanaConnectorPage";
import { AtlassianConnectorPage } from "./connectors/AtlassianConnectorPage";
import { ApiError } from "./api";

type View =
  | { name: "fleet" }
  | { name: "run"; assignmentId: string }
  | { name: "trace"; dimension: string; value: string }
  | { name: "agents" }
  | { name: "repos" }
  | { name: "skills" }
  | { name: "settings" }
  | { name: "users" }
  | { name: "groups" }
  | { name: "slack" }
  | { name: "github" }
  | { name: "asana" }
  | { name: "atlassian" };

/** Parse the current URL hash into a view. Unknown/empty hashes -> fleet. */
function hashToView(hash: string): View {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "agents") return { name: "agents" };
  if (parts[0] === "repos") return { name: "repos" };
  if (parts[0] === "skills") return { name: "skills" };
  if (parts[0] === "settings") return { name: "settings" };
  if (parts[0] === "access" && parts[1] === "users") return { name: "users" };
  if (parts[0] === "access" && parts[1] === "groups") return { name: "groups" };
  if (parts[0] === "connectors" && parts[1] === "slack") return { name: "slack" };
  if (parts[0] === "connectors" && parts[1] === "github") return { name: "github" };
  if (parts[0] === "connectors" && parts[1] === "asana") return { name: "asana" };
  if (parts[0] === "connectors" && parts[1] === "atlassian") return { name: "atlassian" };
  if (parts[0] === "run" && parts[1]) return { name: "run", assignmentId: parts[1] };
  if (parts[0] === "trace" && parts[1] && parts[2]) {
    return { name: "trace", dimension: parts[1], value: parts[2] };
  }
  // Legacy routes: redirect old admin paths
  if (parts[0] === "admin") {
    if (parts[1] === "connectors" && parts[2] === "slack") return { name: "slack" };
    if (parts[1] === "connectors" && parts[2] === "github") return { name: "github" };
    if (parts[1] === "connectors" && parts[2] === "asana") return { name: "asana" };
    if (parts[1] === "connectors" && parts[2] === "atlassian") return { name: "atlassian" };
    if (parts[1] === "connectors" && parts[2] === "access") return { name: "users" };
    if (parts[1] === "connectors") return { name: "slack" };
    // Redirect #/admin to #/repos (primary admin action)
    return { name: "repos" };
  }
  return { name: "fleet" };
}

/** Serialize a view to its URL hash. */
function viewToHash(view: View): string {
  switch (view.name) {
    case "fleet": return "#/";
    case "agents": return "#/agents";
    case "repos": return "#/repos";
    case "skills": return "#/skills";
    case "settings": return "#/settings";
    case "users": return "#/access/users";
    case "groups": return "#/access/groups";
    case "slack": return "#/connectors/slack";
    case "github": return "#/connectors/github";
    case "asana": return "#/connectors/asana";
    case "atlassian": return "#/connectors/atlassian";
    case "run": return `#/run/${encodeURIComponent(view.assignmentId)}`;
    case "trace": return `#/trace/${encodeURIComponent(view.dimension)}/${encodeURIComponent(view.value)}`;
  }
}

/** Map view name to the sidebar nav item id for active highlighting. */
function viewToNavId(view: View): string {
  switch (view.name) {
    case "fleet": return "fleet";
    case "agents": return "agents";
    case "repos": return "repos";
    case "skills": return "skills";
    case "settings": return "settings";
    case "users": return "users";
    case "groups": return "groups";
    case "slack": return "slack";
    case "github": return "github";
    case "asana": return "asana";
    case "atlassian": return "atlassian";
    // Detail views: highlight Fleet
    case "run": return "fleet";
    case "trace": return "fleet";
  }
}

/** Decode the `cognito:groups` claim. */
function groupsFromProfile(profile: Record<string, unknown> | undefined): string[] {
  const raw = profile?.["cognito:groups"];
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw === "string") return [raw];
  return [];
}

/** GitHub manifest flow redirect normalization. */
function normalizeGitHubAppCallback() {
  if (window.location.pathname.replace(/\/$/, "").endsWith("/github-app-callback")) {
    const search = window.location.search;
    const base = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");
    window.history.replaceState(
      {},
      document.title,
      `${base}/#/settings${search}`,
    );
  }
}

// -- SVG Icons for sidebar nav --
const IconFleet = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <rect x="14" y="14" width="7" height="7" rx="1" />
  </svg>
);
const IconAgents = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3" />
    <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
  </svg>
);
const IconRepos = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
    <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" />
  </svg>
);
const IconSkills = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
  </svg>
);
const IconSlack = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14.5 10c-.83 0-1.5-.67-1.5-1.5v-5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5v5c0 .83-.67 1.5-1.5 1.5z" />
    <path d="M20.5 10H19V8.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5-.67 1.5-1.5 1.5z" />
    <path d="M9.5 14c.83 0 1.5.67 1.5 1.5v5c0 .83-.67 1.5-1.5 1.5S8 21.33 8 20.5v-5c0-.83.67-1.5 1.5-1.5z" />
    <path d="M3.5 14H5v1.5c0 .83-.67 1.5-1.5 1.5S2 16.33 2 15.5 2.67 14 3.5 14z" />
    <path d="M14 14.5c0-.83.67-1.5 1.5-1.5h5c.83 0 1.5.67 1.5 1.5s-.67 1.5-1.5 1.5h-5c-.83 0-1.5-.67-1.5-1.5z" />
    <path d="M14 20.5c0 .83-.67 1.5-1.5 1.5s-1.5-.67-1.5-1.5.67-1.5 1.5-1.5h1.5v1.5z" />
    <path d="M10 9.5C10 10.33 9.33 11 8.5 11h-5C2.67 11 2 10.33 2 9.5S2.67 8 3.5 8h5c.83 0 1.5.67 1.5 1.5z" />
    <path d="M10 3.5C10 2.67 10.67 2 11.5 2S13 2.67 13 3.5 12.33 5 11.5 5H10V3.5z" />
  </svg>
);
const IconGitHub = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 00-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0020 4.77 5.07 5.07 0 0019.91 1S18.73.65 16 2.48a13.38 13.38 0 00-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 005 4.77a5.44 5.44 0 00-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 009 18.13V22" />
  </svg>
);
const IconAsana = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="6" r="4" />
    <circle cx="6" cy="16" r="4" />
    <circle cx="18" cy="16" r="4" />
  </svg>
);
const IconAtlassian = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M7.5 11.5 2 21h7l3-6-4.5-3.5z" />
    <path d="M12 3 8.5 9.5 15 21h7L12 3z" />
  </svg>
);
const IconUsers = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M23 21v-2a4 4 0 00-3-3.87" />
    <path d="M16 3.13a4 4 0 010 7.75" />
  </svg>
);
const IconGroups = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M22 21v-2a4 4 0 00-3-3.87" />
    <path d="M16 3.13a4 4 0 010 7.75" />
  </svg>
);
const IconSettings = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z" />
  </svg>
);

/** Build the admin nav items (visible to admins only). */
function buildAdminNav(): NavItem[] {
  return [
    { id: "agents", label: "Agents", hash: "#/agents", icon: <IconAgents />, adminOnly: true },
    { id: "repos", label: "Repositories", hash: "#/repos", icon: <IconRepos />, adminOnly: true },
    { id: "skills", label: "Skills", hash: "#/skills", icon: <IconSkills />, adminOnly: true },
    { id: "slack", label: "Slack", hash: "#/connectors/slack", icon: <IconSlack />, section: "CONNECTORS", adminOnly: true },
    { id: "github", label: "GitHub", hash: "#/connectors/github", icon: <IconGitHub />, adminOnly: true },
    { id: "asana", label: "Asana", hash: "#/connectors/asana", icon: <IconAsana />, adminOnly: true },
    { id: "atlassian", label: "Atlassian", hash: "#/connectors/atlassian", icon: <IconAtlassian />, adminOnly: true },
    { id: "users", label: "Users", hash: "#/access/users", icon: <IconUsers />, section: "ACCESS", adminOnly: true },
    { id: "groups", label: "Groups", hash: "#/access/groups", icon: <IconGroups />, adminOnly: true },
    { id: "settings", label: "Settings", hash: "#/settings", icon: <IconSettings />, adminOnly: true },
  ];
}

export function App({ config }: { config: AppConfig }) {
  const auth = useAuth();
  const api = useApi(config);
  normalizeGitHubAppCallback();

  const [view, setViewState] = useState<View>(() => hashToView(window.location.hash));

  const navigate = useCallback((next: View) => {
    const hash = viewToHash(next);
    if (window.location.hash !== hash) window.location.hash = hash;
    setViewState(next);
  }, []);

  const navigateHash = useCallback((hash: string) => {
    if (window.location.hash !== hash) window.location.hash = hash;
    setViewState(hashToView(hash));
  }, []);

  useEffect(() => {
    const onHashChange = () => setViewState(hashToView(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // GitHub App manifest callback exchange (runs once on mount if the hash matches).
  const exchangeStarted = useRef(false);
  useEffect(() => {
    const hash = window.location.hash;
    if (!hash.includes("github-app/setup-callback") && !hash.includes("settings?code=")) return;
    if (exchangeStarted.current) return;
    exchangeStarted.current = true;
    const q = hash.split("?")[1] ?? "";
    const code = new URLSearchParams(q).get("code");
    if (!code) {
      window.location.hash = "#/settings";
      return;
    }
    api
      .gitHubAppExchange(code)
      .then(() => {
        window.location.hash = "#/connectors/github";
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) void auth.signinRedirect();
        exchangeStarted.current = false;
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (auth.isLoading) {
    return <Centered>Signing in...</Centered>;
  }
  if (auth.error) {
    return (
      <Centered>
        <div className="banner error">Authentication error: {auth.error.message}</div>
        <button className="primary" onClick={() => void auth.signinRedirect()}>
          Try again
        </button>
      </Centered>
    );
  }
  if (!auth.isAuthenticated) {
    return (
      <Centered>
        <h1>SDLC Agent Fleet</h1>
        <p className="muted">Operator sign-in required.</p>
        <button className="primary" onClick={() => void auth.signinRedirect()}>
          Sign in
        </button>
      </Centered>
    );
  }

  const profile = auth.user?.profile as Record<string, unknown> | undefined;
  const groups = groupsFromProfile(profile);
  const isAdmin = groups.includes("admins");
  const isOperator = groups.includes("operators") || isAdmin;
  const email = (profile?.email as string) ?? profile?.sub ?? "operator";

  const signOut = () => {
    void auth.removeUser();
    window.location.href = cognitoLogoutUrl(config);
  };

  const onAuthError = () => void auth.signinRedirect();

  // Build nav items
  const navItems: NavItem[] = [
    { id: "fleet", label: "Fleet", hash: "#/", icon: <IconFleet /> },
    ...(isAdmin ? buildAdminNav() : []),
  ];

  const activeNavId = viewToNavId(view);

  // Check if current view requires admin and user is not admin
  const adminViews = ["agents", "repos", "skills", "settings", "users", "groups", "slack", "github", "asana", "atlassian"];
  const requiresAdmin = adminViews.includes(view.name);

  return (
    <AppShell
      navItems={navItems}
      activeId={activeNavId}
      email={String(email)}
      isAdmin={isAdmin}
      onNavigate={navigateHash}
      onSignOut={signOut}
    >
      {!isOperator && (
        <div className="banner error">
          Your account is not in the <b>operators</b> group. The API will reject data requests
          with 403 — ask an admin to add you.
        </div>
      )}

      {requiresAdmin && !isAdmin ? (
        <div className="banner error">
          Your account is not in the <b>admins</b> group.
        </div>
      ) : (
        <>
          {view.name === "fleet" && (
            <FleetView
              api={api}
              onOpenRun={(assignmentId) => navigate({ name: "run", assignmentId })}
              onTrace={(dimension, value) => navigate({ name: "trace", dimension, value })}
              onAuthError={onAuthError}
            />
          )}
          {view.name === "run" && (
            <RunDetailView
              api={api}
              assignmentId={view.assignmentId}
              onBack={() => navigate({ name: "fleet" })}
              onTrace={(dimension, value) => navigate({ name: "trace", dimension, value })}
              onOpenRun={(assignmentId) => navigate({ name: "run", assignmentId })}
              onAuthError={onAuthError}
            />
          )}
          {view.name === "trace" && (
            <TraceView
              api={api}
              dimension={view.dimension}
              value={view.value}
              onBack={() => navigate({ name: "fleet" })}
              onOpenRun={(assignmentId) => navigate({ name: "run", assignmentId })}
              onAuthError={onAuthError}
            />
          )}
          {view.name === "agents" && (
            <AgentsPage api={api} onAuthError={onAuthError} />
          )}
          {view.name === "repos" && (
            <ReposPage api={api} onAuthError={onAuthError} />
          )}
          {view.name === "skills" && (
            <SkillsPage api={api} onAuthError={onAuthError} />
          )}
          {view.name === "settings" && (
            <SettingsPage api={api} onAuthError={onAuthError} />
          )}
          {view.name === "users" && (
            <UsersPage api={api} onAuthError={onAuthError} />
          )}
          {view.name === "groups" && (
            <GroupsPage api={api} onAuthError={onAuthError} />
          )}
          {view.name === "slack" && (
            <div className="page">
              <div className="page-header">
                <div>
                  <h1>Slack</h1>
                  <p className="page-desc">
                    Workspaces, channel access, trigger rules, and onboarding requests.
                  </p>
                </div>
              </div>
              <SlackConnectorPage api={api} onAuthError={onAuthError} />
            </div>
          )}
          {view.name === "github" && (
            <div className="page">
              <div className="page-header">
                <div>
                  <h1>GitHub</h1>
                  <p className="page-desc">
                    GitHub App connection, installation status, and event activity.
                  </p>
                </div>
              </div>
              <GitHubConnectorPage api={api} onAuthError={onAuthError} />
            </div>
          )}
          {view.name === "asana" && (
            <div className="page">
              <div className="page-header">
                <div>
                  <h1>Asana</h1>
                  <p className="page-desc">
                    Asana connection, access rules, and event activity.
                  </p>
                </div>
              </div>
              <AsanaConnectorPage api={api} onAuthError={onAuthError} />
            </div>
          )}
          {view.name === "atlassian" && (
            <div className="page">
              <div className="page-header">
                <div>
                  <h1>Atlassian</h1>
                  <p className="page-desc">
                    Jira + Confluence: sites, projects/spaces, access rules, automations, and activity.
                  </p>
                </div>
              </div>
              <AtlassianConnectorPage api={api} onAuthError={onAuthError} />
            </div>
          )}
        </>
      )}
    </AppShell>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="center">{children}</div>;
}
