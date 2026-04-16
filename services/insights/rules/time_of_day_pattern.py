"""
Rule: Time-of-Day Call Pattern Analysis

Detects when a floor's heating calls are clustering unusually in a specific
time-of-day bucket compared to that floor's own historical distribution.

Periods
-------
  night     00:00 – 05:59
  morning   06:00 – 11:59
  afternoon 12:00 – 17:59
  evening   18:00 – 23:59

A finding is emitted when a floor's share of calls in one period exceeds its
historical share by more than ``threshold_ratio`` (default 1.8×).

Guards
------
- Requires at least ``min_events`` (default 8) total historical session events.
- Only fires when the observed window has at least ``min_window_events``
  (default 3) calls in the anomalous period.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime

_SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"

_PERIODS: list[tuple[str, int, int]] = [
    ("night", 0, 5),
    ("morning", 6, 11),
    ("afternoon", 12, 17),
    ("evening", 18, 23),
]


def _period_for_hour(hour: int) -> str:
    for name, start, end in _PERIODS:
        if start <= hour <= end:
            return name
    return "unknown"


def _utc_ts() -> str:
    return datetime.now(UTC).isoformat()


class TimeOfDayPatternRule:
    """
    Detects unusual time-of-day clustering of heating calls per floor.

    Args:
        history:           List of ``heating_session_ended.v1`` event dicts
                           representing the historical baseline (oldest first).
        threshold_ratio:   How much higher than baseline share triggers a finding.
                           Default 1.8 means the observed period share must be
                           ≥ 1.8× the historical share.
        min_events:        Minimum total historical session events required to
                           compute a baseline. Fewer → no findings.
        min_window_events: Minimum calls in the flagged period during the
                           observation window. Fewer → no findings (avoids noise
                           from single-event periods).
    """

    def __init__(
        self,
        history: list[dict],
        threshold_ratio: float = 1.8,
        min_events: int = 8,
        min_window_events: int = 3,
    ) -> None:
        self._history = history or []
        self._threshold_ratio = threshold_ratio
        self._min_events = min_events
        self._min_window_events = min_window_events

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, window_events: list[dict]) -> list[dict]:
        """
        Compare call distribution in ``window_events`` to the historical baseline.

        Args:
            window_events: Recent ``heating_session_ended.v1`` events to analyse
                           (e.g. last 48 hours). These are compared to the
                           historical distribution built from ``self._history``.

        Returns:
            List of finding dicts (one per floor/period combination that exceeds
            the threshold). Empty list when no anomalies detected.
        """
        baseline = self._build_distribution(self._history)
        window_dist = self._build_distribution(window_events)

        findings: list[dict] = []

        all_floors = set(baseline) | set(window_dist)
        for floor in sorted(all_floors):
            hist_counts = baseline.get(floor, {})
            hist_total = sum(hist_counts.values())
            if hist_total < self._min_events:
                continue  # insufficient history for this floor

            obs_counts = window_dist.get(floor, {})
            obs_total = sum(obs_counts.values())
            if obs_total == 0:
                continue

            for period_name, _, _ in _PERIODS:
                hist_count = hist_counts.get(period_name, 0)
                obs_count = obs_counts.get(period_name, 0)

                if obs_count < self._min_window_events:
                    continue

                hist_share = hist_count / hist_total
                obs_share = obs_count / obs_total

                # Avoid division by zero for periods with zero history share
                if hist_share == 0:
                    if obs_count >= self._min_window_events:
                        ratio = float("inf")
                    else:
                        continue
                else:
                    ratio = obs_share / hist_share

                if ratio < self._threshold_ratio:
                    continue

                findings.append(
                    {
                        "schema": "homeops.insights.time_of_day_anomaly.v1",
                        "source": "insights.time_of_day_pattern.v1",
                        "ts": _utc_ts(),
                        "data": {
                            "floor": floor,
                            "period": period_name,
                            "observed_share": round(obs_share, 3),
                            "historical_share": round(hist_share, 3),
                            "ratio": round(ratio, 2) if not math.isinf(ratio) else None,
                            "observed_count": obs_count,
                            "observed_total": obs_total,
                            "historical_count": hist_count,
                            "historical_total": hist_total,
                            "threshold_ratio": self._threshold_ratio,
                        },
                    }
                )

        return findings

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_distribution(self, events: list[dict]) -> dict[str, dict[str, int]]:
        """
        Count session events per floor per time-of-day period.

        Returns:
            ``{"floor_1": {"night": 3, "morning": 12, ...}, ...}``
        """
        dist: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for evt in events:
            if evt.get("schema") != _SESSION_SCHEMA:
                continue
            data = evt.get("data", {})
            floor = data.get("floor")
            ts_str = evt.get("ts") or data.get("started_at") or data.get("ended_at")
            if not floor or not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                hour = dt.astimezone(UTC).hour
            except (ValueError, TypeError):
                continue
            period = _period_for_hour(hour)
            dist[floor][period] += 1

        # Convert nested defaultdicts to plain dicts for cleaner output
        return {floor: dict(periods) for floor, periods in dist.items()}
