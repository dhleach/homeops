import { useState, useCallback } from "react";

/**
 * Hook that POSTs to /api/diagnostic and manages loading/error/answer state.
 *
 * @param {string} apiUrl  Base URL for the API (e.g. "https://api.homeops.now")
 * @returns {{ ask: (question: string) => void, answer: string|null, loading: boolean, error: string|null, reset: () => void }}
 */
export function useAsk(apiUrl) {
  const [answer, setAnswer] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const ask = useCallback(
    async (question) => {
      setLoading(true);
      setAnswer(null);
      setError(null);

      try {
        const res = await fetch(`${apiUrl}/api/diagnostic`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question }),
        });
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
    [apiUrl],
  );

  const reset = useCallback(() => {
    setAnswer(null);
    setError(null);
  }, []);

  return { ask, answer, loading, error, reset };
}
