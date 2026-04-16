"""
Rule: Heating Efficiency Degradation Over Time

Detects a statistically meaningful upward trend in average heating session
duration for a floor across rolling weekly windows.

A floor that takes progressively longer to heat the same space — without a
corresponding change in setpoint or outdoor temperature — is a potential signal
of insulation degradation, HVAC wear, or duct issues.

Algorithm
---------
1. Bucket ``heating_session_ended.v1`` events into ISO calendar weeks.
2. Compute per-week mean session duration for each floor.
3. Require at least ``min_weeks`` (default 3) non-empty weeks of data.
4. Fit a simple linear regression (ordinary least squares) over the weekly means.
5. If the slope exceeds ``slope_threshold_s_per_week`` (default 60 s/week) and
   the trend is monotonically upward in the last ``min_weeks`` windows, emit a
   finding.

Guards
------
- Requires at least ``min_events_per_week`` (default 3) sessions per week to
  include that week in the regression.
- Floors with fewer than ``min_weeks`` qualifying weeks are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime

_SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"


def _utc_ts() -> str:
    return datetime.now(UTC).isoformat()


def _iso_week_key(dt: datetime) -> str:
    """Return 'YYYY-WNN' for the ISO week containing ``dt``."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _linear_slope(xs: list[float], ys: list[float]) -> float:
    """
    Compute OLS slope (Δy/Δx) for paired lists of x and y values.

    Returns 0.0 for degenerate inputs (fewer than 2 points, zero variance in x).
    """
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0.0:
        return 0.0
    return num / denom


class EfficiencyDegradationRule:
    """
    Detects week-over-week creep in average heating session duration per floor.

    Args:
        history:                  List of ``heating_session_ended.v1`` event dicts,
                                  oldest first.
        min_weeks:                Minimum qualifying weeks required to run regression.
        min_events_per_week:      Minimum sessions in a week to include it.
        slope_threshold_s_per_week: OLS slope (seconds/week) above which a finding
                                  is emitted.
    """

    def __init__(
        self,
        history: list[dict],
        min_weeks: int = 3,
        min_events_per_week: int = 3,
        slope_threshold_s_per_week: float = 60.0,
    ) -> None:
        self._history = history or []
        self._min_weeks = min_weeks
        self._min_events_per_week = min_events_per_week
        self._slope_threshold = slope_threshold_s_per_week

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self) -> list[dict]:
        """
        Scan history for per-floor efficiency degradation trends.

        Returns:
            List of finding dicts (one per floor whose trend exceeds the threshold).
        """
        weekly = self._bucket_by_week()
        findings: list[dict] = []

        for floor, week_data in sorted(weekly.items()):
            # week_data: sorted list of (week_key, [duration_s, ...])
            qualifying = [
                (wk, durs) for wk, durs in week_data if len(durs) >= self._min_events_per_week
            ]
            if len(qualifying) < self._min_weeks:
                continue

            week_keys = [wk for wk, _ in qualifying]
            means = [sum(durs) / len(durs) for _, durs in qualifying]

            # x = week index (0, 1, 2, …)
            xs = list(range(len(means)))
            slope = _linear_slope(xs, means)

            if slope < self._slope_threshold:
                continue

            # Secondary guard: last 3 means must be monotonically non-decreasing
            last_three = means[-3:]
            if not all(last_three[i] <= last_three[i + 1] for i in range(len(last_three) - 1)):
                continue

            findings.append(
                {
                    "schema": "homeops.insights.efficiency_degradation.v1",
                    "source": "insights.efficiency_degradation.v1",
                    "ts": _utc_ts(),
                    "data": {
                        "floor": floor,
                        "slope_s_per_week": round(slope, 1),
                        "threshold_s_per_week": self._slope_threshold,
                        "weeks_analysed": len(qualifying),
                        "week_keys": week_keys,
                        "weekly_mean_s": [round(m, 1) for m in means],
                        "earliest_week": week_keys[0],
                        "latest_week": week_keys[-1],
                    },
                }
            )

        return findings

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bucket_by_week(self) -> dict[str, list[tuple[str, list[float]]]]:
        """
        Group session durations by floor and ISO week.

        Returns:
            ``{"floor_1": [("2026-W10", [320.0, 410.0, ...]), ...], ...}``
            Each floor's list is sorted chronologically by week key.
        """
        raw: dict[str, dict[str, list[float]]] = {}

        for evt in self._history:
            if evt.get("schema") != _SESSION_SCHEMA:
                continue
            data = evt.get("data", {})
            floor = data.get("floor")
            duration_s = data.get("duration_s")
            ts_str = evt.get("ts") or data.get("ended_at")
            if not floor or duration_s is None or not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue

            wk = _iso_week_key(dt)
            raw.setdefault(floor, {}).setdefault(wk, []).append(float(duration_s))

        result: dict[str, list[tuple[str, list[float]]]] = {}
        for floor, week_dict in raw.items():
            result[floor] = sorted(
                week_dict.items()
            )  # sort by week key string (lexicographic = chronological)

        return result
