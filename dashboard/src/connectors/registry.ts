// Connector registry — the descriptor list the Connectors index + router
// iterate. Adding a connector is one entry here plus its page component; nothing
// else in the app hardcodes the connector set (mirrors the fleet's
// registry-driven ethos).

import type { ComponentType } from "react";
import type { DashboardApi } from "../api";
import { SlackConnectorPage } from "./SlackConnectorPage";
import { AsanaConnectorPage } from "./AsanaConnectorPage";
import { GitHubConnectorPage } from "./GitHubConnectorPage";

export interface ConnectorPageProps {
  api: DashboardApi;
  onAuthError: () => void;
}

export interface ConnectorDescriptor {
  id: "slack" | "asana" | "github";
  label: string;
  blurb: string;
  Page: ComponentType<ConnectorPageProps>;
}

export const CONNECTORS: ConnectorDescriptor[] = [
  {
    id: "slack",
    label: "Slack",
    blurb:
      "Onboard workspaces, gate channels, and manage who may trigger agents. Users can request channel access with /sdlc-onboard-channel.",
    Page: SlackConnectorPage,
  },
  {
    id: "asana",
    label: "Asana",
    blurb: "Task mentions, assignments, and the Agent custom field trigger agents from Asana.",
    Page: AsanaConnectorPage,
  },
  {
    id: "github",
    label: "GitHub",
    blurb: "The GitHub App delivers issue/PR comment mentions. Register + install it here.",
    Page: GitHubConnectorPage,
  },
];

export function findConnector(id: string): ConnectorDescriptor | undefined {
  return CONNECTORS.find((c) => c.id === id);
}
