// Asana connector page. The Asana PAT + webhook secret + bot-user GIDs are set
// out-of-band (scripts/bootstrap_asana_*.py + stack params) today, so this page
// documents the connection and surfaces the shared access-rules axis (trigger
// rules scoped to connector="asana"). A future iteration can make the secrets +
// GID mapping editable here.

import { ActivityPanel } from "./ActivityPanel";
import { ConnectorLayout } from "./ConnectorLayout";
import { TriggerRulesPanel } from "./TriggerRulesPanel";
import type { ConnectorPageProps } from "./registry";

export function AsanaConnectorPage({ api, onAuthError }: ConnectorPageProps) {
  return (
    <ConnectorLayout
      label="Asana"
      tabs={[
        {
          key: "connection",
          label: "Connection",
          render: () => (
            <div>
              <p className="muted">
                Asana credentials (PAT + webhook secret) and the bot-user / Agent-field mapping are
                configured during setup with <code>scripts/bootstrap_asana_webhook.py</code>. Once
                registered, task comment mentions, assignments, and the Agent custom field trigger
                agents.
              </p>
            </div>
          ),
        },
        {
          key: "access",
          label: "Access rules",
          render: () => (
            <TriggerRulesPanel api={api} connector="asana" onAuthError={onAuthError} />
          ),
        },
        {
          key: "activity",
          label: "Activity",
          render: () => <ActivityPanel api={api} source="asana" onAuthError={onAuthError} />,
        },
      ]}
    />
  );
}
