// Connector registry — the descriptor list the Connectors index + router
// iterate. Adding a connector is one entry here plus its page component; nothing
// else in the app hardcodes the connector set (mirrors the fleet's
// registry-driven ethos).

import type { ComponentType } from "react";
import type { DashboardApi } from "../api";
import { SlackConnectorPage } from "./SlackConnectorPage";
import { AsanaConnectorPage } from "./AsanaConnectorPage";
import { GitHubConnectorPage } from "./GitHubConnectorPage";
import { AccessConnectorPage } from "./AccessConnectorPage";

export interface ConnectorPageProps {
  api: DashboardApi;
  onAuthError: () => void;
}

/** Health of a connector, driving the index card badge (spec §9.2/§9.3). */
export type ConnectorHealth = "ok" | "warn" | "unknown";
export interface ConnectorStatus {
  health: ConnectorHealth;
  label: string; // short human summary, e.g. "2 workspaces" / "not connected"
}

export interface ConnectorDescriptor {
  id: "slack" | "asana" | "github" | "access";
  label: string;
  blurb: string;
  requiredRole: "admin";
  icon?: string; // short glyph/emoji for the card (kept simple — no asset pipeline)
  /** Fetches the connector's health for the card badge. Read-only. */
  useStatus?: (api: DashboardApi) => Promise<ConnectorStatus>;
  Page: ComponentType<ConnectorPageProps>;
}

export const CONNECTORS: ConnectorDescriptor[] = [
  {
    id: "slack",
    label: "Slack",
    icon: "💬",
    requiredRole: "admin",
    blurb:
      "Onboard workspaces, gate channels, and manage who may trigger agents. Users can request channel access with /sdlc-onboard-channel.",
    useStatus: async (api) => {
      const { workspaces } = await api.listSlackWorkspaces();
      const active = workspaces.filter((w) => w.enabled && w.status === "active").length;
      if (!workspaces.length) return { health: "unknown", label: "no workspaces onboarded" };
      if (!active) return { health: "warn", label: "workspaces disabled" };
      return { health: "ok", label: `${active} active workspace${active === 1 ? "" : "s"}` };
    },
    Page: SlackConnectorPage,
  },
  {
    id: "asana",
    label: "Asana",
    icon: "✅",
    requiredRole: "admin",
    blurb: "Task mentions, assignments, and the Agent custom field trigger agents from Asana.",
    Page: AsanaConnectorPage,
  },
  {
    id: "github",
    label: "GitHub",
    icon: "🐙",
    requiredRole: "admin",
    blurb: "The GitHub App delivers issue/PR comment mentions. Register + install it here.",
    useStatus: async (api) => {
      const s = await api.gitHubAppStatus();
      return s.configured
        ? { health: "ok", label: s.slug ? `app: ${s.slug}` : "app configured" }
        : { health: "warn", label: "app not registered" };
    },
    Page: GitHubConnectorPage,
  },
  {
    id: "access",
    label: "Access — Users & Groups",
    icon: "👤",
    requiredRole: "admin",
    blurb:
      "The cross-source identity directory, the user-onboarding approval queue, and permission groups. Approve first-touch users and assign groups here.",
    useStatus: async (api) => {
      const { requests } = await api.listUserRequests("pending");
      return requests.length
        ? { health: "warn", label: `${requests.length} pending request${requests.length === 1 ? "" : "s"}` }
        : { health: "ok", label: "no pending requests" };
    },
    Page: AccessConnectorPage,
  },
];

export function findConnector(id: string): ConnectorDescriptor | undefined {
  return CONNECTORS.find((c) => c.id === id);
}
