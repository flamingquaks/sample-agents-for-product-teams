// GitHub connector page. Wraps the existing GitHubAppPanel (registration +
// install status + the manifest-callback exchange, which AdminView still drives
// on the #/admin hash). Repo onboarding stays in Admin → Fleet config (a GitHub
// resource concern, not the connection), cross-linked here.

import { GitHubAppPanel } from "../GitHubAppPanel";
import { ActivityPanel } from "./ActivityPanel";
import { ConnectorLayout } from "./ConnectorLayout";
import { GitHubAutomationsTab } from "./GitHubAutomationsTab";
import type { ConnectorPageProps } from "./registry";

export function GitHubConnectorPage({ api, onAuthError }: ConnectorPageProps) {
  return (
    <ConnectorLayout
      label="GitHub"
      tabs={[
        {
          key: "connection",
          label: "Connection",
          render: () => (
            <div>
              <GitHubAppPanel api={api} onAuthError={onAuthError} />
              <p className="muted" style={{ marginTop: 12 }}>
                Repositories are onboarded in <b>Admin → Fleet configuration</b>. A mention from a
                non-onboarded repo is rejected at dispatch.
              </p>
            </div>
          ),
        },
        {
          key: "automations",
          label: "Auto-review",
          render: () => <GitHubAutomationsTab api={api} onAuthError={onAuthError} />,
        },
        {
          key: "activity",
          label: "Activity",
          render: () => <ActivityPanel api={api} source="github" onAuthError={onAuthError} />,
        },
      ]}
    />
  );
}
