// Runtime configuration.
//
// A deployed build reads /config.json (written at deploy time from the
// foundation stack's outputs — see Phase 5), so a single static bundle works
// against any stack without a rebuild. For local `npm run dev` we fall back to
// Vite env vars (.env.local). This module resolves the two sources into one
// AppConfig the rest of the app consumes.

export interface AppConfig {
  /** Base URL of the dashboard query API, e.g. https://abc.execute-api…/dev */
  apiBaseUrl: string;
  /** Cognito OIDC issuer: https://cognito-idp.<region>.amazonaws.com/<poolId> */
  cognitoAuthority: string;
  /** Cognito app client id (public SPA client, PKCE). */
  cognitoClientId: string;
  /** Cognito Hosted-UI domain (used for the RP-initiated logout redirect). */
  cognitoLoginDomain: string;
  /** OAuth redirect URI registered on the app client. */
  redirectUri: string;
}

interface RawConfig {
  apiBaseUrl?: string;
  cognitoAuthority?: string;
  cognitoClientId?: string;
  cognitoLoginDomain?: string;
  redirectUri?: string;
}

function fromEnv(): RawConfig {
  const env = import.meta.env;
  return {
    apiBaseUrl: env.VITE_API_BASE_URL,
    cognitoAuthority: env.VITE_COGNITO_AUTHORITY,
    cognitoClientId: env.VITE_COGNITO_CLIENT_ID,
    cognitoLoginDomain: env.VITE_COGNITO_LOGIN_DOMAIN,
    // Default the redirect to where the app is actually served — origin + the
    // Vite base path — so it works both at the CloudFront root and under a
    // subpath. This is the URL that must be registered as a Cognito CallbackURL.
    redirectUri: env.VITE_REDIRECT_URI || appBaseUrl(),
  };
}

/** Absolute URL of the app's own base path (honors a non-root deploy). */
function appBaseUrl(): string {
  // import.meta.env.BASE_URL is Vite's configured base ("/" at root, or e.g.
  // "/dashboard/" under a subpath). Resolve it against the current origin.
  return new URL(import.meta.env.BASE_URL ?? "/", window.location.origin).href;
}

function trimTrailingSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

/**
 * Load and validate runtime config. Tries config.json first (deployed), then
 * merges in env fallbacks for any missing field (local dev). Throws with a
 * clear message if a required field is still missing — a misconfigured deploy
 * should fail loudly at startup, not silently point auth/API at nothing.
 */
export async function loadConfig(): Promise<AppConfig> {
  let file: RawConfig = {};
  try {
    // Fetch relative to the app's base path (not the origin root), so config.json
    // is found whether the SPA is served at "/" or under a subpath — matching
    // Vite's base:"./" and the appBaseUrl() redirect above.
    const configUrl = new URL("config.json", appBaseUrl()).href;
    const resp = await fetch(configUrl, { cache: "no-store" });
    if (resp.ok) file = (await resp.json()) as RawConfig;
  } catch {
    // No config.json (local dev) — fall back to env entirely.
  }

  const env = fromEnv();
  const merged: RawConfig = {
    apiBaseUrl: file.apiBaseUrl || env.apiBaseUrl,
    cognitoAuthority: file.cognitoAuthority || env.cognitoAuthority,
    cognitoClientId: file.cognitoClientId || env.cognitoClientId,
    cognitoLoginDomain: file.cognitoLoginDomain || env.cognitoLoginDomain,
    redirectUri: file.redirectUri || env.redirectUri,
  };

  const missing = (
    ["apiBaseUrl", "cognitoAuthority", "cognitoClientId", "cognitoLoginDomain", "redirectUri"] as const
  ).filter((k) => !merged[k]);
  if (missing.length > 0) {
    throw new Error(
      `Dashboard is missing required config: ${missing.join(", ")}. ` +
        `Provide a /config.json or a .env.local (see .env.example).`,
    );
  }

  return {
    apiBaseUrl: trimTrailingSlash(merged.apiBaseUrl!),
    cognitoAuthority: merged.cognitoAuthority!,
    cognitoClientId: merged.cognitoClientId!,
    cognitoLoginDomain: trimTrailingSlash(merged.cognitoLoginDomain!),
    redirectUri: merged.redirectUri!,
  };
}
