// Permission-groups panel (spec §17): create/list/delete named groups. A group
// is the RECOMMENDED access mechanism — members inherit its group-scoped trigger
// rules across every source. Membership is edited on the Users panel (assigned at
// onboarding approval); group ACCESS is authored as group-scoped trigger rules.

import { useCallback, useState } from "react";
import { ApiError, type DashboardApi } from "../api";
import { usePolling } from "../hooks";
import type { PermGroup } from "../types";

export function GroupsPanel({
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
  const poll = usePolling<{ groups: PermGroup[] }>(() => api.listGroups(), {
    isActive: () => false,
    deps: [api],
    onError: handleErr,
  });

  const [groupId, setGroupId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [recommended, setRecommended] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const create = async () => {
    if (!groupId.trim()) {
      setMsg("Group id is required (lowercase, letter-first, [a-z0-9-]).");
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.createGroup({
        group_id: groupId.trim(),
        name: name.trim() || groupId.trim(),
        description: description.trim(),
        recommended,
      });
      setGroupId("");
      setName("");
      setDescription("");
      setRecommended(false);
      poll.refresh();
    } catch (e) {
      handleErr(e);
      setMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.deleteGroup(id);
      poll.refresh();
    } catch (e) {
      handleErr(e);
      setMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const groups = poll.data?.groups ?? [];

  return (
    <div className="groups-panel">
      <h3>Permission groups</h3>
      {msg && <p className="msg">{msg}</p>}
      <div className="create-form">
        <input
          placeholder="group id (e.g. edtech-eng)"
          value={groupId}
          onChange={(e) => setGroupId(e.target.value)}
        />
        <input placeholder="display name" value={name} onChange={(e) => setName(e.target.value)} />
        <input
          placeholder="description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <label>
          <input
            type="checkbox"
            checked={recommended}
            onChange={(e) => setRecommended(e.target.checked)}
          />
          Recommended
        </label>
        <button disabled={busy} onClick={create}>
          Create group
        </button>
      </div>

      {groups.length === 0 ? (
        <p className="empty">No permission groups yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Group</th>
              <th>Description</th>
              <th>Members</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.group_id}>
                <td>
                  {g.name}
                  {g.recommended ? " ★" : ""} <code>{g.group_id}</code>
                </td>
                <td>{g.description || "—"}</td>
                <td>{g.member_count ?? 0}</td>
                <td>
                  <button disabled={busy} onClick={() => remove(g.group_id)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        Group access is authored as group-scoped trigger rules (subject type
        “group”) on each connector’s Trigger Rules panel. Membership is assigned when
        approving a user on the Users panel.
      </p>
    </div>
  );
}
