// Connectors index (inside the Admin panel): a card per connector. Clicking a
// card opens its dedicated sub-page (#/admin/connectors/<id>). Each card shows a
// health badge driven by the descriptor's useStatus (spec §9.2/§9.3).

import { useEffect, useState } from "react";
import type { DashboardApi } from "../api";
import { CONNECTORS, type ConnectorStatus } from "./registry";

export function ConnectorsView({
  api,
  onOpen,
}: {
  api: DashboardApi;
  onOpen: (id: string) => void;
}) {
  const [status, setStatus] = useState<Record<string, ConnectorStatus>>({});

  useEffect(() => {
    let live = true;
    for (const c of CONNECTORS) {
      if (!c.useStatus) continue;
      c.useStatus(api)
        .then((s) => live && setStatus((prev) => ({ ...prev, [c.id]: s })))
        .catch(() => live && setStatus((prev) => ({ ...prev, [c.id]: { health: "unknown", label: "status unavailable" } })));
    }
    return () => {
      live = false;
    };
  }, [api]);

  return (
    <div>
      <div className="section-head">
        <div>
          <h2>Connectors</h2>
          <p className="muted">
            Manage the event sources that trigger fleet agents. Each connector owns its own
            connection, triggers, and access rules.
          </p>
        </div>
      </div>
      <div className="card-grid">
        {CONNECTORS.map((c) => {
          const s = status[c.id];
          return (
            <button key={c.id} className="connector-card" onClick={() => onOpen(c.id)}>
              <h3>
                {c.icon ? <span aria-hidden>{c.icon} </span> : null}
                {c.label}
              </h3>
              {c.useStatus && (
                <span className={`pill ${s ? s.health : "unknown"}`}>
                  {s ? s.label : "checking…"}
                </span>
              )}
              <p className="muted">{c.blurb}</p>
            </button>
          );
        })}
      </div>
    </div>
  );
}
