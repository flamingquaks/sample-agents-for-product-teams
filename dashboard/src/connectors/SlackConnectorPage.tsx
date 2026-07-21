// Slack connector page: workspaces, channel allow/deny, WHO trigger rules, the
// channel-onboarding request approval queue, and the access simulator.

import { useCallback, useState } from "react";
import { ApiError } from "../api";
import { usePolling } from "../hooks";
import type { ChannelPolicy, ChannelRequest, SlackWorkspace } from "../types";
import { ConnectorLayout } from "./ConnectorLayout";
import { TriggerRulesPanel } from "./TriggerRulesPanel";
import type { ConnectorPageProps } from "./registry";

export function SlackConnectorPage({ api, onAuthError }: ConnectorPageProps) {
  return (
    <ConnectorLayout
      label="Slack"
      onBack={() => (window.location.hash = "#/admin/connectors")}
      tabs={[
        { key: "workspaces", label: "Workspaces", render: () => <WorkspacesTab api={api} onAuthError={onAuthError} /> },
        { key: "channels", label: "Channels", render: () => <ChannelsTab api={api} onAuthError={onAuthError} /> },
        { key: "rules", label: "Access rules", render: () => <TriggerRulesPanel api={api} connector="slack" onAuthError={onAuthError} /> },
        { key: "requests", label: "Requests", render: () => <RequestsTab api={api} onAuthError={onAuthError} /> },
        { key: "simulate", label: "Test access", render: () => <SimulatorTab api={api} onAuthError={onAuthError} /> },
      ]}
    />
  );
}

function useErr(onAuthError: () => void) {
  return useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );
}

function WorkspacesTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const poll = usePolling<{ workspaces: SlackWorkspace[] }>(() => api.listSlackWorkspaces(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [teamId, setTeamId] = useState("");
  const [teamName, setTeamName] = useState("");
  const [policy, setPolicy] = useState<"allowlist" | "denylist">("allowlist");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const add = async () => {
    setBusy(true); setMsg(null);
    try {
      await api.onboardSlackWorkspace({ team_id: teamId.trim(), team_name: teamName.trim(), default_channel_policy: policy });
      setTeamId(""); setTeamName(""); poll.refresh();
    } catch (e) { handleErr(e); setMsg(`Failed: ${(e as Error).message}`); }
    finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    if (!window.confirm(`Remove workspace ${id}?`)) return;
    setBusy(true);
    try { await api.deleteSlackWorkspace(id); poll.refresh(); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const workspaces = poll.data?.workspaces ?? [];
  return (
    <div>
      <p className="muted">
        Onboard each Slack workspace the fleet serves. Store its signing secret + bot token with
        <code> scripts/bootstrap_slack.py</code>, then point the Slack app at the endpoints shown in
        the stack outputs.
      </p>
      <div className="filters">
        <input placeholder="team id (T…)" value={teamId} disabled={busy} onChange={(e) => setTeamId(e.target.value)} />
        <input placeholder="workspace name" value={teamName} disabled={busy} onChange={(e) => setTeamName(e.target.value)} />
        <select value={policy} disabled={busy} onChange={(e) => setPolicy(e.target.value as "allowlist" | "denylist")}>
          <option value="allowlist">allowlist (default-deny channels)</option>
          <option value="denylist">denylist (default-allow channels)</option>
        </select>
        <button className="primary" disabled={busy} onClick={() => void add()}>Onboard workspace</button>
      </div>
      {msg && <div className="banner error">{msg}</div>}
      <table>
        <thead><tr><th>Team</th><th>Name</th><th>Channel policy</th><th>Status</th><th /></tr></thead>
        <tbody>
          {workspaces.map((w) => (
            <tr key={w.team_id}>
              <td><code>{w.team_id}</code></td>
              <td>{w.team_name || "—"}</td>
              <td>{w.default_channel_policy}</td>
              <td><span className={`pill ${w.status === "active" ? "ok" : "unknown"}`}>{w.status}</span></td>
              <td><button disabled={busy} onClick={() => void remove(w.team_id)}>Remove</button></td>
            </tr>
          ))}
          {workspaces.length === 0 && !poll.loading && (
            <tr><td colSpan={5} className="muted">No workspaces onboarded.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function ChannelsTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const wsPoll = usePolling<{ workspaces: SlackWorkspace[] }>(() => api.listSlackWorkspaces(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [teamId, setTeamId] = useState("");
  const [channels, setChannels] = useState<ChannelPolicy[]>([]);
  const [channelId, setChannelId] = useState("");
  const [channelName, setChannelName] = useState("");
  const [mode, setMode] = useState<"allow" | "deny">("allow");
  const [busy, setBusy] = useState(false);

  const load = async (tid: string) => {
    setTeamId(tid);
    if (!tid) { setChannels([]); return; }
    try { setChannels((await api.listChannels(tid)).channels); }
    catch (e) { handleErr(e); }
  };
  const add = async () => {
    setBusy(true);
    try {
      await api.putChannelPolicy({ team_id: teamId, channel_id: channelId.trim(), mode, channel_name: channelName.trim() });
      setChannelId(""); setChannelName(""); await load(teamId);
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };
  const remove = async (cid: string) => {
    setBusy(true);
    try { await api.deleteChannelPolicy(teamId, cid); await load(teamId); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const workspaces = wsPoll.data?.workspaces ?? [];
  return (
    <div>
      <p className="muted">The WHERE axis: which channels may trigger agents, per the workspace’s policy.</p>
      <div className="filters">
        <select value={teamId} onChange={(e) => void load(e.target.value)}>
          <option value="">select a workspace…</option>
          {workspaces.map((w) => <option key={w.team_id} value={w.team_id}>{w.team_name || w.team_id}</option>)}
        </select>
      </div>
      {teamId && (
        <>
          <div className="filters">
            <input placeholder="channel id (C…)" value={channelId} disabled={busy} onChange={(e) => setChannelId(e.target.value)} />
            <input placeholder="#channel-name" value={channelName} disabled={busy} onChange={(e) => setChannelName(e.target.value)} />
            <select value={mode} disabled={busy} onChange={(e) => setMode(e.target.value as "allow" | "deny")}>
              <option value="allow">allow</option>
              <option value="deny">deny</option>
            </select>
            <button className="primary" disabled={busy} onClick={() => void add()}>Add</button>
          </div>
          <table>
            <thead><tr><th>Channel</th><th>Name</th><th>Mode</th><th /></tr></thead>
            <tbody>
              {channels.map((c) => (
                <tr key={c.channel_id}>
                  <td><code>{c.channel_id}</code></td>
                  <td>{c.channel_name || "—"}</td>
                  <td><span className={`pill ${c.mode === "allow" ? "ok" : "err"}`}>{c.mode}</span></td>
                  <td><button disabled={busy} onClick={() => void remove(c.channel_id)}>Remove</button></td>
                </tr>
              ))}
              {channels.length === 0 && <tr><td colSpan={4} className="muted">No channel rules.</td></tr>}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

function RequestsTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const poll = usePolling<{ requests: ChannelRequest[] }>(() => api.listChannelRequests("pending"), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [busy, setBusy] = useState(false);

  const decide = async (id: string, approve: boolean) => {
    setBusy(true);
    try {
      if (approve) await api.approveChannelRequest(id);
      else await api.denyChannelRequest(id);
      poll.refresh();
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const requests = poll.data?.requests ?? [];
  return (
    <div>
      <p className="muted">
        Pending channel-onboarding requests filed by users via <code>/sdlc-onboard-channel</code>.
        Approving allows the channel and grants the requested agents; denying just records the
        decision.
      </p>
      <table>
        <thead><tr><th>Channel</th><th>Workspace</th><th>Requested by</th><th>Agents</th><th /></tr></thead>
        <tbody>
          {requests.map((r) => (
            <tr key={r.request_id}>
              <td>{r.channel_name || <code>{r.channel_id}</code>}</td>
              <td><code>{r.team_id}</code></td>
              <td><code>{r.requested_by}</code></td>
              <td>{r.requested_agents.length ? r.requested_agents.join(", ") : "any"}</td>
              <td>
                <button className="primary" disabled={busy} onClick={() => void decide(r.request_id, true)}>Approve</button>{" "}
                <button disabled={busy} onClick={() => void decide(r.request_id, false)}>Deny</button>
              </td>
            </tr>
          ))}
          {requests.length === 0 && !poll.loading && (
            <tr><td colSpan={5} className="muted">No pending requests.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function SimulatorTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const [principal, setPrincipal] = useState("");
  const [agentId, setAgentId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [channel, setChannel] = useState("");
  const [groups, setGroups] = useState("");
  const [result, setResult] = useState<{ decision: string; reason: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true); setResult(null);
    try {
      const r = await api.simulateAccess({
        principal: principal.trim(), agent_id: agentId.trim(),
        workspace: workspace.trim() || undefined, channel_id: channel.trim() || undefined,
        principal_groups: groups.split(",").map((g) => g.trim()).filter(Boolean),
      });
      setResult(r);
    } catch (e) { handleErr(e); setResult({ decision: "ERROR", reason: (e as Error).message }); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <p className="muted">Dry-run a trigger decision to confirm the right users get access — or a proper reject.</p>
      <div className="filters">
        <input placeholder="principal (slack:T…:U…)" value={principal} onChange={(e) => setPrincipal(e.target.value)} />
        <input placeholder="agent id" value={agentId} onChange={(e) => setAgentId(e.target.value)} />
        <input placeholder="workspace (T…)" value={workspace} onChange={(e) => setWorkspace(e.target.value)} />
        <input placeholder="channel (C…)" value={channel} onChange={(e) => setChannel(e.target.value)} />
        <input placeholder="groups (comma-sep)" value={groups} onChange={(e) => setGroups(e.target.value)} />
        <button className="primary" disabled={busy || !principal || !agentId} onClick={() => void run()}>Test</button>
      </div>
      {result && (
        <div className={`banner ${result.decision === "ALLOW" ? "ok" : "error"}`}>
          <b>{result.decision}</b> — {result.reason}
        </div>
      )}
    </div>
  );
}
