"""
Rule: Heating Efficiency Metric

Computes a simple per-floor heating efficiency score: degrees Fahrenheit gained
per minute of furnace runtime, derived from completed heating sessions that have
both a ``duration_s`` and a ``temp_delta_f`` recorded.

This metric lets the Ask HomeOps chat widget answer questions like:
  - "Which floor is least efficient?"
  - "Is Floor 2 heating efficiently?"
  - "How has Floor 1's efficiency changed over the last two weeks?"

Output
------
``check()`` returns a list of efficiency-score dicts — one per floor that has
sufficient data — sorted by score ascending (least efficient first).

Guards
------
- Requires at least ``min_sessions`` (default 5) sessions with valid temp deltas.
- Sessions where ``temp_delta_f <= 0`` are excluded (setpoint already met,
  cooling period, sensor noise).
- Sessions shorter than ``min_duration_s`` (default 60 s) are excluded (short-
  cycles skew the metric).
"""

from __future__ import annotations

from datetime import UTC, datetime

_SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"

FLOOR_LABELS = {
    "floor_1": "Floor 1",
    "floor_2": "Floor 2",
    "floor_3": "Floor 3",
}


def _utc_ts() -> str:
    return datetime.now(UTC).isoformat()


class HeatingEfficiencyRule:
    """
    Computes per-floor heating efficiency from session history.

    Args:
        history:       List of ``heating_session_ended.v1`` event dicts,
                       oldest first.
        min_sessions:  Minimum sessions with valid temp deltas required to
                       compute a score for a floor.
        min_duration_s: Sessions shorter than this are excluded.
        lookback_days: Only consider sessions within this many days.
                       None means use all history.
    """

    def __init__(
        self,
        history: list[dict],
        min_sessions: int = 5,
        min_duration_s: int = 60,
        lookback_days: int | None = 14,
    ) -> None:
        self._history = history or []
        self._min_sessions = min_sessions
        self._min_duration_s = min_duration_s
        self._lookback_days = lookback_days

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self) -> list[dict]:
        """
        Compute per-floor efficiency scores.

        Returns:
            List of score dicts, sorted by ``score_f_per_min`` ascending
            (least efficient first). Returns an empty list when no floors
            have sufficient data.

            Each dict has schema ``homeops.insights.heating_efficiency.v1``.
        """
        cutoff = self._cutoff()
        per_floor: dict[str, list[tuple[float, float]]] = {}  # floor → [(duration_s, delta_f)]

        for evt in self._history:
            if evt.get("schema") != _SESSION_SCHEMA:
                continue
            data = evt.get("data", {})
            floor = data.get("floor")
            duration_s = data.get("duration_s")
            temp_delta_f = data.get("temp_delta_f")

            if not floor or duration_s is None or temp_delta_f is None:
                continue
            if duration_s < self._min_duration_s:
                continue
            if temp_delta_f <= 0:
                continue

            # Timestamp filter
            if cutoff is not None:
                ts_str = evt.get("ts") or data.get("ended_at")
                if ts_str:
                    try:
                        dt = datetime.fromisoformat(ts_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        if dt < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass

            per_floor.setdefault(floor, []).append((float(duration_s), float(temp_delta_f)))

        results: list[dict] = []

        for floor, sessions in sorted(per_floor.items()):
            if len(sessions) < self._min_sessions:
                continue

            total_duration_s = sum(d for d, _ in sessions)
            total_delta_f = sum(delta for _, delta in sessions)

            if total_duration_s == 0:
                continue

            score = total_delta_f / (total_duration_s / 60.0)  # °F per minute

            results.append(
                {
                    "schema": "homeops.insights.heating_efficiency.v1",
                    "source": "insights.heating_efficiency.v1",
                    "ts": _utc_ts(),
                    "data": {
                        "floor": floor,
                        "label": FLOOR_LABELS.get(floor, floor),
                        "score_f_per_min": round(score, 3),
                        "session_count": len(sessions),
                        "total_runtime_min": round(total_duration_s / 60.0, 1),
                        "total_temp_gain_f": round(total_delta_f, 1),
                        "lookback_days": self._lookback_days,
                    },
                }
            )

        # Sort least efficient first so callers can easily surface the worst floor
        results.sort(key=lambda r: r["data"]["score_f_per_min"])
        return results

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def summary_text(self) -> str:
        """
        Return a human-readable one-paragraph summary of floor efficiency scores,
        suitable for inclusion in the LLM context string.

        Returns an empty string when there is insufficient data.
        """
        scores = self.check()
        if not scores:
            return ""

        lines: list[str] = ["Heating Efficiency (°F gained per minute of runtime):"]
        for s in sorted(scores, key=lambda r: r["data"]["floor"]):
            d = s["data"]
            lines.append(
                f"  {d['label']}: {d['score_f_per_min']:.2f} °F/min "
                f"({d['session_count']} sessions, {d['total_runtime_min']} min total runtime)"
            )

        if len(scores) > 1:
            worst = scores[0]["data"]
            best = scores[-1]["data"]
            lines.append(
                f"  Least efficient: {worst['label']} ({worst['score_f_per_min']:.2f} °F/min)"
            )
            lines.append(
                f"  Most efficient: {best['label']} ({best['score_f_per_min']:.2f} °F/min)"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cutoff(self) -> datetime | None:
        if self._lookback_days is None:
            return None
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - __import__(
            "datetime"
        ).timedelta(days=self._lookback_days)
