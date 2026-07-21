// Capabilities panel (admins only): onboard, view, and remove agents from the
// UI instead of via code/YAML edits + a custom deploy. A "capability" is one
// fleet-config row (see infra/dashboard/config_store.py). Onboarding one writes
// the declarative config here; the build → runtime lifecycle (later phases)
// brings it to "active", at which point it enters the Dispatch Router registry.
//
// Mirrors the repo-onboarding UX in AdminView: a focused modal for the create
// flow, a status pill per row, and a confirm-guarded remove. Kept a separate
// component so AdminView stays legible.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { fmtTime } from "./format";
import { usePolling } from "./hooks";
import type { CapabilityConfig } from "./types";

// Mirrors config_store._AGENT_ID_RE / admin._validate_capability_body so the
// client rejects the same ids the server would, with an inline reason.
const AGENT_ID_RE = /^[a-z][a-z0-9-]{0,62}[a-z0-9]$/;

// Which lifecycle states read as healthy vs in-flight vs broken, for the pill.
// Class names match styles.css (.pill.ok / .pill.err / .pill.unknown).
function statusClass(status?: string): string {
  if (status === "active") return "ok";
  if (status === "failed") return "err";
  return "unknown"; // pending | building | disabled
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

  // Capabilities change on admin action; a "building" one transitions on its own
  // as the pipeline/runtime progresses, so poll on the active beat while any row
  // is mid-lifecycle, otherwise only on the idle beat.
  const poll = usePolling<{ capabilities: CapabilityConfig[] }>(
    () => api.listCapabilities(),
    {
      isActive: (data) =>
        (data?.capabilities ?? []).some(
          (c) => c.status === "building" || c.status === "pending",
        ),
      deps: [api],
      onError: handleError,
    },
  );

  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [onboardOpen, setOnboardOpen] = useState(false);

  const remove = useCallback(
    async (agentId: string) => {
      setBusy(true);
      setErr(null);
      setMsg(null);
      try {
        await api.deleteCapability(agentId);
        setMsg(`Removed ${agentId}.`);
        poll.refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          onAuthError();
          return;
        }
        const status = e instanceof ApiError ? ` (HTTP ${e.status})` : "";
        setErr(`Remove ${agentId} failed${status}: ${(e as Error).message}`);
      } finally {
        setBusy(false);
      }
    },
    [api, poll, onAuthError],
  );

  // Enable/disable toggle. Enabling (re)builds + deploys; disabling de-routes the
  // agent WITHOUT tearing its runtime down (spec §9). For a built-in this is the
  // ONLY lever — its config is fixed server-side, so we send just the flag.
  const setEnabled = useCallback(
    async (agentId: string, enabled: boolean) => {
      setBusy(true);
      setErr(null);
      setMsg(null);
      try {
        await api.onboardCapability({ agent_id: agentId, enabled });
        setMsg(
          enabled
            ? `Enabling ${agentId} — building the container and standing up its runtime.`
            : `Disabled ${agentId} — de-routed (its runtime is left running, not torn down).`,
        );
        poll.refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          onAuthError();
          return;
        }
        const status = e instanceof ApiError ? ` (HTTP ${e.status})` : "";
        setErr(`${enabled ? "Enable" : "Disable"} ${agentId} failed${status}: ${(e as Error).message}`);
      } finally {
        setBusy(false);
      }
    },
    [api, poll, onAuthError],
  );

  const caps = poll.data?.capabilities ?? [];

  return (
    <div>
      <div className="section-head">
        <div>
          <h2>Capabilities (agents)</h2>
          <p className="muted">
            Onboard an agent to make the fleet dispatch to it. Onboarding builds the
            agent’s container from the shared pipeline and stands up its runtime; it
            becomes routable once it reaches <code>active</code>. Containers are
            rebuilt weekly to pick up security patches.
          </p>
        </div>
        <button className="primary" disabled={busy} onClick={() => setOnboardOpen(true)}>
          Onboard capability
        </button>
      </div>

      {err && <div className="banner error">{err}</div>}
      {msg && <div className="banner ok">{msg}</div>}

      {poll.error && (
        <div className="banner error">Failed to load capabilities: {poll.error}</div>
      )}

      {onboardOpen && (
        <OnboardCapabilityModal
          api={api}
          onClose={() => setOnboardOpen(false)}
          onSuccess={(agentId) => {
            setOnboardOpen(false);
            setErr(null);
            setMsg(`Onboarded ${agentId} — building the container and standing up its runtime.`);
            poll.refresh();
          }}
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
              <td>{c.onboarded_by || "—"}</td>
              <td>{fmtTime(c.updated_at ?? c.onboarded_at)}</td>
              <td className="row-actions">
                {c.enabled ? (
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm(`Disable ${c.agent_id}? It stops being dispatchable (its runtime is left running, not torn down).`)) {
                        void setEnabled(c.agent_id, false);
                      }
                    }}
                  >
                    Disable
                  </button>
                ) : (
                  <button
                    disabled={busy}
                    onClick={() => void setEnabled(c.agent_id, true)}
                  >
                    Enable
                  </button>
                )}
                {/* Built-in (system) agents are undeletable — enable/disable only. */}
                {!c.builtin && (
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm(`Delete capability ${c.agent_id}? This destroys the custom agent.`)) {
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
              <td colSpan={9} className="muted">
                No capabilities onboarded yet. Use “Onboard capability” to add one.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// The onboard form. agent_id must match a directory under agents/ in the source
// the build pipeline pulls — the field is validated for shape here, but a
// nonexistent agent_id surfaces as a build failure (the capability lands in
// "failed" with the reason), not a client error, since the client can't see the
// source tree. Optional aliases/triggers/env keep the common case one field.
function OnboardCapabilityModal({
  api,
  onSuccess,
  onClose,
}: {
  api: DashboardApi;
  onSuccess: (agentId: string) => void;
  onClose: () => void;
}) {
  const [agentId, setAgentId] = useState("");
  const [description, setDescription] = useState("");
  const [aliases, setAliases] = useState("");
  const [env, setEnv] = useState("");
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  // Parse "KEY=value, KEY2=value2" into an env object; returns null (with a hint
  // set) on a malformed entry so we don't POST a half-parsed env.
  const parseEnv = (): Record<string, string> | null => {
    const out: Record<string, string> = {};
    for (const pair of env.split(",").map((s) => s.trim()).filter(Boolean)) {
      const eq = pair.indexOf("=");
      if (eq <= 0) {
        setHint(`Env entry "${pair}" must be KEY=value.`);
        return null;
      }
      const key = pair.slice(0, eq).trim();
      const val = pair.slice(eq + 1).trim();
      if (!/^[A-Z][A-Z0-9_]*$/.test(key)) {
        setHint(`Env key "${key}" must be UPPER_SNAKE_CASE.`);
        return null;
      }
      out[key] = val;
    }
    return out;
  };

  const submit = async () => {
    const id = agentId.trim();
    if (!AGENT_ID_RE.test(id)) {
      setHint(
        `"${id}" isn't a valid agent id — lowercase, start with a letter, end alphanumeric, [a-z0-9-], 2-64 chars.`,
      );
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
        aliases: aliases
          .split(",")
          .map((a) => a.trim())
          .filter(Boolean),
        env: envObj,
        enabled: true,
      });
      onSuccess(id);
    } catch (e) {
      if (e instanceof ApiError) setHint(e.message);
      else setHint((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

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
        aria-labelledby="onboard-cap-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="onboard-cap-title">Onboard capability</h3>
        <p className="muted">
          The agent id must match a directory under <code>agents/</code> in the fleet
          source. Onboarding triggers the shared build pipeline and stands up the
          runtime; the capability becomes routable once it reaches <code>active</code>.
        </p>

        <label className="field">
          <span>Agent id</span>
          <input
            ref={inputRef}
            placeholder="e.g. triage"
            value={agentId}
            disabled={busy}
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
            {busy ? "Onboarding…" : "Onboard"}
          </button>
        </div>
      </div>
    </div>
  );
}
