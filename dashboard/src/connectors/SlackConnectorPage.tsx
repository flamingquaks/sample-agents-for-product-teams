// Slack connector page: workspaces, channel allow/deny, WHO trigger rules, the
// channel-onboarding request approval queue, and the access simulator.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../api";
import { usePolling } from "../hooks";
import type {
  CapabilityConfig,
  ChannelPolicy,
  ChannelRequest,
  NotifSub,
  SlackWorkspace,
} from "../types";
import { ActivityPanel } from "./ActivityPanel";
import { ConnectorLayout } from "./ConnectorLayout";
import { TriggerRulesPanel } from "./TriggerRulesPanel";
import type { ConnectorPageProps } from "./registry";

export function SlackConnectorPage({ api, onAuthError }: ConnectorPageProps) {
  return (
    <ConnectorLayout
      label="Slack"
      tabs={[
        { key: "workspaces", label: "Workspaces", render: () => <WorkspacesTab api={api} onAuthError={onAuthError} /> },
        { key: "channels", label: "Channels", render: () => <ChannelsTab api={api} onAuthError={onAuthError} /> },
        { key: "rules", label: "Access rules", render: () => <TriggerRulesPanel api={api} connector="slack" onAuthError={onAuthError} /> },
        { key: "requests", label: "Requests", render: () => <RequestsTab api={api} onAuthError={onAuthError} /> },
        { key: "notifications", label: "Notifications", render: () => <NotificationsTab api={api} onAuthError={onAuthError} /> },
        { key: "simulate", label: "Test access", render: () => <SimulatorTab api={api} onAuthError={onAuthError} /> },
        { key: "activity", label: "Activity", render: () => <ActivityPanel api={api} source="slack" onAuthError={onAuthError} /> },
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
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  // Guided connect flow state
  const [manifest, setManifest] = useState<string | null>(null);
  const [botToken, setBotToken] = useState("");
  const [signingSecret, setSigningSecret] = useState("");

  const showManifest = async () => {
    setErrMsg(null);
    try {
      const r = await api.slackManifest();
      setManifest(JSON.stringify(r.manifest, null, 2));
    } catch (e) {
      handleErr(e);
      setErrMsg(`Failed to build manifest: ${(e as Error).message}`);
    }
  };

  const connect = async () => {
    setBusy(true); setErrMsg(null); setMsg(null);
    try {
      const rec = await api.connectSlackWorkspace({
        bot_token: botToken.trim(),
        signing_secret: signingSecret.trim(),
      });
      setBotToken(""); setSigningSecret(""); setManifest(null);
      setMsg(`Connected workspace "${rec.team_name || rec.team_id}".`);
      poll.refresh();
    } catch (e) {
      handleErr(e);
      setErrMsg((e as Error).message);
    } finally { setBusy(false); }
  };

  const remove = async (id: string) => {
    if (!window.confirm(`Remove workspace ${id}?`)) return;
    setBusy(true);
    try { await api.deleteSlackWorkspace(id); poll.refresh(); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const workspaces = poll.data?.workspaces ?? [];
  const hasWorkspaces = workspaces.length > 0;
  return (
    <div>
      {hasWorkspaces && (
        <table>
          <thead><tr><th>Workspace</th><th>Team ID</th><th>Channel policy</th><th>Status</th><th /></tr></thead>
          <tbody>
            {workspaces.map((w) => (
              <tr key={w.team_id}>
                <td>{w.team_name || "—"}</td>
                <td><code>{w.team_id}</code></td>
                <td>{w.default_channel_policy}</td>
                <td><span className={`pill ${w.status === "active" ? "ok" : "unknown"}`}>{w.status}</span></td>
                <td><button disabled={busy} onClick={() => void remove(w.team_id)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="panel" style={{ marginTop: 16 }}>
        <h4>Connect a workspace</h4>
        <p className="muted">Three steps — all done right here, no CLI needed.</p>
        <ol className="muted" style={{ lineHeight: 1.8 }}>
          <li>
            <button disabled={busy} onClick={() => void showManifest()} style={{ verticalAlign: "middle" }}>
              Get the app manifest
            </button>{" "}
            then{" "}
            <a href="https://api.slack.com/apps?new_app=1&manifest_format=json" target="_blank" rel="noreferrer">
              create the Slack app from it ↗
            </a>
          </li>
          <li>Install the app to your workspace (OAuth & Permissions → Install to Workspace)</li>
          <li>Paste the credentials below (from Basic Information + OAuth pages)</li>
        </ol>
        {manifest && (
          <div style={{ position: "relative" }}>
            <button
              style={{ position: "absolute", top: 4, right: 8, fontSize: 12 }}
              onClick={() => void navigator.clipboard.writeText(manifest)}
            >
              Copy
            </button>
            <pre className="manifest" style={{ maxHeight: 200, overflow: "auto", marginBottom: 12 }}>{manifest}</pre>
          </div>
        )}
        <div className="filters" style={{ flexWrap: "wrap" }}>
          <input
            style={{ flex: "1 1 260px" }}
            type="password"
            placeholder="Signing Secret (Basic Information → App Credentials)"
            value={signingSecret}
            disabled={busy}
            onChange={(e) => setSigningSecret(e.target.value)}
          />
          <input
            style={{ flex: "1 1 320px" }}
            type="password"
            placeholder="Bot Token (xoxb-… from OAuth & Permissions)"
            value={botToken}
            disabled={busy}
            onChange={(e) => setBotToken(e.target.value)}
          />
          <button
            className="primary"
            disabled={busy || !botToken.trim() || !signingSecret.trim()}
            onClick={() => void connect()}
          >
            {busy ? "Connecting…" : "Connect workspace"}
          </button>
        </div>
        {errMsg && <div className="banner error">{errMsg}</div>}
        {msg && <div className="banner ok">{msg}</div>}
      </div>
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
      <p className="muted">Control which channels can trigger agents. A workspace using the allowlist policy (the default) blocks all channels except those listed here.</p>
      <div className="filters">
        <select value={teamId} onChange={(e) => void load(e.target.value)}>
          <option value="">select a workspace…</option>
          {workspaces.map((w) => <option key={w.team_id} value={w.team_id}>{w.team_name || w.team_id}</option>)}
        </select>
      </div>
      {teamId && (
        <>
          <div className="filters">
            <input placeholder="channel ID (right-click channel → Copy link → last segment)" value={channelId} disabled={busy} onChange={(e) => setChannelId(e.target.value)} />
            <input placeholder="#channel-name (optional, for your reference)" value={channelName} disabled={busy} onChange={(e) => setChannelName(e.target.value)} />
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
  const [msg, setMsg] = useState<string | null>(null);
  const [approving, setApproving] = useState<ChannelRequest | null>(null);

  const deny = async (id: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.denyChannelRequest(id);
      poll.refresh();
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const requests = poll.data?.requests ?? [];
  return (
    <div>
      <p className="muted">
        Pending channel-onboarding requests filed by users via <code>/sdlc-onboard-channel</code>.
        Approving grants specific agents to <b>everyone who triggers from that channel</b> — you
        pick which agents on approve. This is a WHO-can-use-WHICH-agent grant; it does not by
        itself scope repositories (agents act on repos per the repo onboarding + co-repo rules,
        and channel <b>notifications</b> are configured separately in the Notifications tab).
      </p>
      {msg && <div className="banner ok">{msg}</div>}
      <table>
        <thead><tr><th>Channel</th><th>Workspace</th><th>Requested by</th><th>Requested agents</th><th /></tr></thead>
        <tbody>
          {requests.map((r) => (
            <tr key={r.request_id}>
              <td>{r.channel_name || r.channel_label || <code>{r.channel_id}</code>}</td>
              <td>{r.workspace_label || <code>{r.team_id}</code>}</td>
              <td>
                {r.requested_by_label && r.requested_by_label !== r.requested_by
                  ? r.requested_by_label
                  : <code>{r.requested_by}</code>}
              </td>
              <td>
                {r.requested_agents.length
                  ? r.requested_agents.join(", ")
                  : <span className="muted">any (you'll choose on approve)</span>}
              </td>
              <td>
                <button className="primary" disabled={busy} onClick={() => { setMsg(null); setApproving(r); }}>Approve…</button>{" "}
                <button disabled={busy} onClick={() => void deny(r.request_id)}>Deny</button>
              </td>
            </tr>
          ))}
          {requests.length === 0 && !poll.loading && (
            <tr><td colSpan={5} className="muted">No pending requests.</td></tr>
          )}
        </tbody>
      </table>

      {approving && (
        <ApproveChannelDialog
          api={api}
          request={approving}
          onClose={() => setApproving(null)}
          onDone={(channel, agents) => {
            setApproving(null);
            setMsg(`Approved ${channel} for: ${agents.join(", ")}.`);
            poll.refresh();
          }}
          onAuthError={onAuthError}
        />
      )}
    </div>
  );
}

// Approve dialog — the backend REQUIRES a concrete (non-wildcard) agent scope,
// so a bare "Approve" of an "any" request 400s. This dialog makes the admin pick
// exactly which fleet agents the channel gets, defaulting to the ones the user
// asked for (minus any wildcard). It loads the live capability list so the admin
// picks from real agents, not free text. It ALSO scopes the channel's approved
// direct-work repos (spec §19) — the set a /sdlc-message-agent dispatch from the
// channel may name; grouped siblings stay reachable via co-repo mechanics only.
function ApproveChannelDialog({
  api, request, onClose, onDone, onAuthError,
}: {
  api: ConnectorPageProps["api"];
  request: ChannelRequest;
  onClose: () => void;
  onDone: (channel: string, agents: string[]) => void;
  onAuthError: () => void;
}) {
  const handleErr = useErr(onAuthError);
  const caps = usePolling<{ capabilities: CapabilityConfig[] }>(() => api.listCapabilities(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const repoPoll = usePolling<{ repos: { repo: string }[] }>(() => api.listRepos(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(request.requested_agents.filter((a) => a && a !== "*")),
  );
  const [selectedRepos, setSelectedRepos] = useState<Set<string>>(
    () => new Set(request.requested_repos ?? []),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  // Only enabled agents are meaningfully grantable; show them first.
  const agents = useMemo(() => {
    const list = caps.data?.capabilities ?? [];
    return [...list].sort((a, b) =>
      Number(b.enabled ?? false) - Number(a.enabled ?? false) ||
      a.agent_id.localeCompare(b.agent_id));
  }, [caps.data]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    if (err) setErr(null);
  };

  const toggleRepo = (repo: string) => {
    setSelectedRepos((prev) => {
      const next = new Set(prev);
      if (next.has(repo)) next.delete(repo); else next.add(repo);
      return next;
    });
  };

  const fleetRepos = repoPoll.data?.repos ?? [];
  const channel = request.channel_name || request.channel_id;
  const submit = async () => {
    const chosen = [...selected];
    if (chosen.length === 0) {
      setErr("Pick at least one agent — a channel grant must name concrete agents.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.approveChannelRequest(request.request_id, chosen, [...selectedRepos]);
      onDone(channel, chosen);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="approve-title"
           onClick={(e) => e.stopPropagation()}>
        <h3 id="approve-title">Approve #{channel}</h3>
        <p className="muted">
          Choose which agents anyone in <b>#{channel}</b> may trigger. This creates a permit for
          each — the channel is allowed and the grants take effect immediately.
        </p>

        {caps.data === null && !caps.error && <p className="muted">Loading agents…</p>}
        {caps.error && <div className="banner error">Could not load agents: {caps.error}</div>}

        <div className="repo-picker" role="listbox" aria-multiselectable="true">
          {agents.map((c) => (
            <label key={c.agent_id} className="field-inline" style={{ display: "block" }}>
              <input
                type="checkbox"
                checked={selected.has(c.agent_id)}
                disabled={busy}
                onChange={() => toggle(c.agent_id)}
              />{" "}
              <code>{c.agent_id}</code>
              {c.description && <span className="muted"> — {c.description}</span>}
              {!c.enabled && <span className="muted"> · disabled</span>}
            </label>
          ))}
          {agents.length === 0 && caps.data !== null && (
            <p className="muted">No agents onboarded yet — onboard an agent first.</p>
          )}
        </div>

        <p className="muted" style={{ marginTop: 12, marginBottom: 4 }}>
          <b>Repositories this channel may work on</b> — what <code>@sdlc-agents</code> mentions
          and <code>/sdlc-message-agent</code>{" "}
          from #{channel} can target. Repos grouped with an approved repo are reachable by the
          agent while working an approved repo, but can't be targeted directly unless approved
          here. Leave empty for agent-only (no-repo) work.
        </p>
        <div className="repo-picker" role="listbox" aria-multiselectable="true">
          {fleetRepos.map((r) => (
            <label key={r.repo} className="field-inline" style={{ display: "block" }}>
              <input
                type="checkbox"
                checked={selectedRepos.has(r.repo)}
                disabled={busy}
                onChange={() => toggleRepo(r.repo)}
              />{" "}
              <code>{r.repo}</code>
              {(request.requested_repos ?? []).includes(r.repo) && (
                <span className="muted"> · requested</span>
              )}
            </label>
          ))}
          {fleetRepos.length === 0 && repoPoll.data !== null && (
            <p className="muted">No repositories onboarded to the fleet yet.</p>
          )}
        </div>

        {err && <div className="banner error" role="alert">{err}</div>}

        <div className="modal-actions">
          <button disabled={busy} onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy || selected.size === 0} onClick={() => void submit()}>
            {busy ? "Approving…" : `Approve for ${selected.size || ""} ${selected.size === 1 ? "agent" : "agents"}`}
          </button>
        </div>
      </div>
    </div>
  );
}

function NotificationsTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const poll = usePolling<{ subscriptions: NotifSub[] }>(() => api.listNotifSubs(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [busy, setBusy] = useState(false);

  const remove = async (teamId: string, channelId: string) => {
    if (!window.confirm(`Remove notifications for ${channelId}?`)) return;
    setBusy(true);
    try { await api.deleteNotifSub(teamId, channelId); poll.refresh(); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  // §18.5: admins can adjust the severity floor of a self-configured subscription
  // in-dashboard (the tier/event/repo detail is edited by the channel via the
  // /sdlc-notify modal; the floor is the one knob worth an admin override here).
  const setFloor = async (s: NotifSub, floor: NotifSub["min_severity"]) => {
    setBusy(true);
    try {
      await api.upsertNotifSub({
        team_id: s.team_id, channel_id: s.channel_id, repos: s.repos,
        tiers: s.tiers, min_severity: floor,
      });
      poll.refresh();
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const tierSummary = (s: NotifSub) =>
    (["actionable", "informative", "error"] as const)
      .filter((t) => (s.tiers[t] ?? []).length)
      .map((t) => `${t} (${s.tiers[t]!.length})`)
      .join(", ") || "—";

  const subs = poll.data?.subscriptions ?? [];
  return (
    <div>
      <p className="muted">
        Channels self-configure notifications with <code>/sdlc-notify</code> (three tiers:
        actionable, informative, error). Admins can review and remove subscriptions here — a
        subscription only receives, it grants no access.
      </p>
      <table>
        <thead><tr><th>Channel</th><th>Workspace</th><th>Repos</th><th>Tiers</th><th>Floor</th><th /></tr></thead>
        <tbody>
          {subs.map((s) => (
            <tr key={`${s.team_id}#${s.channel_id}`}>
              <td>{s.channel_label && s.channel_label !== s.channel_id ? s.channel_label : <code>{s.channel_id}</code>}</td>
              <td>{s.workspace_label || <code>{s.team_id}</code>}</td>
              <td>{s.repos.length ? s.repos.join(", ") : "—"}</td>
              <td>{tierSummary(s)}</td>
              <td>
                <select
                  value={s.min_severity}
                  disabled={busy}
                  onChange={(e) => void setFloor(s, e.target.value as NotifSub["min_severity"])}
                >
                  <option value="informative">informative</option>
                  <option value="actionable">actionable</option>
                  <option value="error">error</option>
                </select>
              </td>
              <td><button disabled={busy} onClick={() => void remove(s.team_id, s.channel_id)}>Remove</button></td>
            </tr>
          ))}
          {subs.length === 0 && !poll.loading && (
            <tr><td colSpan={6} className="muted">No notification subscriptions yet.</td></tr>
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
      <p className="muted">Test whether a specific user would be allowed to trigger an agent from a given channel. Useful for verifying rules before telling users they have access.</p>
      <div className="filters">
        <input placeholder="user identity (e.g. slack:TXXXXXX:UXXXXXX or email)" value={principal} onChange={(e) => setPrincipal(e.target.value)} />
        <input placeholder="agent (e.g. workitems)" value={agentId} onChange={(e) => setAgentId(e.target.value)} />
        <input placeholder="workspace team ID (optional)" value={workspace} onChange={(e) => setWorkspace(e.target.value)} />
        <input placeholder="channel ID (optional)" value={channel} onChange={(e) => setChannel(e.target.value)} />
        <input placeholder="permission groups (comma-separated, optional)" value={groups} onChange={(e) => setGroups(e.target.value)} />
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
