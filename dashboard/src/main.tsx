import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { AuthProvider } from "react-oidc-context";
import { App } from "./App";
import { buildOidcConfig } from "./auth";
import { loadConfig, type AppConfig } from "./config";
import "./styles.css";

/**
 * Bootstrap: runtime config must load before the OIDC provider can be built
 * (it needs the authority/client id), so we resolve it first and show a
 * loading/error state until it's ready. A config failure is fatal and shown
 * plainly rather than half-initializing auth.
 */
function Root() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadConfig().then(setConfig).catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="center">
        <div className="banner error">Configuration error: {error}</div>
      </div>
    );
  }
  if (!config) {
    return <div className="center">Loading…</div>;
  }

  return (
    <AuthProvider {...buildOidcConfig(config)}>
      <App config={config} />
    </AuthProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
