// Cognito Hosted-UI auth via oidc-client-ts / react-oidc-context.
//
// Authorization Code + PKCE against the Cognito user pool's OIDC issuer. We
// request the openid/email/profile scopes; the access token carries the
// `cognito:groups` claim the API's operator check reads. Tokens live in
// sessionStorage (cleared when the tab closes) rather than localStorage, to
// shrink the window a stolen token is usable.

import { WebStorageStateStore } from "oidc-client-ts";
import type { AuthProviderProps } from "react-oidc-context";
import type { AppConfig } from "./config";

export function buildOidcConfig(config: AppConfig): AuthProviderProps {
  return {
    authority: config.cognitoAuthority,
    client_id: config.cognitoClientId,
    redirect_uri: config.redirectUri,
    response_type: "code",
    scope: "openid email profile",
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    // Strip the ?code=&state= params from the URL after the redirect callback
    // so a refresh doesn't try to redeem an already-used code.
    onSigninCallback: () => {
      window.history.replaceState({}, document.title, window.location.pathname);
    },
    // Cognito's RP-initiated logout lives on the Hosted-UI domain, not the OIDC
    // issuer, so oidc-client-ts can't discover it from metadata. Point the
    // logout there explicitly. (Wired up in the sign-out handler.)
    metadataSeed: {
      end_session_endpoint: `${config.cognitoLoginDomain}/logout`,
    },
  };
}

/**
 * Build the Cognito Hosted-UI logout URL. Cognito's /logout requires
 * client_id + logout_uri (a registered LogoutURL) rather than the standard
 * OIDC id_token_hint, so we construct it directly.
 */
export function cognitoLogoutUrl(config: AppConfig): string {
  const params = new URLSearchParams({
    client_id: config.cognitoClientId,
    logout_uri: config.redirectUri,
  });
  return `${config.cognitoLoginDomain}/logout?${params.toString()}`;
}
