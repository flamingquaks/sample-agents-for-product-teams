// Shared chrome for a connector sub-page: a back link, the connector title, and
// a tab strip. Each page renders its own tab content via the render-prop.

import { useState } from "react";

export interface Tab {
  key: string;
  label: string;
  render: () => React.ReactNode;
}

export function ConnectorLayout({
  label,
  tabs,
  onBack,
}: {
  label: string;
  tabs: Tab[];
  onBack: () => void;
}) {
  const [active, setActive] = useState(tabs[0]?.key);
  const current = tabs.find((t) => t.key === active) ?? tabs[0];
  return (
    <div>
      <div className="section-head">
        <div>
          <button className="button-link" onClick={onBack}>
            ← Connectors
          </button>
          <h2>{label}</h2>
        </div>
      </div>
      <div className="tab-strip" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={t.key === current?.key}
            className={t.key === current?.key ? "tab active" : "tab"}
            onClick={() => setActive(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="tab-panel" role="tabpanel">
        {current?.render()}
      </div>
    </div>
  );
}
