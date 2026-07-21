// The "Access" sub-page (spec §16–§17): the cross-source user directory +
// onboarding approval queue (Users tab) and permission groups (Groups tab).
// Cross-cutting, not tied to one event source, but it lives in the Connectors
// area because that's where access to the fleet is administered.

import { ConnectorLayout } from "./ConnectorLayout";
import { GroupsPanel } from "./GroupsPanel";
import { UsersPanel } from "./UsersPanel";
import type { ConnectorPageProps } from "./registry";

export function AccessConnectorPage({ api, onAuthError }: ConnectorPageProps) {
  return (
    <ConnectorLayout
      label="Access — Users & Groups"
      onBack={() => (window.location.hash = "#/admin/connectors")}
      tabs={[
        {
          key: "users",
          label: "Users",
          render: () => <UsersPanel api={api} onAuthError={onAuthError} />,
        },
        {
          key: "groups",
          label: "Groups",
          render: () => <GroupsPanel api={api} onAuthError={onAuthError} />,
        },
      ]}
    />
  );
}
