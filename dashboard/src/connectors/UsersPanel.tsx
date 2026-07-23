// Users panel (spec §16): the cross-source identity directory + the
// user-onboarding approval queue. A first touch from any source creates a
// PENDING identity and files a request here; an admin approves it — which flips
// the identity to ACTIVE and assigns permission groups (the access step, §17.4).

import { useCallback, useState } from "react";
import { ApiError, type DashboardApi } from "../api";
import { usePolling } from "../hooks";
import type { Identity, PermGroup, UserRequest } from "../types";

function handleKeys(id: Identity): string {
  const parts: string[] = [];
  if (id.handles.github) parts.push(`github:${id.handles.github}`);
  if (id.handles.asana) parts.push(`asana:${id.handles.asana}`);
  for (const [team, uid] of Object.entries(id.handles.slack ?? {})) {
    parts.push(`slack:${team}:${uid}`);
  }
  return parts.join(", ") || "—";
}

export function UsersPanel({
  api,
  onAuthError,
}: {
  api: DashboardApi;
  onAuthError: () => void;
}) {
  const handleErr = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onAuthError();
    },
    [onAuthError],
  );

  const requests = usePolling<{ requests: UserRequest[] }>(
    () => api.listUserRequests("pending"),
    { isActive: () => false, deps: [api], onError: handleErr },
  );
  const identities = usePolling<{ identities: Identity[] }>(
    () => api.listIdentities(),
    { isActive: () => false, deps: [api], onError: handleErr },
  );
  const groups = usePolling<{ groups: PermGroup[] }>(() => api.listGroups(), {
    isActive: () => false,
    deps: [api],
    onError: handleErr,
  });

  const [selectedGroups, setSelectedGroups] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const groupOptions = groups.data?.groups ?? [];

  const approve = async (req: UserRequest) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.approveUserRequest(req.request_id, selectedGroups[req.request_id] ?? []);
      requests.refresh();
      identities.refresh();
    } catch (e) {
      handleErr(e);
      setMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const deny = async (req: UserRequest) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.denyUserRequest(req.request_id);
      requests.refresh();
    } catch (e) {
      handleErr(e);
      setMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const pending = requests.data?.requests ?? [];
  const allIdentities = identities.data?.identities ?? [];

  return (
    <div className="users-panel">
      <section>
        <h3>Pending onboarding requests</h3>
        {msg && <p className="msg">{msg}</p>}
        {pending.length === 0 ? (
          <p className="empty">No pending user-onboarding requests.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Email / handle</th>
                <th>Assign groups</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {pending.map((req) => (
                <tr key={req.request_id}>
                  <td>{req.source}</td>
                  <td>
                    {req.display_name || req.proposed_email || req.identity_id}
                    {req.display_name && req.proposed_email && (
                      <div className="muted" style={{ fontSize: 12 }}>{req.proposed_email}</div>
                    )}
                  </td>
                  <td>
                    <select
                      multiple
                      value={selectedGroups[req.request_id] ?? []}
                      onChange={(e) =>
                        setSelectedGroups((prev) => ({
                          ...prev,
                          [req.request_id]: Array.from(
                            e.target.selectedOptions,
                            (o) => o.value,
                          ),
                        }))
                      }
                    >
                      {groupOptions.map((g) => (
                        <option key={g.group_id} value={g.group_id}>
                          {g.name}
                          {g.recommended ? " ★" : ""}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <button disabled={busy} onClick={() => approve(req)}>
                      Approve
                    </button>
                    <button disabled={busy} onClick={() => deny(req)}>
                      Deny
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section>
        <h3>Identities</h3>
        {allIdentities.length === 0 ? (
          <p className="empty">No identities yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Handles</th>
                <th>Groups</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {allIdentities.map((id) => (
                <tr key={id.identity_id}>
                  <td>{id.display_name || "—"}</td>
                  <td>{id.email || "—"}</td>
                  <td className="handles">{handleKeys(id)}</td>
                  <td>{id.groups.join(", ") || "—"}</td>
                  <td>
                    <span className={`status status-${id.status}`}>{id.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
