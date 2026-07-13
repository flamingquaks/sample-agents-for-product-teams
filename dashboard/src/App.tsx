// Top-level app: gate on Cognito auth + operator role, then render the fleet
// view. Run-detail and trace are lightweight stubs here — Phase 4 fills them.
// Navigation is a tiny in-memory view state (no router dep) since there are
// only a few views.

import { useState } from "react";
import { useAuth } from "react-oidc-context";
import type { AppConfig } from "./config";
import { cognitoLogoutUrl } from "./auth";
import { useApi } from "./hooks";
import { FleetView } from "./FleetView";

type View =
  | { name: "fleet" }
  | { name: "run"; assignmentId: string }
  | { name: "trace"; dimension: string; value: string };

/** Decode the `cognito:groups` claim to show whether the user is an operator.
 *  The API is the real gate; this only drives a friendly message. */
function groupsFromProfile(profile: Record<string, unknown> | undefined): string[] {
  const raw = profile?.["cognito:groups"];
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw === "string") return [raw];
  return [];
}

export function App({ config }: { config: AppConfig }) {
  const auth = useAuth();
  const api = useApi(config);
  const [view, setView] = useState<View>({ name: "fleet" });

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
  const isOperator = groupsFromProfile(profile).includes("operators");
  const email = (profile?.email as string) ?? profile?.sub ?? "operator";

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
          />
        )}
        {view.name === "run" && (
          <StubView title={`Run ${view.assignmentId}`} onBack={() => setView({ name: "fleet" })} />
        )}
        {view.name === "trace" && (
          <StubView
            title={`Trace ${view.dimension} = ${view.value}`}
            onBack={() => setView({ name: "fleet" })}
          />
        )}
      </main>
    </>
  );
}

function StubView({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <div>
      <button onClick={onBack}>← Back to fleet</button>
      <h2>{title}</h2>
      <p className="muted">Detailed view lands in Phase 4.</p>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="center">{children}</div>;
}
