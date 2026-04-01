"""
Rule: Furnace Session Duration Anomaly

Detects abnormally short or long furnace heating sessions:

- **Short session** (< SHORT_SESSION_THRESHOLD_S, default 90 s):
  Suggests short-cycling or a limit-switch trip. Always a warning.

- **Long session** (> max(p95 × 1.5, per-floor absolute fallback)):
  Suggests overheating risk — especially on floor 2.

The rule is stateless per-call: feed each completed session via
check_session() and act on the returned list of derived event dicts.
"""

from __future__ import annotations

from rules.confidence import compute_confidence, severity_label

SHORT_SESSION_THRESHOLD_S: int = 90  # seconds — below this = short-cycle risk

# Per-floor absolute fallback long-session thresholds (seconds).
# Used when no baseline is available for a floor.
_LONG_SESSION_FALLBACK_S: dict[str, int] = {
    "floor_1": 1800,
    "floor_2": 2700,
    "floor_3": 1200,
}

_DEFAULT_LONG_SESSION_FALLBACK_S: int = 2700  # for unknown / None floors


def _utc_ts() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class FurnaceSessionAnomalyRule:
    """
    Checks a single completed furnace heating session for duration anomalies.

    Args:
        baseline: Dict of per-floor stats as produced by ``compute_baseline()``.
                  Shape: ``{"floor_1": {"p95": 900.0, ...}, ...}``.
                  May be empty or missing floors — absolute thresholds are used as fallback.
    """

    def __init__(self, baseline: dict | None = None) -> None:
        self._baseline: dict = baseline or {}

    def check_session(
        self,
        floor: str | None,
        duration_s: int | None,
        ts_str: str,
    ) -> list[dict]:
        """
        Evaluate one completed heating session and return any warning events.

        Args:
            floor:      Floor identifier ("floor_1", "floor_2", "floor_3") or None.
            duration_s: Session duration in seconds. None → skip (across_restart sessions).
            ts_str:     ISO-8601 timestamp string for the event.

        Returns:
            A list of 0 or 1 derived event dicts.
            Short-session check takes priority — a pathologically short session never
            also fires a long-session warning.
        """
        if duration_s is None:
            return []

        # --- Short session check (absolute threshold, highest priority) ---
        if duration_s < SHORT_SESSION_THRESHOLD_S:
            return [
                {
                    "schema": "homeops.consumer.heating_short_session_warning.v1",
                    "source": "consumer.v1",
                    "ts": _utc_ts(),
                    "data": {
                        "floor": floor,
                        "duration_s": duration_s,
                        "threshold_s": SHORT_SESSION_THRESHOLD_S,
                        "likely_cause": "short_cycle",
                        "session_ts": ts_str,
                        "confidence": 1.0,
                        "severity": "high",
                    },
                }
            ]

        # --- Long session check ---
        floor_baseline = self._baseline.get(floor) if floor else None
        baseline_p95: float | None = float(floor_baseline["p95"]) if floor_baseline else None

        # Absolute fallback threshold for this floor.
        abs_fallback = _LONG_SESSION_FALLBACK_S.get(floor or "", _DEFAULT_LONG_SESSION_FALLBACK_S)

        # Effective threshold: whichever is higher of p95×1.5 and absolute fallback.
        if baseline_p95 is not None:
            threshold_s = max(baseline_p95 * 1.5, abs_fallback)
        else:
            threshold_s = float(abs_fallback)

        if duration_s > threshold_s:
            # Proxy z-score: how far past the threshold (in units of threshold_s).
            z_proxy = (duration_s - threshold_s) / threshold_s
            confidence = compute_confidence(z_proxy)
            sev = severity_label(confidence)

            return [
                {
                    "schema": "homeops.consumer.heating_long_session_warning.v1",
                    "source": "consumer.v1",
                    "ts": _utc_ts(),
                    "data": {
                        "floor": floor,
                        "duration_s": duration_s,
                        "threshold_s": threshold_s,
                        "baseline_p95_s": baseline_p95,
                        "likely_cause": "overheating_risk",
                        "session_ts": ts_str,
                        "confidence": confidence,
                        "severity": sev,
                    },
                }
            ]

        return []
