import { useState, useEffect, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_URL ?? "";
const REFRESH_INTERVAL_MS = 30_000;

/**
 * Fetches and auto-refreshes live temperature data from /api/current-temps.
 *
 * Returns: { data, loading, error, lastUpdated, refresh }
 */
export function useTemps() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/current-temps`);
      if (!res.ok) throw new Error(`API returned ${res.status}`);
      const json = await res.json();
      setData(json);
      setLastUpdated(new Date());
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  return { data, loading, error, lastUpdated, refresh };
}
