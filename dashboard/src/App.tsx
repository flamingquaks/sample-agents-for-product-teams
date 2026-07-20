// Top-level app: gate on Cognito auth + operator role, then render the fleet /
// run-detail / trace / admin views. Navigation is hash-based (#/, #/admin,
// #/run/<id>, #/trace/<dim>/<value>) so each view has a real URL you can
// bookmark, deep-link, and refresh — no router dependency, and it survives a
// hard refresh on the S3/CloudFront SPA without relying on error-page rewrites.

import { useEffect, useState } from "react";
import { useAuth } from "react-oidc-context";
import type { AppConfig } from "./config";
import { cognitoLogoutUrl } from "./auth";
import { useApi } from "./hooks";
import { FleetView } from "./FleetView";
import { RunDetailView } from "./RunDetailView";
import { TraceView } from "./TraceView";
import { AdminView } from "./AdminView";
import { ConnectorsView } from "./connectors/ConnectorsView";
import { findConnector } from "./connectors/registry";

type View =
  | { name: "fleet" }
  | { name: "run"; assignmentId: string }
  | { name: "trace"; dimension: string; value: string }
  | { name: "admin" }
  | { name: "connectors" }
  | { name: "connector"; id: string };

/** Serialize a view to its URL hash. */
function viewToHash(view: View): string {
  switch (view.name) {
    case "admin":
      return "#/admin";
    case "connectors":
      return "#/admin/connectors";
    case "connector":
      return `#/admin/connectors/${encodeURIComponent(view.id)}`;
    case "run":
      return `#/run/${encodeURIComponent(view.assignmentId)}`;
    case "trace":
      return `#/trace/${encodeURIComponent(view.dimension)}/${encodeURIComponent(view.value)}`;
    default:
      return "#/";
  }
}

/** Parse the current URL hash into a view. Unknown/empty hashes → fleet. */
function hashToView(hash: string): View {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "admin") {
    // Connectors live INSIDE the admin panel: #/admin/connectors[/<id>]. An
    // unknown connector id falls back to the index. (The GitHub App manifest
    // callback path — #/admin/github-app/... — falls through to the admin view,
    // which owns the code exchange.)
    if (parts[1] === "connectors") {
      if (parts[2] && findConnector(parts[2])) return { name: "connector", id: parts[2] };
      return { name: "connectors" };
    }
    return { name: "admin" };
  }
  if (parts[0] === "run" && parts[1]) return { name: "run", assignmentId: parts[1] };
  if (parts[0] === "trace" && parts[1] && parts[2]) {
    return { name: "trace", dimension: parts[1], value: parts[2] };
  }
  return { name: "fleet" };
}

/** Decode the `cognito:groups` claim to show whether the user is an operator.
 *  The API is the real gate; this only drives a friendly message. */
function groupsFromProfile(profile: Record<string, unknown> | undefined): string[] {
  const raw = profile?.["cognito:groups"];
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw === "string") return [raw];
  return [];
}

/** GitHub's manifest flow returns to the fragment-free path /github-app-callback
 *  (a "#..." redirect_url is rejected by GitHub). CloudFront serves that path as
 *  the SPA; here we translate it — once, on load — into the hash route AdminView
 *  already handles (#/admin/github-app/setup-callback?code=...), preserving the
 *  code, so there's a single exchange path.
 *
 *  Rewrites onto the app's OWN base path (import.meta.env.BASE_URL — "/" at the
 *  CloudFront root, or e.g. "/dashboard/" under a subpath), NOT a hardcoded
 *  origin root: a subpath deploy serves index.html/config.json only under that
 *  base, so an origin-root rewrite would land where nothing is served and the
 *  code would never be exchanged. */
function normalizeGitHubAppCallback() {
  if (window.location.pathname.replace(/\/$/, "").endsWith("/github-app-callback")) {
    const search = window.location.search; // ?code=...&state=...
    const base = (import.meta.env.BASE_URL || "/").replace(/\/$/, "");
    window.history.replaceState(
      {},
      document.title,
      `${base}/#/admin/github-app/setup-callback${search}`,
    );
  }
}

export function App({ config }: { config: AppConfig }) {
  const auth = useAuth();
  const api = useApi(config);
  // Translate the GitHub App manifest callback path → hash route before routing.
  normalizeGitHubAppCallback();
  // View is derived from the URL hash so it survives refresh + deep-links.
  const [view, setViewState] = useState<View>(() => hashToView(window.location.hash));

  // Keep the hash in sync when navigating in-app, and react to back/forward or
  // a manually edited hash.
  const navigate = (next: View) => {
    const hash = viewToHash(next);
    if (window.location.hash !== hash) window.location.hash = hash;
    setViewState(next);
  };
  useEffect(() => {
    const onHashChange = () => setViewState(hashToView(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  const setView = navigate;

  if (auth.isLoading) {
    return <Centered>Signing in…</Centered>;
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
        <h1>SDLC Agent Fleet — Dashboard</h1>
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
  // Admins can view the operator surfaces too (the read API accepts either
  // group), so treat an admin as an operator for the not-in-group banner.
  const isOperator = groups.includes("operators") || isAdmin;
  const email = (profile?.email as string) ?? profile?.sub ?? "operator";
  // The admin panel + its nested Connectors sub-pages are all "in admin".
  const inAdmin =
    view.name === "admin" || view.name === "connectors" || view.name === "connector";

  const signOut = () => {
    // Clear local session, then hit Cognito's Hosted-UI logout to end the IdP
    // session too (otherwise the next "sign in" silently re-auths).
    void auth.removeUser();
    window.location.href = cognitoLogoutUrl(config);
  };

  return (
    <>
      <header className="app-header">
        <h1
          style={{ cursor: "pointer" }}
          onClick={() => setView({ name: "fleet" })}
          title="Fleet"
        >
          SDLC Agent Fleet
        </h1>
        <div className="who">
          {isAdmin && (
            <button
              onClick={() => setView(inAdmin ? { name: "fleet" } : { name: "admin" })}
            >
              {inAdmin ? "Fleet" : "Admin"}
            </button>
          )}
          <span>{String(email)}</span>
          <button onClick={signOut}>Sign out</button>
        </div>
      </header>
      <main className="content">
        {!isOperator && (
          <div className="banner error">
            Your account is not in the <b>operators</b> group. The API will reject data requests
            with 403 — ask an admin to add you.
          </div>
        )}
        {view.name === "fleet" && (
          <FleetView
            api={api}
            onOpenRun={(assignmentId) => setView({ name: "run", assignmentId })}
            onTrace={(dimension, value) => setView({ name: "trace", dimension, value })}
            onAuthError={() => void auth.signinRedirect()}
          />
        )}
        {view.name === "run" && (
          <RunDetailView
            api={api}
            assignmentId={view.assignmentId}
            onBack={() => setView({ name: "fleet" })}
            onTrace={(dimension, value) => setView({ name: "trace", dimension, value })}
            onAuthError={() => void auth.signinRedirect()}
          />
        )}
        {view.name === "trace" && (
          <TraceView
            api={api}
            dimension={view.dimension}
            value={view.value}
            onBack={() => setView({ name: "fleet" })}
            onOpenRun={(assignmentId) => setView({ name: "run", assignmentId })}
            onAuthError={() => void auth.signinRedirect()}
          />
        )}
        {inAdmin &&
          (isAdmin ? (
            view.name === "admin" ? (
              <AdminView
                api={api}
                onAuthError={() => void auth.signinRedirect()}
                onOpenConnectors={() => setView({ name: "connectors" })}
              />
            ) : view.name === "connectors" ? (
              <ConnectorsView onOpen={(id) => setView({ name: "connector", id })} />
            ) : (
              <ConnectorPageHost
                id={(view as { id: string }).id}
                api={api}
                onAuthError={() => void auth.signinRedirect()}
              />
            )
          ) : (
            <div className="banner error">
              Your account is not in the <b>admins</b> group.
            </div>
          ))}
      </main>
    </>
  );
}

/** Resolve the connector id to its page from the registry (unknown → index). */
function ConnectorPageHost({
  id,
  api,
  onAuthError,
}: {
  id: string;
  api: import("./api").DashboardApi;
  onAuthError: () => void;
}) {
  const descriptor = findConnector(id);
  if (!descriptor) {
    window.location.hash = "#/admin/connectors";
    return null;
  }
  const Page = descriptor.Page;
  return <Page api={api} onAuthError={onAuthError} />;
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="center">{children}</div>;
}
