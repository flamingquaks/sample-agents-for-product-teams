// Atlassian (Jira + Confluence) connector page — atlassian-connector spec §A10.
// ONE page for the suite: guided site connect (paste-token, the Slack pattern),
// per-product enablement + Forge app-install card + delivery verification, Jira
// project / Confluence space onboarding (allow/deny + repo scope + write mode),
// per-product access rules, the automation-rule builder, notification scopes,
// and per-source activity.

import { useCallback, useMemo, useState } from "react";
import { ApiError } from "../api";
import { usePolling } from "../hooks";
import type {
  AtlassianSite,
  AutomationRule,
  CapabilityConfig,
  ConfluenceSpace,
  JiraProject,
  NotifSub,
} from "../types";
import { ActivityPanel } from "./ActivityPanel";
import { ConnectorLayout } from "./ConnectorLayout";
import { TriggerRulesPanel } from "./TriggerRulesPanel";
import type { ConnectorPageProps } from "./registry";

export function AtlassianConnectorPage({ api, onAuthError }: ConnectorPageProps) {
  const [product, setProduct] = useState<"jira" | "confluence">("jira");
  return (
    <ConnectorLayout
      label="Atlassian"
      tabs={[
        { key: "sites", label: "Sites", render: () => <SitesTab api={api} onAuthError={onAuthError} /> },
        { key: "projects", label: "Jira projects", render: () => <ProjectsTab api={api} onAuthError={onAuthError} /> },
        { key: "spaces", label: "Confluence spaces", render: () => <SpacesTab api={api} onAuthError={onAuthError} /> },
        {
          key: "rules", label: "Access rules", render: () => (
            <div>
              <div className="filters">
                <select value={product} onChange={(e) => setProduct(e.target.value as "jira" | "confluence")}>
                  <option value="jira">Jira rules</option>
                  <option value="confluence">Confluence rules</option>
                </select>
              </div>
              <TriggerRulesPanel api={api} connector={product} onAuthError={onAuthError} />
            </div>
          ),
        },
        { key: "automations", label: "Automations", render: () => <AutomationsTab api={api} onAuthError={onAuthError} /> },
        { key: "simulate", label: "Test access", render: () => <SimulatorTab api={api} onAuthError={onAuthError} /> },
        { key: "notifications", label: "Notifications", render: () => <NotificationsTab api={api} onAuthError={onAuthError} /> },
        {
          key: "activity", label: "Activity", render: () => (
            <div>
              <h4>Jira</h4>
              <ActivityPanel api={api} source="jira" onAuthError={onAuthError} />
              <h4 style={{ marginTop: 24 }}>Confluence</h4>
              <ActivityPanel api={api} source="confluence" onAuthError={onAuthError} />
            </div>
          ),
        },
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

// --- Sites (§A12 guided connect) ----------------------------------------------

function SitesTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const poll = usePolling<{ sites: AtlassianSite[] }>(() => api.listAtlassianSites(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const forgePoll = usePolling<{ deployed: boolean; app_id: string; install_link: string }>(
    () => api.atlassianForgeStatus(),
    { isActive: () => false, deps: [api], onError: handleErr },
  );
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [siteUrl, setSiteUrl] = useState("");
  const [botEmail, setBotEmail] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [enableJira, setEnableJira] = useState(true);
  const [enableConfluence, setEnableConfluence] = useState(true);
  const [verifyState, setVerifyState] = useState<Record<string, string>>({});

  const connect = async () => {
    setBusy(true); setErrMsg(null); setMsg(null);
    try {
      const rec = await api.connectAtlassianSite({
        site_url: siteUrl.trim(),
        bot_email: botEmail.trim(),
        api_token: apiToken.trim(),
        products: { jira: enableJira, confluence: enableConfluence },
      });
      setSiteUrl(""); setBotEmail(""); setApiToken("");
      setMsg(`Connected site "${rec.site_name || rec.site_url}". Now install the Forge app (below) and verify delivery.`);
      poll.refresh();
    } catch (e) {
      handleErr(e);
      setErrMsg((e as Error).message);
    } finally { setBusy(false); }
  };

  const toggleProduct = async (s: AtlassianSite, product: "jira" | "confluence") => {
    setBusy(true);
    try {
      await api.setAtlassianProducts(s.site_id, {
        ...s.products,
        [product]: !s.products[product],
      });
      poll.refresh();
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const verify = async (siteId: string, product: "jira" | "confluence") => {
    setBusy(true);
    try {
      const r = await api.verifyAtlassianWebhook(siteId, product);
      const last = r.last_seen as number | null;
      setVerifyState((prev) => ({
        ...prev,
        [`${siteId}:${product}`]: last
          ? `✅ last delivery ${new Date(last * 1000).toLocaleString()}`
          : "⚠️ no deliveries seen yet — install the Forge app, then comment on an issue/page",
      }));
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const remove = async (id: string) => {
    if (!window.confirm(`Remove site ${id}? Onboarded projects/spaces stay but become inert.`)) return;
    setBusy(true);
    try { await api.deleteAtlassianSite(id); poll.refresh(); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const tokenBadge = (s: AtlassianSite) => {
    if (!s.token_expires_at) return null;
    const daysLeft = Math.floor((s.token_expires_at - Date.now() / 1000) / 86400);
    if (daysLeft > 30) return null;
    return (
      <span className={`pill ${daysLeft <= 3 ? "err" : "warn"}`}>
        token {daysLeft <= 0 ? "EXPIRED" : `expires in ${daysLeft}d`}
      </span>
    );
  };

  const sites = poll.data?.sites ?? [];
  return (
    <div>
      {poll.error && <div className="banner error">Could not load sites: {poll.error}</div>}
      {forgePoll.error && (
        <div className="banner error">Could not load Forge install status: {forgePoll.error}</div>
      )}
      {sites.length > 0 && (
        <table>
          <thead><tr><th>Site</th><th>Products</th><th>Delivery</th><th>Status</th><th /></tr></thead>
          <tbody>
            {sites.map((s) => (
              <tr key={s.site_id}>
                <td>
                  <a href={s.site_url} target="_blank" rel="noreferrer">{s.site_name || s.site_url}</a>
                  <div className="muted"><code>{s.site_id}</code></div>
                  {tokenBadge(s)}
                </td>
                <td>
                  {(["jira", "confluence"] as const).map((p) => (
                    <label key={p} className="field-inline" style={{ display: "block" }}>
                      <input type="checkbox" checked={!!s.products[p]} disabled={busy}
                             onChange={() => void toggleProduct(s, p)} /> {p}
                    </label>
                  ))}
                </td>
                <td>
                  {(["jira", "confluence"] as const).map((p) => (
                    <div key={p} style={{ marginBottom: 4 }}>
                      <button disabled={busy || !s.products[p]} onClick={() => void verify(s.site_id, p)}>
                        Verify {p}
                      </button>{" "}
                      <span className="muted">{verifyState[`${s.site_id}:${p}`] ?? ""}</span>
                    </div>
                  ))}
                </td>
                <td><span className={`pill ${s.status === "active" ? "ok" : "unknown"}`}>{s.status}</span></td>
                <td><button disabled={busy} onClick={() => void remove(s.site_id)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="panel" style={{ marginTop: 16 }}>
        <h4>Connect a site</h4>
        <p className="muted">~10 minutes for both products — no CLI for the site itself.</p>
        <ol className="muted" style={{ lineHeight: 1.8 }}>
          <li>
            In Atlassian admin: create an <code>sdlc-agents</code> <b>service account</b>, grant it the
            target projects' work permissions + spaces' page permissions, and mint a <b>scoped API
            token</b> (max 1 year) from{" "}
            <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noreferrer">
              id.atlassian.com ↗
            </a>
          </li>
          <li>Paste the site URL + service-account email + token below → <b>Connect</b></li>
          <li>
            <b>Install the events app</b> on the site:{" "}
            {forgePoll.data?.deployed ? (
              <>
                <a href={forgePoll.data.install_link} target="_blank" rel="noreferrer">
                  open the private install link ↗
                </a>{" "}
                (read-only event scopes; sole egress = this fleet's webhook API — shown up front;
                no secret changes hands). One install covers both products. Then <b>Verify
                delivery</b> above.
              </>
            ) : (
              <span>
                the <code>atlassian-events</code> forwarder isn't deployed yet — it ships with{" "}
                <code>scripts/deploy_fleet.py</code> when the Forge CLI is logged in, or run{" "}
                <code>scripts/deploy_forge_atlassian.py</code> once. The install link appears here
                automatically after that.
              </span>
            )}
          </li>
          <li>Onboard Jira projects / Confluence spaces in their tabs; grant access in Access rules.</li>
        </ol>
        <div className="filters" style={{ flexWrap: "wrap" }}>
          <input style={{ flex: "1 1 260px" }} placeholder="Site URL (https://your-site.atlassian.net)"
                 value={siteUrl} disabled={busy} onChange={(e) => setSiteUrl(e.target.value)} />
          <input style={{ flex: "1 1 240px" }} placeholder="Service-account email"
                 value={botEmail} disabled={busy} onChange={(e) => setBotEmail(e.target.value)} />
          <input style={{ flex: "1 1 280px" }} type="password" placeholder="Scoped API token"
                 value={apiToken} disabled={busy} onChange={(e) => setApiToken(e.target.value)} />
        </div>
        <div className="filters">
          <label className="field-inline">
            <input type="checkbox" checked={enableJira} disabled={busy}
                   onChange={(e) => setEnableJira(e.target.checked)} /> Jira
          </label>
          <label className="field-inline">
            <input type="checkbox" checked={enableConfluence} disabled={busy}
                   onChange={(e) => setEnableConfluence(e.target.checked)} /> Confluence
          </label>
          <button className="primary"
                  disabled={busy || !siteUrl.trim() || !botEmail.trim() || !apiToken.trim()}
                  onClick={() => void connect()}>
            {busy ? "Connecting…" : "Connect site"}
          </button>
        </div>
        {errMsg && <div className="banner error">{errMsg}</div>}
        {msg && <div className="banner ok">{msg}</div>}
      </div>
    </div>
  );
}

// --- shared site picker ---------------------------------------------------------

function useSites(api: ConnectorPageProps["api"], onAuthError: () => void) {
  const handleErr = useErr(onAuthError);
  return usePolling<{ sites: AtlassianSite[] }>(() => api.listAtlassianSites(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
}

// --- Jira projects ---------------------------------------------------------------

function ProjectsTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const sitesPoll = useSites(api, onAuthError);
  const repoPoll = usePolling<{ repos: { repo: string }[] }>(() => api.listRepos(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [siteId, setSiteId] = useState("");
  const [projects, setProjects] = useState<JiraProject[]>([]);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"allow" | "deny">("allow");
  const [repos, setRepos] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  const load = async (sid: string) => {
    setSiteId(sid);
    if (!sid) { setProjects([]); return; }
    try { setProjects((await api.listJiraProjects(sid)).projects); }
    catch (e) { handleErr(e); }
  };
  const add = async () => {
    setBusy(true); setErrMsg(null);
    try {
      await api.putJiraProject({
        site_id: siteId, project_key: key.trim().toUpperCase(), mode,
        project_name: name.trim(), repos: [...repos],
      });
      setKey(""); setName(""); setRepos(new Set()); await load(siteId);
    } catch (e) { handleErr(e); setErrMsg((e as Error).message); }
    finally { setBusy(false); }
  };
  const remove = async (k: string) => {
    setBusy(true);
    try { await api.deleteJiraProject(siteId, k); await load(siteId); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const sites = sitesPoll.data?.sites ?? [];
  const fleetRepos = repoPoll.data?.repos ?? [];
  return (
    <div>
      <p className="muted">
        Onboard Jira projects (the WHERE axis). With the default allowlist posture, agents can only
        read/write issues in projects listed <b>allow</b> here. Linked repos are the project's
        co-scope — what a Jira dispatch may reach with GitHub tools.
      </p>
      {sitesPoll.error && <div className="banner error">Could not load sites: {sitesPoll.error}</div>}
      <div className="filters">
        <select value={siteId} onChange={(e) => void load(e.target.value)}>
          <option value="">select a site…</option>
          {sites.map((s) => <option key={s.site_id} value={s.site_id}>{s.site_name || s.site_url}</option>)}
        </select>
      </div>
      {siteId && (
        <>
          <div className="filters" style={{ flexWrap: "wrap" }}>
            <input placeholder="Project key (e.g. ENG)" value={key} disabled={busy}
                   onChange={(e) => setKey(e.target.value)} style={{ width: 140 }} />
            <input placeholder="Project name (optional)" value={name} disabled={busy}
                   onChange={(e) => setName(e.target.value)} />
            <select value={mode} disabled={busy} onChange={(e) => setMode(e.target.value as "allow" | "deny")}>
              <option value="allow">allow</option>
              <option value="deny">deny</option>
            </select>
            <button className="primary" disabled={busy || !key.trim()} onClick={() => void add()}>Add</button>
          </div>
          {repoPoll.error && <div className="banner error">Could not load repos: {repoPoll.error}</div>}
          {fleetRepos.length > 0 && (
            <div className="repo-picker" style={{ marginBottom: 12 }}>
              <span className="muted">Linked repos: </span>
              {fleetRepos.map((r) => (
                <label key={r.repo} className="field-inline" style={{ marginRight: 12 }}>
                  <input type="checkbox" checked={repos.has(r.repo)} disabled={busy}
                         onChange={() => setRepos((prev) => {
                           const next = new Set(prev);
                           if (next.has(r.repo)) next.delete(r.repo); else next.add(r.repo);
                           return next;
                         })} /> <code>{r.repo}</code>
                </label>
              ))}
            </div>
          )}
          {errMsg && <div className="banner error">{errMsg}</div>}
          <table>
            <thead><tr><th>Project</th><th>Name</th><th>Mode</th><th>Linked repos</th><th /></tr></thead>
            <tbody>
              {projects.map((p) => (
                <tr key={p.project_key}>
                  <td><code>{p.project_key}</code></td>
                  <td>{p.project_name || "—"}</td>
                  <td><span className={`pill ${p.mode === "allow" ? "ok" : "err"}`}>{p.mode}</span></td>
                  <td>{p.repos.length ? p.repos.join(", ") : "—"}</td>
                  <td><button disabled={busy} onClick={() => void remove(p.project_key)}>Remove</button></td>
                </tr>
              ))}
              {projects.length === 0 && <tr><td colSpan={5} className="muted">No project rules.</td></tr>}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

// --- Confluence spaces ----------------------------------------------------------

function SpacesTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const sitesPoll = useSites(api, onAuthError);
  const capsPoll = usePolling<{ capabilities: CapabilityConfig[] }>(() => api.listCapabilities(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const repoPoll = usePolling<{ repos: { repo: string }[] }>(() => api.listRepos(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [siteId, setSiteId] = useState("");
  const [spaces, setSpaces] = useState<ConfluenceSpace[]>([]);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"allow" | "deny">("allow");
  const [writeMode, setWriteMode] = useState<"propose" | "direct">("propose");
  const [writeAgents, setWriteAgents] = useState<Set<string>>(new Set());
  const [repos, setRepos] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  const load = async (sid: string) => {
    setSiteId(sid);
    if (!sid) { setSpaces([]); return; }
    try { setSpaces((await api.listConfluenceSpaces(sid)).spaces); }
    catch (e) { handleErr(e); }
  };
  const add = async () => {
    setBusy(true); setErrMsg(null);
    try {
      await api.putConfluenceSpace({
        site_id: siteId, space_key: key.trim().toUpperCase(), mode,
        space_name: name.trim(), write_mode: writeMode, write_agents: [...writeAgents],
        repos: [...repos],
      });
      setKey(""); setName(""); setWriteAgents(new Set()); setRepos(new Set()); await load(siteId);
    } catch (e) { handleErr(e); setErrMsg((e as Error).message); }
    finally { setBusy(false); }
  };
  const flipWriteMode = async (sp: ConfluenceSpace) => {
    setBusy(true);
    try {
      await api.putConfluenceSpace({
        site_id: sp.site_id, space_key: sp.space_key, mode: sp.mode,
        space_name: sp.space_name, repos: sp.repos, write_agents: sp.write_agents,
        write_mode: sp.write_mode === "propose" ? "direct" : "propose",
      });
      await load(siteId);
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };
  const remove = async (k: string) => {
    setBusy(true);
    try { await api.deleteConfluenceSpace(siteId, k); await load(siteId); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  const sites = sitesPoll.data?.sites ?? [];
  const fleetRepos = repoPoll.data?.repos ?? [];
  const agents = useMemo(
    () => (capsPoll.data?.capabilities ?? []).map((c) => c.agent_id).sort(),
    [capsPoll.data],
  );
  return (
    <div>
      <p className="muted">
        Onboard Confluence spaces. <b>Onboarding a space makes it readable by ALL agents</b> — a
        confidential space you never onboard is invisible to every agent (reads are broker-scoped,
        unlike GitHub). Writes stay separately gated: <b>propose</b> (default) means agents post
        proposals as comments for a human to apply; <b>direct</b> lets writes land immediately
        (version-guarded + attributed). Write agents optionally narrows writes to listed agents.
      </p>
      {sitesPoll.error && <div className="banner error">Could not load sites: {sitesPoll.error}</div>}
      <div className="filters">
        <select value={siteId} onChange={(e) => void load(e.target.value)}>
          <option value="">select a site…</option>
          {sites.map((s) => <option key={s.site_id} value={s.site_id}>{s.site_name || s.site_url}</option>)}
        </select>
      </div>
      {siteId && (
        <>
          <div className="filters" style={{ flexWrap: "wrap" }}>
            <input placeholder="Space key (e.g. DOCS)" value={key} disabled={busy}
                   onChange={(e) => setKey(e.target.value)} style={{ width: 140 }} />
            <input placeholder="Space name (optional)" value={name} disabled={busy}
                   onChange={(e) => setName(e.target.value)} />
            <select value={mode} disabled={busy} onChange={(e) => setMode(e.target.value as "allow" | "deny")}>
              <option value="allow">allow</option>
              <option value="deny">deny</option>
            </select>
            <select value={writeMode} disabled={busy}
                    onChange={(e) => setWriteMode(e.target.value as "propose" | "direct")}>
              <option value="propose">propose (safe default)</option>
              <option value="direct">direct</option>
            </select>
            <button className="primary" disabled={busy || !key.trim()} onClick={() => void add()}>Add</button>
          </div>
          {agents.length > 0 && (
            <div className="repo-picker" style={{ marginBottom: 12 }}>
              <span className="muted">Write agents (empty = any Cedar-granted agent): </span>
              {agents.map((a) => (
                <label key={a} className="field-inline" style={{ marginRight: 12 }}>
                  <input type="checkbox" checked={writeAgents.has(a)} disabled={busy}
                         onChange={() => setWriteAgents((prev) => {
                           const next = new Set(prev);
                           if (next.has(a)) next.delete(a); else next.add(a);
                           return next;
                         })} /> <code>{a}</code>
                </label>
              ))}
            </div>
          )}
          {repoPoll.error && <div className="banner error">Could not load repos: {repoPoll.error}</div>}
          {fleetRepos.length > 0 && (
            <div className="repo-picker" style={{ marginBottom: 12 }}>
              <span className="muted">Linked repos (the space's co-scope — what a Confluence dispatch may reach with GitHub tools): </span>
              {fleetRepos.map((r) => (
                <label key={r.repo} className="field-inline" style={{ marginRight: 12 }}>
                  <input type="checkbox" checked={repos.has(r.repo)} disabled={busy}
                         onChange={() => setRepos((prev) => {
                           const next = new Set(prev);
                           if (next.has(r.repo)) next.delete(r.repo); else next.add(r.repo);
                           return next;
                         })} /> <code>{r.repo}</code>
                </label>
              ))}
            </div>
          )}
          {errMsg && <div className="banner error">{errMsg}</div>}
          <table>
            <thead><tr><th>Space</th><th>Name</th><th>Mode</th><th>Write mode</th><th>Write agents</th><th>Linked repos</th><th /></tr></thead>
            <tbody>
              {spaces.map((sp) => (
                <tr key={sp.space_key}>
                  <td><code>{sp.space_key}</code></td>
                  <td>{sp.space_name || "—"}</td>
                  <td><span className={`pill ${sp.mode === "allow" ? "ok" : "err"}`}>{sp.mode}</span></td>
                  <td>
                    <span className={`pill ${sp.write_mode === "propose" ? "warn" : "ok"}`}>{sp.write_mode}</span>{" "}
                    <button disabled={busy} onClick={() => void flipWriteMode(sp)}>
                      → {sp.write_mode === "propose" ? "direct" : "propose"}
                    </button>
                  </td>
                  <td>{sp.write_agents.length ? sp.write_agents.join(", ") : "any granted"}</td>
                  <td>{(sp.repos ?? []).length ? sp.repos.join(", ") : "—"}</td>
                  <td><button disabled={busy} onClick={() => void remove(sp.space_key)}>Remove</button></td>
                </tr>
              ))}
              {spaces.length === 0 && <tr><td colSpan={7} className="muted">No space rules.</td></tr>}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

// --- Automations (§A8 rule builder) ----------------------------------------------

const JIRA_EVENTS = ["issue_transitioned", "issue_created", "issue_commented", "issue_assigned"];
const CONFLUENCE_EVENTS = ["page_labeled", "page_created", "page_updated"];
const TEMPLATE_VARS: Record<string, string[]> = {
  jira: ["issue_key", "summary", "project", "status", "from_status", "to_status",
         "issue_type", "reporter", "assignee", "site_url"],
  confluence: ["title", "space", "page_id", "page_url", "label", "author"],
};

function AutomationsTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const poll = usePolling<{ rules: AutomationRule[] }>(() => api.listAutomationRules(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const sitesPoll = useSites(api, onAuthError);
  const capsPoll = usePolling<{ capabilities: CapabilityConfig[] }>(() => api.listCapabilities(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const [connector, setConnector] = useState<"jira" | "confluence">("jira");
  const [event, setEvent] = useState(JIRA_EVENTS[0]);
  const [site, setSite] = useState("*");
  const [container, setContainer] = useState("");
  const [toStatus, setToStatus] = useState("");
  const [label, setLabel] = useState("");
  const [agentId, setAgentId] = useState("");
  const [template, setTemplate] = useState("");
  const [cooldown, setCooldown] = useState("3600");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  const events = connector === "jira" ? JIRA_EVENTS : CONFLUENCE_EVENTS;
  const agents = useMemo(
    () => (capsPoll.data?.capabilities ?? []).filter((c) => c.enabled).map((c) => c.agent_id).sort(),
    [capsPoll.data],
  );

  const resetForm = () => {
    setEditingId(null); setConnector("jira"); setEvent(JIRA_EVENTS[0]); setSite("*");
    setContainer(""); setToStatus(""); setLabel(""); setAgentId(""); setTemplate("");
    setCooldown("3600"); setErrMsg(null);
  };
  // Populate the form from an existing rule (its match is reversed back into the
  // discrete fields), so "edit" reuses the same builder as "create".
  const startEdit = (r: AutomationRule) => {
    // This builder is Atlassian-only; github rules are edited in the GitHub
    // connector's Auto-review tab and never surface here (poll is filtered).
    if (r.connector !== "jira" && r.connector !== "confluence") return;
    setEditingId(r.rule_id);
    setConnector(r.connector);
    setEvent(r.event);
    const m = r.match || {};
    setSite(String(m.site ?? "*") || "*");
    setContainer(String((r.connector === "jira" ? m.project : m.space) ?? ""));
    setToStatus(String(m.to_status ?? ""));
    setLabel(String(m.label ?? ""));
    setAgentId(r.action.agent_id);
    setTemplate(r.action.instruction_template);
    setCooldown(String(r.cooldown_seconds ?? 3600));
    setErrMsg(null);
  };

  const submit = async () => {
    setBusy(true); setErrMsg(null);
    try {
      const match: Record<string, unknown> = { site };
      if (connector === "jira" && container) match.project = container.toUpperCase();
      if (connector === "confluence" && container) match.space = container.toUpperCase();
      if (event === "issue_transitioned" && toStatus) match.to_status = toStatus;
      if (event === "page_labeled" && label) match.label = label;
      const cooldownSeconds = Math.max(0, parseInt(cooldown, 10) || 0);
      if (editingId) {
        await api.updateAutomationRule(editingId, {
          connector, event, match, agent_id: agentId,
          instruction_template: template, cooldown_seconds: cooldownSeconds,
        });
      } else {
        await api.createAutomationRule({
          connector, event, match, agent_id: agentId,
          instruction_template: template, cooldown_seconds: cooldownSeconds,
        });
      }
      resetForm();
      poll.refresh();
    } catch (e) { handleErr(e); setErrMsg((e as Error).message); }
    finally { setBusy(false); }
  };
  const toggle = async (r: AutomationRule) => {
    setBusy(true);
    try { await api.setAutomationRuleEnabled(r.rule_id, !r.enabled); poll.refresh(); }
    catch (e) { handleErr(e); } finally { setBusy(false); }
  };
  const remove = async (r: AutomationRule) => {
    if (!window.confirm("Delete this rule (and its grant)?")) return;
    setBusy(true);
    try {
      await api.deleteAutomationRule(r.rule_id);
      if (editingId === r.rule_id) resetForm();
      poll.refresh();
    } catch (e) { handleErr(e); } finally { setBusy(false); }
  };

  // Atlassian builder shows only jira/confluence rules — github auto-review
  // rules live in the GitHub connector's Auto-review tab.
  const rules = (poll.data?.rules ?? []).filter(
    (r) => r.connector === "jira" || r.connector === "confluence",
  );
  const sites = sitesPoll.data?.sites ?? [];
  return (
    <div>
      <p className="muted">
        Data-driven event → agent rules ("issue moved to <i>Code Review</i> ⇒ run <code>adr</code>";
        "page labeled <code>sdlc-review</code> ⇒ run <code>adr</code>"). Rules dispatch through the
        full authz/guardrail spine under an auditable synthetic principal; creating/enabling a rule
        auto-authors its grant, disabling removes it. Loop brakes: bot-actor guard, per-unit
        cooldown, hourly ceiling, chain-depth cap.
      </p>
      {poll.error && <div className="banner error">Could not load rules: {poll.error}</div>}
      <div className="panel">
        <h4>{editingId ? "Edit rule" : "New rule"}</h4>
        <div className="filters" style={{ flexWrap: "wrap" }}>
          <select value={connector} disabled={busy}
                  onChange={(e) => {
                    const c = e.target.value as "jira" | "confluence";
                    setConnector(c);
                    setEvent(c === "jira" ? JIRA_EVENTS[0] : CONFLUENCE_EVENTS[0]);
                  }}>
            <option value="jira">jira</option>
            <option value="confluence">confluence</option>
          </select>
          <select value={event} disabled={busy} onChange={(e) => setEvent(e.target.value)}>
            {events.map((ev) => <option key={ev} value={ev}>{ev}</option>)}
          </select>
          <select value={site} disabled={busy} onChange={(e) => setSite(e.target.value)}>
            <option value="*">any site</option>
            {sites.map((s) => <option key={s.site_id} value={s.site_id}>{s.site_name || s.site_url}</option>)}
          </select>
          <input placeholder={connector === "jira" ? "project (blank = any)" : "space (blank = any)"}
                 value={container} disabled={busy} onChange={(e) => setContainer(e.target.value)}
                 style={{ width: 160 }} />
          {event === "issue_transitioned" && (
            <input placeholder='to status (e.g. "Code Review")' value={toStatus} disabled={busy}
                   onChange={(e) => setToStatus(e.target.value)} style={{ width: 180 }} />
          )}
          {event === "page_labeled" && (
            <input placeholder="label (e.g. sdlc-review)" value={label} disabled={busy}
                   onChange={(e) => setLabel(e.target.value)} style={{ width: 180 }} />
          )}
          <select value={agentId} disabled={busy} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">run agent…</option>
            {agents.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <label className="field-inline" title="Minimum seconds between fires per unit-of-work (issue/page)">
            cooldown&nbsp;
            <input type="number" min={0} step={60} value={cooldown} disabled={busy}
                   onChange={(e) => setCooldown(e.target.value)} style={{ width: 90 }} />&nbsp;s
          </label>
        </div>
        <textarea
          placeholder={`Instruction template — variables: ${TEMPLATE_VARS[connector].map((v) => `{{${v}}}`).join(" ")}`}
          value={template} disabled={busy} rows={2} style={{ width: "100%", marginTop: 8 }}
          onChange={(e) => setTemplate(e.target.value)}
        />
        <div className="filters" style={{ marginTop: 8 }}>
          <span className="muted">
            Variables: {TEMPLATE_VARS[connector].map((v) => (
              <button key={v} style={{ fontSize: 11, marginRight: 4 }} disabled={busy}
                      onClick={() => setTemplate((t) => `${t}{{${v}}}`)}>
                {`{{${v}}}`}
              </button>
            ))}
          </span>
          <button className="primary" disabled={busy || !agentId || !template.trim()}
                  onClick={() => void submit()}>
            {editingId ? "Save changes" : "Create rule"}
          </button>
          {editingId && (
            <button disabled={busy} onClick={resetForm}>Cancel</button>
          )}
        </div>
        {errMsg && <div className="banner error">{errMsg}</div>}
      </div>

      <table style={{ marginTop: 16 }}>
        <thead><tr><th>Rule</th><th>Match</th><th>Agent</th><th>Cooldown</th><th>Fired</th><th>Enabled</th><th /></tr></thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.rule_id} className={editingId === r.rule_id ? "row-editing" : undefined}>
              <td><code>{r.connector}:{r.event}</code></td>
              <td><code style={{ fontSize: 11 }}>{JSON.stringify(r.match)}</code></td>
              <td><code>{r.action.agent_id}</code></td>
              <td>{r.cooldown_seconds ?? 3600}s</td>
              <td>{r.fire_count ?? 0}×{r.last_fired_at ? ` · last ${new Date(r.last_fired_at * 1000).toLocaleString()}` : ""}</td>
              <td>
                <button disabled={busy} onClick={() => void toggle(r)}>
                  {r.enabled ? "✅ on → disable" : "⏸ off → enable"}
                </button>
              </td>
              <td>
                <button disabled={busy} onClick={() => startEdit(r)}>Edit</button>{" "}
                <button disabled={busy} onClick={() => void remove(r)}>Delete</button>
              </td>
            </tr>
          ))}
          {rules.length === 0 && !poll.loading && (
            <tr><td colSpan={7} className="muted">No automation rules yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// --- Test access (per-source access simulator) -----------------------------------

function SimulatorTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const sitesPoll = useSites(api, onAuthError);
  const [source, setSource] = useState<"jira" | "confluence">("jira");
  const [principal, setPrincipal] = useState("");
  const [agentId, setAgentId] = useState("");
  const [siteId, setSiteId] = useState("");
  const [container, setContainer] = useState("");
  const [groups, setGroups] = useState("");
  const [result, setResult] = useState<{ decision: string; reason: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const sites = sitesPoll.data?.sites ?? [];
  const run = async () => {
    setBusy(true); setResult(null);
    try {
      const key = container.trim().toUpperCase();
      const r = await api.simulateAccess({
        principal: principal.trim(), agent_id: agentId.trim(), source,
        workspace: siteId.trim() || undefined,
        project_key: source === "jira" ? key || undefined : undefined,
        space_key: source === "confluence" ? key || undefined : undefined,
        principal_groups: groups.split(",").map((g) => g.trim()).filter(Boolean),
      });
      setResult(r);
    } catch (e) { handleErr(e); setResult({ decision: "ERROR", reason: (e as Error).message }); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <p className="muted">
        Test whether a person would be allowed to trigger an agent from a given Jira project or
        Confluence space — the same data-driven decision the router makes (grant sets + container
        posture + forbid-wins). Verify rules here before telling users they have access. The
        principal is the Atlassian account id namespaced as <code>atlassian:&lt;accountId&gt;</code>.
      </p>
      {sitesPoll.error && <div className="banner error">Could not load sites: {sitesPoll.error}</div>}
      <div className="filters" style={{ flexWrap: "wrap" }}>
        <select value={source} disabled={busy} onChange={(e) => setSource(e.target.value as "jira" | "confluence")}>
          <option value="jira">jira</option>
          <option value="confluence">confluence</option>
        </select>
        <input placeholder="principal (atlassian:<accountId> or email)" value={principal}
               style={{ flex: "1 1 260px" }} onChange={(e) => setPrincipal(e.target.value)} />
        <input placeholder="agent (e.g. workitems)" value={agentId}
               onChange={(e) => setAgentId(e.target.value)} />
        <select value={siteId} disabled={busy} onChange={(e) => setSiteId(e.target.value)}>
          <option value="">select a site…</option>
          {sites.map((s) => <option key={s.site_id} value={s.site_id}>{s.site_name || s.site_url}</option>)}
        </select>
        <input placeholder={source === "jira" ? "project key (e.g. ENG)" : "space key (e.g. DOCS)"}
               value={container} onChange={(e) => setContainer(e.target.value)} style={{ width: 180 }} />
        <input placeholder="permission groups (comma-separated, optional)" value={groups}
               onChange={(e) => setGroups(e.target.value)} />
        <button className="primary" disabled={busy || !principal || !agentId || !siteId || !container.trim()}
                onClick={() => void run()}>
          Test
        </button>
      </div>
      {result && (
        <div className={`banner ${result.decision === "ALLOW" ? "ok" : "error"}`}>
          <b>{result.decision}</b> — {result.reason}
        </div>
      )}
    </div>
  );
}

// --- Notifications (channel subs with project/space scopes) ----------------------

function NotificationsTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = useErr(onAuthError);
  const poll = usePolling<{ subscriptions: NotifSub[] }>(() => api.listNotifSubs(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const subs = (poll.data?.subscriptions ?? []).filter(
    (s) => (s.projects ?? []).length || (s.spaces ?? []).length ||
           Object.values(s.tiers).some((evts) =>
             (evts ?? []).some((e) =>
               ["agent_replied", "agent_commented", "issue_transitioned", "page_published",
                "automation_fired", "automation_throttled", "doc_proposal_ready"].includes(e))),
  );
  return (
    <div>
      <p className="muted">
        Channel subscriptions with Atlassian events or project/space scopes (channels self-configure
        via <code>/sdlc-notify</code>; users opt into personal DMs with <code>/sdlc-notify me</code> —
        per-user prefs live on Access → Users). All subscriptions are on the Slack page; this view
        filters to Atlassian-relevant ones.
      </p>
      {poll.error && <div className="banner error">Could not load subscriptions: {poll.error}</div>}
      <table>
        <thead><tr><th>Channel</th><th>Projects</th><th>Spaces</th><th>Tiers</th></tr></thead>
        <tbody>
          {subs.map((s) => (
            <tr key={`${s.team_id}#${s.channel_id}`}>
              <td>{s.channel_label || <code>{s.channel_id}</code>}</td>
              <td>{(s.projects ?? []).join(", ") || "—"}</td>
              <td>{(s.spaces ?? []).join(", ") || "—"}</td>
              <td>
                {(["actionable", "informative", "error"] as const)
                  .filter((t) => (s.tiers[t] ?? []).length)
                  .map((t) => `${t} (${s.tiers[t]!.length})`)
                  .join(", ") || "—"}
              </td>
            </tr>
          ))}
          {subs.length === 0 && !poll.loading && (
            <tr><td colSpan={4} className="muted">No Atlassian-scoped subscriptions yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
