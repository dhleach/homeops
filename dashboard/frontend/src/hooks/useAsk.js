import { useState, useCallback } from "react";

/**
 * Hook that POSTs to /api/diagnostic and manages loading/error/answer state.
 *
 * Revision history:
 *   2026-08-21  Added OIDC bearer headers and stable handling for auth, quota,
 *               and temporary-service responses.
 *
 * @param {string} apiUrl  Base URL for the API (e.g. "https://api.homeops.now")
 * @param {string|null} accessToken  OIDC access token for the diagnostic API
 * @param {() => void} onUnauthorized  Clears the local session after a 401
 * @returns {{ ask: (question: string) => void, answer: string|null, loading: boolean, error: string|null, reset: () => void }}
 */
export function useAsk(apiUrl, accessToken, onUnauthorized) {
  const [answer, setAnswer] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const ask = useCallback(
    async (question) => {
      setLoading(true);
      setAnswer(null);
      setError(null);

      try {
        if (!accessToken) {
          throw new Error("Sign in to use Ask HomeOps.");
        }

        const res = await fetch(`${apiUrl}/api/diagnostic`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${accessToken}`,
          },
          body: JSON.stringify({ question }),
        });
        if (res.status === 401) {
          onUnauthorized?.();
          throw new Error("Your sign-in expired. Sign in again to continue.");
        }
        if (res.status === 403) throw new Error("Your account is not allowed to use Ask HomeOps.");
        if (res.status === 429) throw new Error("Ask HomeOps is temporarily at capacity. Try again shortly.");
        if (res.status === 503) throw new Error("Ask HomeOps is temporarily unavailable. Try again shortly.");
        if (!res.ok) throw new Error(`API returned ${res.status}`);
        const data = await res.json();
        if (data.error) {
          setError(data.error);
        } else {
          setAnswer(data.answer);
        }
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    },
    [accessToken, apiUrl, onUnauthorized],
  );

  const reset = useCallback(() => {
    setAnswer(null);
    setError(null);
  }, []);

  return { ask, answer, loading, error, reset };
}
