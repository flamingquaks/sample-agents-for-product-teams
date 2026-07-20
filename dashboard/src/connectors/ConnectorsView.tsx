// Connectors index (inside the Admin panel): a card per connector. Clicking a
// card opens its dedicated sub-page (#/admin/connectors/<id>).

import { CONNECTORS } from "./registry";

export function ConnectorsView({ onOpen }: { onOpen: (id: string) => void }) {
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
        {CONNECTORS.map((c) => (
          <button key={c.id} className="connector-card" onClick={() => onOpen(c.id)}>
            <h3>{c.label}</h3>
            <p className="muted">{c.blurb}</p>
          </button>
        ))}
      </div>
    </div>
  );
}
