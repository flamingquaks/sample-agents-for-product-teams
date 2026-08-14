// GitHub auto-review rules (reviewer-agent spec §auto-trigger). A data-driven
// event → agent builder over the connector-generic automation-rule API, scoped
// to connector="github": "PR opened in acme/web ⇒ run reviewer". Same spine as
// the Atlassian automations tab — rules dispatch through the full authz/guardrail
// path under a synthetic principal, creating/enabling auto-authors the grant,
// and the loop brakes (bot-actor guard, per-PR cooldown, hourly ceiling,
// chain-depth cap) apply unchanged.

import { useMemo, useState } from "react";
import { ApiError } from "../api";
import { usePolling } from "../hooks";
import type { AutomationRule, CapabilityConfig, RepoConfig } from "../types";
import type { ConnectorPageProps } from "./registry";

const GITHUB_EVENTS = ["pull_request.opened", "pull_request.synchronize"];
const GITHUB_TEMPLATE_VARS = [
  "repo", "owner", "repo_name", "pr_number", "action", "author", "title",
  "base_branch", "head_branch", "head_sha", "labels", "pr_url",
];

export function GitHubAutomationsTab({ api, onAuthError }: ConnectorPageProps) {
  const handleErr = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) onAuthError();
  };
  const poll = usePolling<{ rules: AutomationRule[] }>(
    () => api.listAutomationRules("github"),
    { isActive: () => false, deps: [api], onError: handleErr },
  );
  const reposPoll = usePolling<{ repos: RepoConfig[] }>(() => api.listRepos(), {
    isActive: () => false, deps: [api], onError: handleErr,
  });
  const capsPoll = usePolling<{ capabilities: CapabilityConfig[] }>(
    () => api.listCapabilities(),
    { isActive: () => false, deps: [api], onError: handleErr },
  );

  const [event, setEvent] = useState(GITHUB_EVENTS[0]);
  const [repo, setRepo] = useState("");
  const [agentId, setAgentId] = useState("reviewer");
  const [template, setTemplate] = useState(
    "Review pull request #{{pr_number}} in {{repo}}.",
  );
  const [cooldown, setCooldown] = useState("3600");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  const agents = useMemo(
    () => (capsPoll.data?.capabilities ?? [])
      .filter((c) => c.enabled).map((c) => c.agent_id).sort(),
    [capsPoll.data],
  );
  const repos = useMemo(
    () => (reposPoll.data?.repos ?? []).filter((r) => r.enabled).map((r) => r.repo),
    [reposPoll.data],
  );

  const resetForm = () => {
    setEditingId(null); setEvent(GITHUB_EVENTS[0]); setRepo(""); setAgentId("reviewer");
    setTemplate("Review pull request #{{pr_number}} in {{repo}}.");
    setCooldown("3600"); setErrMsg(null);
  };
  const startEdit = (r: AutomationRule) => {
    setEditingId(r.rule_id);
    setEvent(r.event);
    setRepo(String((r.match || {}).repo ?? ""));
    setAgentId(r.action.agent_id);
    setTemplate(r.action.instruction_template);
    setCooldown(String(r.cooldown_seconds ?? 3600));
    setErrMsg(null);
  };

  const submit = async () => {
    setBusy(true); setErrMsg(null);
    try {
      // Blank repo = any onboarded repo; a concrete repo scopes the rule.
      const match: Record<string, unknown> = {};
      if (repo.trim()) match.repo = repo.trim().toLowerCase();
      const cooldownSeconds = Math.max(0, parseInt(cooldown, 10) || 0);
      const body = {
        connector: "github" as const, event, match, agent_id: agentId,
        instruction_template: template, cooldown_seconds: cooldownSeconds,
      };
      if (editingId) await api.updateAutomationRule(editingId, body);
      else await api.createAutomationRule(body);
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

  const rules = poll.data?.rules ?? [];
  return (
    <div>
      <p className="muted">
        Auto-review rules ("PR opened in <code>acme/web</code> ⇒ run <code>reviewer</code>"). A rule
        fires only when the PR author is not the fleet bot and no <code>@mention</code> resolved (a
        mention is explicit intent and wins). Rules dispatch through the full authz/guardrail spine
        under a synthetic principal; creating/enabling auto-authors the grant, disabling removes it.
        Loop brakes: bot-actor guard, per-PR cooldown, hourly ceiling, chain-depth cap.
      </p>
      {poll.error && <div className="banner error">Could not load rules: {poll.error}</div>}
      <div className="panel">
        <h4>{editingId ? "Edit rule" : "New auto-review rule"}</h4>
        <div className="filters" style={{ flexWrap: "wrap" }}>
          <select value={event} disabled={busy} onChange={(e) => setEvent(e.target.value)}>
            {GITHUB_EVENTS.map((ev) => <option key={ev} value={ev}>{ev}</option>)}
          </select>
          <select value={repo} disabled={busy} onChange={(e) => setRepo(e.target.value)}>
            <option value="">any onboarded repo</option>
            {repos.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <select value={agentId} disabled={busy} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">run agent…</option>
            {agents.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <label className="field-inline" title="Minimum seconds between fires per PR">
            cooldown&nbsp;
            <input type="number" min={0} step={60} value={cooldown} disabled={busy}
                   onChange={(e) => setCooldown(e.target.value)} style={{ width: 90 }} />&nbsp;s
          </label>
        </div>
        <textarea
          placeholder={`Instruction template — variables: ${GITHUB_TEMPLATE_VARS.map((v) => `{{${v}}}`).join(" ")}`}
          value={template} disabled={busy} rows={2} style={{ width: "100%", marginTop: 8 }}
          onChange={(e) => setTemplate(e.target.value)}
        />
        <div className="filters" style={{ marginTop: 8 }}>
          <span className="muted">
            Variables: {GITHUB_TEMPLATE_VARS.map((v) => (
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
          {editingId && <button disabled={busy} onClick={resetForm}>Cancel</button>}
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
            <tr><td colSpan={7} className="muted">No auto-review rules yet. Mention <code>@reviewer</code> on a PR works without one.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
