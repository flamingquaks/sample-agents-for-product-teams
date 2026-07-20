// The WHO grant-rules panel for a connector: list + create + delete permit/forbid
// rules (subject → agent → workspace). Shared by the Slack + Asana pages.

import { useCallback, useState } from "react";
import { ApiError, type DashboardApi } from "../api";
import { usePolling } from "../hooks";
import type { TriggerRule } from "../types";

export function TriggerRulesPanel({
  api,
  connector,
  onAuthError,
}: {
  api: DashboardApi;
  connector: string;
  onAuthError: () => void;
}) {
  const handleErr = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );
  const poll = usePolling<{ rules: TriggerRule[] }>(() => api.listTriggerRules(connector), {
    isActive: () => false,
    deps: [api, connector],
    onError: handleErr,
  });

  const [subjectType, setSubjectType] = useState<"user" | "group">("user");
  const [subjectId, setSubjectId] = useState("");
  const [agentId, setAgentId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [effect, setEffect] = useState<"permit" | "forbid">("permit");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const create = async () => {
    if (!subjectId.trim()) {
      setMsg("Subject is required.");
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.createTriggerRule({
        connector,
        subject_type: subjectType,
        subject_id: subjectId.trim(),
        agent_id: agentId.trim() || "*",
        workspace: workspace.trim() || "*",
        effect,
      });
      setSubjectId("");
      poll.refresh();
    } catch (e) {
      handleErr(e);
      setMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (ruleId: string) => {
    setBusy(true);
    try {
      await api.deleteTriggerRule(ruleId);
      poll.refresh();
    } catch (e) {
      handleErr(e);
    } finally {
      setBusy(false);
    }
  };

  const rules = poll.data?.rules ?? [];
  return (
    <div>
      <p className="muted">
        Grant (permit) or block (forbid) who may trigger which agent. A <code>forbid</code> wins
        over any <code>permit</code>. Leave agent/workspace blank for “any”.
      </p>
      <div className="filters">
        <select value={subjectType} disabled={busy}
                onChange={(e) => setSubjectType(e.target.value as "user" | "group")}>
          <option value="user">user</option>
          <option value="group">group</option>
        </select>
        <input placeholder="subject id (slack:T…:U… or group name)" value={subjectId}
               disabled={busy} onChange={(e) => setSubjectId(e.target.value)} />
        <input placeholder="agent (blank = any)" value={agentId}
               disabled={busy} onChange={(e) => setAgentId(e.target.value)} />
        <input placeholder="workspace (blank = any)" value={workspace}
               disabled={busy} onChange={(e) => setWorkspace(e.target.value)} />
        <select value={effect} disabled={busy}
                onChange={(e) => setEffect(e.target.value as "permit" | "forbid")}>
          <option value="permit">permit</option>
          <option value="forbid">forbid</option>
        </select>
        <button className="primary" disabled={busy} onClick={() => void create()}>
          Add rule
        </button>
      </div>
      {msg && <div className="banner error">{msg}</div>}
      <table>
        <thead>
          <tr><th>Effect</th><th>Subject</th><th>Agent</th><th>Workspace</th><th /></tr>
        </thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.rule_id}>
              <td><span className={`pill ${r.effect === "permit" ? "ok" : "err"}`}>{r.effect}</span></td>
              <td><code>{r.subject_id}</code> <span className="muted">({r.subject_type})</span></td>
              <td>{r.agent_id}</td>
              <td>{r.workspace}</td>
              <td><button disabled={busy} onClick={() => void remove(r.rule_id)}>Remove</button></td>
            </tr>
          ))}
          {rules.length === 0 && !poll.loading && (
            <tr><td colSpan={5} className="muted">No rules yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
