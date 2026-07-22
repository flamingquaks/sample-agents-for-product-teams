// Skills library (admins only): upload, list, and delete SKILL.md packages
// (spec §6). Skills are stored in S3 and referenced by capabilities from the
// authoring form. A .zip is validated + expanded by the isolated unpacker
// Lambda server-side (§6.3); a raw .md is stored directly.

import { useCallback, useRef, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import { usePolling } from "./hooks";
import type { SkillRef } from "./types";

// Strip the "data:...;base64," prefix a FileReader dataURL carries — we send
// only the raw base64 payload.
function stripDataUrl(dataUrl: string): string {
  const comma = dataUrl.indexOf(",");
  return comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
}

export function SkillsPanel({
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

  const poll = usePolling<{ skills: SkillRef[] }>(() => api.listSkills(), {
    // Skills don't self-transition (no build/runtime lifecycle), so the idle beat
    // is always sufficient — refresh() drives updates after an upload/delete.
    isActive: () => false,
    deps: [api],
    onError: handleError,
  });

  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [scope, setScope] = useState("shared");
  const fileRef = useRef<HTMLInputElement>(null);

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

  const upload = (file: File) =>
    run(`Upload ${file.name}`, async () => {
      const isZip = file.name.toLowerCase().endsWith(".zip");
      if (isZip) {
        const b64 = await new Promise<string>((resolve, reject) => {
          const r = new FileReader();
          r.onload = () => resolve(stripDataUrl(String(r.result)));
          r.onerror = () => reject(new Error("could not read file"));
          r.readAsDataURL(file);
        });
        const ref = await api.uploadSkillZip(b64, scope);
        return `Uploaded skill "${ref.name}".`;
      }
      const text = await file.text();
      const ref = await api.uploadSkillMd(text, scope);
      return `Uploaded skill "${ref.name}".`;
    });

  const remove = (s: SkillRef) =>
    run(`Delete ${s.name}`, async () => {
      await api.deleteSkill(s.scope ?? "shared", s.name);
      return `Deleted skill "${s.name}".`;
    });

  const skills = poll.data?.skills ?? [];

  return (
    <div>
      <div className="section-head">
        <div>
          <h2>Skills library</h2>
          <p className="muted">
            Upload <code>SKILL.md</code> packages (a raw <code>.md</code> or a{" "}
            <code>.zip</code> with <code>scripts/</code>/<code>references/</code>).
            Custom agents attach them from the authoring form; a skill is
            injected instruction, never a tool grant.
          </p>
        </div>
        <div className="row-actions">
          <label className="field-inline" style={{ margin: 0 }}>
            <select
              value={scope}
              disabled={busy}
              onChange={(e) => setScope(e.target.value)}
              aria-label="Skill scope"
            >
              <option value="shared">shared</option>
              <option value="capability">capability</option>
            </select>
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".md,.zip"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void upload(f);
              e.target.value = ""; // allow re-selecting the same file
            }}
          />
          <button className="primary" disabled={busy} onClick={() => fileRef.current?.click()}>
            Upload skill
          </button>
        </div>
      </div>

      {err && <div className="banner error">{err}</div>}
      {msg && <div className="banner ok">{msg}</div>}
      {poll.error && <div className="banner error">Failed to load skills: {poll.error}</div>}

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Scope</th>
            <th>S3 prefix</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {skills.map((s) => (
            <tr key={s.s3_prefix}>
              <td>
                <code>{s.name}</code>
              </td>
              <td>{s.scope ?? "shared"}</td>
              <td className="muted">{s.s3_prefix}</td>
              <td className="row-actions">
                <button
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm(`Delete skill "${s.name}"? Agents referencing it lose it on their next build.`)) {
                      void remove(s);
                    }
                  }}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {skills.length === 0 && !poll.loading && (
            <tr>
              <td colSpan={4} className="muted">
                No skills uploaded yet. Use “Upload skill” to add one.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
