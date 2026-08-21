/**
 * Revision history:
 *   2026-08-21  Added browser OIDC session hydration, PKCE redirects, and
 *               safe session clearing for expired diagnostic access tokens.
 */

import { useCallback, useEffect, useState } from "react";
import { OIDC_CONFIGURED, userManager } from "../auth/oidc.js";

const SIGN_IN_ERROR = "Sign-in could not be completed. Please try again.";

/**
 * Owns the browser OIDC session used by Ask HomeOps.
 *
 * The public client uses authorization code + PKCE through the configured
 * provider. Access tokens remain in session storage managed by oidc-client-ts;
 * no identity secret is embedded in the frontend build.
 */
export function useAuth() {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;

    if (!OIDC_CONFIGURED || !userManager) {
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }

    const hydrate = async () => {
      try {
        const isCallback = window.location.pathname === "/auth/callback";
        const currentUser = isCallback
          ? await userManager.signinRedirectCallback()
          : await userManager.getUser();

        if (isCallback) {
          window.history.replaceState({}, document.title, "/");
        }

        if (!cancelled) {
          setUser(currentUser && !currentUser.expired ? currentUser : null);
          setError(null);
        }
      } catch {
        if (!cancelled) {
          setUser(null);
          setError(SIGN_IN_ERROR);
          if (window.location.pathname === "/auth/callback") {
            window.history.replaceState({}, document.title, "/");
          }
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    hydrate();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(() => {
    if (!userManager) return;
    setError(null);
    void userManager.signinRedirect();
  }, []);

  const logout = useCallback(() => {
    if (!userManager) return;
    void userManager.signoutRedirect();
  }, []);

  const handleUnauthorized = useCallback(() => {
    setUser(null);
    void userManager?.removeUser();
  }, []);

  return {
    configured: OIDC_CONFIGURED,
    authenticated: Boolean(user && !user.expired),
    accessToken: user?.access_token ?? null,
    loading,
    error,
    login,
    logout,
    handleUnauthorized,
  };
}
