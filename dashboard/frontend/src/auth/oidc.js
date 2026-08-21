/**
 * Revision history:
 *   2026-08-21  Added the Cognito-compatible public OIDC client settings so the
 *               browser uses authorization code + PKCE without a client secret.
 */

import { UserManager, WebStorageStateStore } from "oidc-client-ts";

const authority = (import.meta.env.VITE_OIDC_AUTHORITY ?? "").trim();
const clientId = (import.meta.env.VITE_OIDC_CLIENT_ID ?? "").trim();
const scope =
  (import.meta.env.VITE_OIDC_SCOPE ?? "").trim() ||
  "openid email profile https://api.homeops.now/diagnostic:read";

export const OIDC_CONFIGURED = Boolean(authority && clientId);

export const oidcSettings = OIDC_CONFIGURED
  ? {
      authority,
      client_id: clientId,
      redirect_uri: `${window.location.origin}/auth/callback`,
      post_logout_redirect_uri: window.location.origin,
      response_type: "code",
      scope,
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      automaticSilentRenew: false,
    }
  : null;

export const userManager = oidcSettings ? new UserManager(oidcSettings) : null;
