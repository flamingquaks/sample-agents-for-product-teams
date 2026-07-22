// Capabilities panel (admins only): author, view, and remove agents from the UI
// instead of via code/YAML edits + a custom deploy. A "capability" is one
// fleet-config row (see infra/dashboard/config_store.py).
//
// Two kinds (spec §3.1):
//  - BUILT-IN (system) agents — fixed config, enable/disable-only, undeletable.
//    Their prompt/deps/grants are code-defined; shown read-only. Clone to start
//    a custom agent from one.
//  - CUSTOM (authored) agents — a config row: system prompt + requirements +
//    per-tool grants + skills, built on the generic base image. Fully editable,
//    deletable (destroys its resources), and — when the approval gate is on —
//    subject to a second-admin Approve before the build starts.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { fmtTime } from "./format";
import { usePolling } from "./hooks";
import type { CapabilityConfig, SkillRef, ToolCatalogEntry } from "./types";

// Mirrors config_store._AGENT_ID_RE / admin._validate_capability_body so the
// client rejects the same ids the server would, with an inline reason.
const AGENT_ID_RE = /^[a-z][a-z0-9-]{0,62}[a-z0-9]$/;

// Which lifecycle states read as healthy vs in-flight vs broken, for the pill.
// Class names match styles.css (.pill.ok / .pill.err / .pill.unknown).
function statusClass(status?: string): string {
  if (status === "active") return "ok";
  if (status === "failed") return "err";
  return "unknown"; // pending | building | disabled | deleting
}

export function CapabilitiesPanel({
  api,
  onAuthError,
}: {
  api: DashboardApi;
  onAuthError: () => void;
}) {
  const handleError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );

  // Capabilities change on admin action; a "building"/"deleting" one transitions
  // on its own as the pipeline/runtime/teardown progresses, so poll on the active
  // beat while any row is mid-lifecycle, otherwise only on the idle beat.
  const poll = usePolling<{ capabilities: CapabilityConfig[] }>(
    () => api.listCapabilities(),
    {
      isActive: (data) =>
        (data?.capabilities ?? []).some((c) =>
          ["building", "pending", "deleting"].includes(c.status ?? ""),
        ),
      deps: [api],
      onError: handleError,
    },
  );

  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The editor modal: null = closed; {source} = create-from-clone / new;
  // {edit} = edit an existing custom agent.
  const [editor, setEditor] = useState<
    | { mode: "new" }
    | { mode: "clone"; source: CapabilityConfig }
    | { mode: "edit"; cap: CapabilityConfig }
    | null
  >(null);

  const run = useCallback(
    async (label: string, fn: () => Promise<string>) => {
      setBusy(true);
      setErr(null);
      setMsg(null);
      try {
        setMsg(await fn());
        poll.refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          onAuthError();
          return;
        }
        const status = e instanceof ApiError ? ` (HTTP ${e.status})` : "";
        setErr(`${label} failed${status}: ${(e as Error).message}`);
      } finally {
        setBusy(false);
      }
    },
    [poll, onAuthError],
  );

  const setEnabled = (agentId: string, enabled: boolean) =>
    run(enabled ? `Enable ${agentId}` : `Disable ${agentId}`, async () => {
      await api.onboardCapability({ agent_id: agentId, enabled });
      return enabled
        ? `Enabling ${agentId} — building the container and standing up its runtime.`
        : `Disabled ${agentId} — de-routed (its runtime is left running, not torn down).`;
    });

  const remove = (agentId: string) =>
    run(`Delete ${agentId}`, async () => {
      await api.deleteCapability(agentId);
      return `Deleting ${agentId} — de-routed; tearing down its runtime, role, image, and scoped skills.`;
    });

  const approve = (agentId: string) =>
    run(`Approve ${agentId}`, async () => {
      await api.approveCapability(agentId);
      return `Approved ${agentId} — build started.`;
    });

  const caps = poll.data?.capabilities ?? [];

  return (
    <div>
      <div className="section-head">
        <div>
          <h2>Capabilities (agents)</h2>
          <p className="muted">
            Author a custom agent — a system prompt, pip requirements, per-tool
            grants, and skills — built on the shared base image; or enable a
            built-in system agent. A capability becomes routable once it reaches{" "}
            <code>active</code>. Containers are rebuilt weekly for security patches.
          </p>
        </div>
        <button className="primary" disabled={busy} onClick={() => setEditor({ mode: "new" })}>
          Author custom agent
        </button>
      </div>

      {err && <div className="banner error">{err}</div>}
      {msg && <div className="banner ok">{msg}</div>}
      {poll.error && (
        <div className="banner error">Failed to load capabilities: {poll.error}</div>
      )}

      {editor && (
        <CapabilityEditorModal
          api={api}
          editor={editor}
          existingIds={caps.map((c) => c.agent_id)}
          onClose={() => setEditor(null)}
          onSuccess={(agentId, verb) => {
            setEditor(null);
            setErr(null);
            setMsg(`${verb} ${agentId}.`);
            poll.refresh();
          }}
          onAuthError={onAuthError}
        />
      )}

      <table>
        <thead>
          <tr>
            <th>Agent</th>
            <th>Type</th>
            <th>Description</th>
            <th>Aliases</th>
            <th>Dispatchable</th>
            <th>Status</th>
            <th>Review</th>
            <th>Onboarded by</th>
            <th>Updated</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {caps.map((c) => (
            <tr key={c.agent_id}>
              <td>
                <code>{c.agent_id}</code>
              </td>
              <td>{c.builtin ? "built-in" : "custom"}</td>
              <td>{c.description || "—"}</td>
              <td>{(c.aliases ?? []).join(", ") || "—"}</td>
              <td>{c.enabled ? "yes" : "no"}</td>
              <td>
                <span className={`pill ${statusClass(c.status)}`} title={c.status_detail || ""}>
                  {c.status ?? "unknown"}
                </span>
              </td>
              <td>
                {c.review_status === "pending_review" ? (
                  <span className="pill warn">pending review</span>
                ) : (
                  "—"
                )}
              </td>
              <td>{c.onboarded_by || "—"}</td>
              <td>{fmtTime(c.updated_at ?? c.onboarded_at)}</td>
              <td className="row-actions">
                {/* Approve is visible on a pending_review agent (a different admin
                    than the author approves — enforced server-side). */}
                {c.review_status === "pending_review" && (
                  <button disabled={busy} onClick={() => void approve(c.agent_id)}>
                    Approve
                  </button>
                )}
                {c.enabled ? (
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Disable ${c.agent_id}? It stops being dispatchable (its runtime is left running, not torn down).`,
                        )
                      ) {
                        void setEnabled(c.agent_id, false);
                      }
                    }}
                  >
                    Disable
                  </button>
                ) : (
                  <button disabled={busy} onClick={() => void setEnabled(c.agent_id, true)}>
                    Enable
                  </button>
                )}
                {/* Custom agents are editable; built-ins are fixed (view via Clone). */}
                {!c.builtin && (
                  <button disabled={busy} onClick={() => setEditor({ mode: "edit", cap: c })}>
                    Edit
                  </button>
                )}
                {/* Clone any agent (incl. built-ins) into a new custom agent. */}
                <button
                  disabled={busy}
                  onClick={() => setEditor({ mode: "clone", source: c })}
                >
                  Clone
                </button>
                {/* Built-in (system) agents are undeletable — enable/disable only. */}
                {!c.builtin && (
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Delete ${c.agent_id}? This DESTROYS the custom agent — its runtime, role, image, and capability-scoped skills.`,
                        )
                      ) {
                        void remove(c.agent_id);
                      }
                    }}
                  >
                    Delete
                  </button>
                )}
              </td>
            </tr>
          ))}
          {caps.length === 0 && !poll.loading && (
            <tr>
              <td colSpan={10} className="muted">
                No capabilities onboarded yet. Use “Author custom agent” to add one.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// The authoring editor. Handles new / clone-from / edit of a CUSTOM agent. A
// built-in is never edited here — the Clone action pre-fills this form from a
// built-in's config into a new editable custom agent (spec §8.2/§8.4).
function CapabilityEditorModal({
  api,
  editor,
  existingIds,
  onSuccess,
  onClose,
  onAuthError,
}: {
  api: DashboardApi;
  editor:
    | { mode: "new" }
    | { mode: "clone"; source: CapabilityConfig }
    | { mode: "edit"; cap: CapabilityConfig };
  existingIds: string[];
  onSuccess: (agentId: string, verb: string) => void;
  onClose: () => void;
  onAuthError: () => void;
}) {
  // Seed from the source (clone) or the edited row; a "new" agent starts blank.
  const seed: Partial<CapabilityConfig> =
    editor.mode === "edit" ? editor.cap : editor.mode === "clone" ? editor.source : {};
  const isEdit = editor.mode === "edit";

  const [agentId, setAgentId] = useState(isEdit ? editor.cap.agent_id : "");
  const [description, setDescription] = useState(seed.description ?? "");
  const [aliases, setAliases] = useState((seed.aliases ?? []).join(", "));
  const [systemPrompt, setSystemPrompt] = useState(seed.system_prompt ?? "");
  const [requirements, setRequirements] = useState((seed.requirements ?? []).join("\n"));
  const [env, setEnv] = useState(
    Object.entries(seed.env ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join(", "),
  );
  const [grants, setGrants] = useState<Set<string>>(new Set(seed.tool_grants ?? []));
  const [skills, setSkills] = useState<SkillRef[]>(seed.skills ?? []);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Tool catalog + skill library are loaded once when the modal opens.
  const [catalog, setCatalog] = useState<ToolCatalogEntry[] | null>(null);
  const [library, setLibrary] = useState<SkillRef[] | null>(null);
  useEffect(() => {
    inputRef.current?.focus();
    let alive = true;
    void (async () => {
      try {
        const [tc, sk] = await Promise.all([api.toolCatalog(), api.listSkills()]);
        if (alive) {
          setCatalog(tc.tools);
          setLibrary(sk.skills);
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) onAuthError();
        // A catalog/library load failure isn't fatal to editing prompt/deps —
        // just leave those pickers empty with a note.
        if (alive) {
          setCatalog((c) => c ?? []);
          setLibrary((l) => l ?? []);
        }
      }
    })();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      alive = false;
      window.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Group catalog tools by connector (target) for display; the grant unit is the
  // individual tool (§3.5).
  const grouped = useMemo(() => {
    const g: Record<string, ToolCatalogEntry[]> = {};
    for (const t of catalog ?? []) (g[t.target] ??= []).push(t);
    return g;
  }, [catalog]);

  const parseEnv = (): Record<string, string> | null => {
    const out: Record<string, string> = {};
    for (const pair of env.split(",").map((s) => s.trim()).filter(Boolean)) {
      const eq = pair.indexOf("=");
      if (eq <= 0) {
        setHint(`Env entry "${pair}" must be KEY=value.`);
        return null;
      }
      const key = pair.slice(0, eq).trim();
      if (!/^[A-Z][A-Z0-9_]*$/.test(key)) {
        setHint(`Env key "${key}" must be UPPER_SNAKE_CASE.`);
        return null;
      }
      out[key] = pair.slice(eq + 1).trim();
    }
    return out;
  };

  const toggleGrant = (actionId: string) => {
    setGrants((prev) => {
      const next = new Set(prev);
      if (next.has(actionId)) next.delete(actionId);
      else next.add(actionId);
      return next;
    });
  };

  const toggleSkill = (ref: SkillRef) => {
    setSkills((prev) =>
      prev.some((s) => s.s3_prefix === ref.s3_prefix)
        ? prev.filter((s) => s.s3_prefix !== ref.s3_prefix)
        : [...prev, ref],
    );
  };

  const submit = async () => {
    const id = agentId.trim();
    if (!AGENT_ID_RE.test(id)) {
      setHint(
        `"${id}" isn't a valid agent id — lowercase, start with a letter, end alphanumeric, [a-z0-9-], 2-64 chars.`,
      );
      return;
    }
    if (!isEdit && existingIds.includes(id)) {
      setHint(`An agent named "${id}" already exists.`);
      return;
    }
    const envObj = parseEnv();
    if (envObj === null) return;
    setHint(null);
    setBusy(true);
    try {
      await api.onboardCapability({
        agent_id: id,
        description: description.trim() || undefined,
        aliases: aliases.split(",").map((a) => a.trim()).filter(Boolean),
        system_prompt: systemPrompt,
        requirements: requirements.split("\n").map((r) => r.trim()).filter(Boolean),
        tool_grants: [...grants],
        skills,
        env: envObj,
        enabled: isEdit ? (editor.cap.enabled ?? false) : false,
      });
      onSuccess(id, isEdit ? "Saved" : "Authored");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        onAuthError();
        return;
      }
      setHint((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const title = isEdit
    ? `Edit ${editor.cap.agent_id}`
    : editor.mode === "clone"
      ? `Clone ${editor.source.agent_id} → new custom agent`
      : "Author custom agent";

  return (
    <div
      className="modal-overlay"
      onClick={() => {
        if (!busy) onClose();
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cap-editor-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="cap-editor-title">{title}</h3>
        <p className="muted">
          A custom agent runs on the shared base image — no code checkin. New
          dependencies or skills may require a second admin’s approval before the
          build starts.
        </p>

        <label className="field">
          <span>Agent id</span>
          <input
            ref={inputRef}
            placeholder="e.g. triage"
            value={agentId}
            disabled={busy || isEdit}
            onChange={(e) => {
              setAgentId(e.target.value);
              if (hint) setHint(null);
            }}
            aria-label="Agent id"
          />
        </label>

        <label className="field">
          <span>Description</span>
          <input
            placeholder="What this agent does"
            value={description}
            disabled={busy}
            onChange={(e) => setDescription(e.target.value)}
            aria-label="Description"
          />
        </label>

        <label className="field">
          <span>Aliases (comma-separated)</span>
          <input
            placeholder="e.g. tri, triage-bot"
            value={aliases}
            disabled={busy}
            onChange={(e) => setAliases(e.target.value)}
            aria-label="Aliases"
          />
        </label>

        <label className="field">
          <span>System prompt</span>
          <textarea
            placeholder="The agent's context and instructions."
            value={systemPrompt}
            disabled={busy}
            onChange={(e) => setSystemPrompt(e.target.value)}
            aria-label="System prompt"
          />
        </label>

        <label className="field">
          <span>Requirements (one pip specifier per line)</span>
          <textarea
            placeholder={"tavily-python>=0.5\nhttpx"}
            value={requirements}
            disabled={busy}
            onChange={(e) => setRequirements(e.target.value)}
            aria-label="Requirements"
          />
          <span className="muted" style={{ textTransform: "none" }}>
            Plain PyPI specifiers only — no flags, URLs, VCS refs, or paths.
          </span>
        </label>

        <div className="field">
          <span>Tool grants (by connector)</span>
          {catalog === null ? (
            <span className="muted">Loading tool catalog…</span>
          ) : catalog.length === 0 ? (
            <span className="muted">No grantable tools available.</span>
          ) : (
            <div className="tool-grid">
              {Object.entries(grouped).map(([target, tools]) => (
                <div className="connector-group" key={target}>
                  <div className="group-label">{target}</div>
                  {tools.map((t) => (
                    <label key={t.action_id}>
                      <input
                        type="checkbox"
                        disabled={busy}
                        checked={grants.has(t.action_id)}
                        onChange={() => toggleGrant(t.action_id)}
                      />
                      <code>{t.tool}</code>
                      <span className={`klass-tag ${t.klass}`}>{t.klass}</span>
                    </label>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="field">
          <span>Skills</span>
          {library === null ? (
            <span className="muted">Loading skills…</span>
          ) : library.length === 0 ? (
            <span className="muted">
              No skills uploaded yet — manage them in the Skills library.
            </span>
          ) : (
            <div className="tool-grid">
              {library.map((s) => (
                <label key={s.s3_prefix}>
                  <input
                    type="checkbox"
                    disabled={busy}
                    checked={skills.some((x) => x.s3_prefix === s.s3_prefix)}
                    onChange={() => toggleSkill(s)}
                  />
                  <code>{s.name}</code>
                  <span className="klass-tag">{s.scope ?? "shared"}</span>
                </label>
              ))}
            </div>
          )}
        </div>

        <label className="field">
          <span>Runtime env (KEY=value, comma-separated)</span>
          <input
            placeholder="e.g. ASANA_PROJECT_GID=123, ASANA_WORKSPACE_GID=456"
            value={env}
            disabled={busy}
            onChange={(e) => setEnv(e.target.value)}
            aria-label="Runtime env"
          />
        </label>

        {hint && (
          <div className="banner error" role="alert">
            {hint}
          </div>
        )}

        <div className="modal-actions">
          <button disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={busy} onClick={() => void submit()}>
            {busy ? "Saving…" : isEdit ? "Save" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
