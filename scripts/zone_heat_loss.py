#!/usr/bin/env python3
"""Estimate per-zone heat loss from thermostat cooling curves.

The command replays the derived event log without starting the consumer or
writing production state.  It pairs each ``floor_call_ended.v1`` event with
the next call for that floor, keeps thermostat temperature samples observed
while the furnace is known to be off and the thermostat is not actively
heating/cooling, and fits a temperature-versus-elapsed-time slope for each
continuous sample segment.  A positive ``heat_loss_rate_f_per_min`` is the
negative of that slope.

Large telemetry gaps are split into separate segments and insufficient or
flat/rising segments remain visible in data-quality counts rather than being
presented as cooling evidence.  The result is a measured baseline for future
comparison, not an insulation or equipment diagnosis.

Usage (last 30 UTC days):
    python3 scripts/zone_heat_loss.py --log state/consumer/events.jsonl

Usage with a historical heating-season range:
    python3 scripts/zone_heat_loss.py \
        --start 2026-03-20 --end 2026-05-31 \
        --log state/consumer/events.jsonl \
        --out reports/zone-heat-loss.md

Usage with JSON output for automation:
    python3 scripts/zone_heat_loss.py --days 90 --format json

Revision history:
  2026-08-23  Added read-only cooling-curve replay so per-zone temperature
              decay can be measured only during known furnace-off intervals,
              with explicit gap, activity, and insufficient-data accounting.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

FLOOR_CALL_STARTED_SCHEMA = "homeops.consumer.floor_call_started.v1"
FLOOR_CALL_ENDED_SCHEMA = "homeops.consumer.floor_call_ended.v1"
THERMOSTAT_TEMP_SCHEMA = "homeops.consumer.thermostat_current_temp_updated.v1"
FURNACE_STARTED_SCHEMA = "homeops.consumer.heating_session_started.v1"
FURNACE_ENDED_SCHEMA = "homeops.consumer.heating_session_ended.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_DAYS = 30
DEFAULT_MIN_SAMPLES = 3
DEFAULT_MIN_DURATION_MIN = 30.0
DEFAULT_MAX_GAP_MIN = 180.0
DEFAULT_MIN_OBSERVATIONS = 3
KNOWN_ZONES = ("floor_1", "floor_2", "floor_3")
RELEVANT_SCHEMAS = {
    FLOOR_CALL_STARTED_SCHEMA,
    FLOOR_CALL_ENDED_SCHEMA,
    THERMOSTAT_TEMP_SCHEMA,
    FURNACE_STARTED_SCHEMA,
    FURNACE_ENDED_SCHEMA,
}


@dataclass(frozen=True)
class TimelineEvent:
    """A relevant event with its source timestamp and stable input order."""

    timestamp: datetime
    source_order: int
    schema: str
    data: dict[str, Any]


@dataclass(frozen=True)
class TemperatureSample:
    """One thermostat temperature update for a zone."""

    timestamp: datetime
    temperature_f: float
    hvac_action: str | None


@dataclass(frozen=True)
class CoolingObservation:
    """One continuous, measurable cooling segment after a floor call ends."""

    zone: str
    call_ended_at: datetime
    next_call_started_at: datetime
    observed_start_at: datetime
    observed_end_at: datetime
    sample_count: int
    duration_min: float
    start_temp_f: float
    end_temp_f: float
    temperature_delta_f: float
    slope_f_per_min: float
    heat_loss_rate_f_per_min: float


def _finite_number(value: Any) -> float | None:
    """Return a finite JSON number, excluding booleans and strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize naive values to UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_timestamp(event: dict[str, Any], data: dict[str, Any]) -> datetime | None:
    """Choose the event's source timestamp, then fall back to envelope time."""
    for key in ("ts", "started_at", "ended_at"):
        timestamp = _parse_timestamp(data.get(key))
        if timestamp is not None:
            return timestamp
    return _parse_timestamp(event.get("ts"))


def _event_key(event: dict[str, Any]) -> str:
    """Return a stable identity for exact duplicate JSONL records."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def load_events(source: str | Path) -> list[TimelineEvent]:
    """Load relevant JSONL events, tolerating malformed and unrelated lines.

    Events are sorted by source timestamp while retaining input order as a
    tie-breaker. Exact duplicate JSON objects are ignored.
    """
    events: list[TimelineEvent] = []
    seen: set[str] = set()
    stream: TextIO
    close_stream = False
    if str(source) == "-":
        stream = sys.stdin
    else:
        try:
            stream = open(source, encoding="utf-8")
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"log file not found: {source}") from exc
        except OSError as exc:
            raise OSError(f"error reading log {source}: {exc}") from exc
        close_stream = True

    try:
        for source_order, line in enumerate(stream):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("schema") not in RELEVANT_SCHEMAS:
                continue
            key = _event_key(event)
            if key in seen:
                continue
            seen.add(key)
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            timestamp = _event_timestamp(event, data)
            if timestamp is None:
                continue
            events.append(TimelineEvent(timestamp, source_order, event["schema"], data))
    finally:
        if close_stream:
            stream.close()

    return sorted(events, key=lambda event: (event.timestamp, event.source_order))


def _zone_from_data(data: dict[str, Any]) -> str | None:
    """Resolve a thermostat zone, including the older entity-id shape."""
    zone = data.get("zone")
    if isinstance(zone, str) and zone:
        return zone
    entity_id = data.get("entity_id")
    if isinstance(entity_id, str) and entity_id.startswith("climate."):
        candidate = entity_id.removeprefix("climate.").removesuffix("_thermostat")
        return candidate or None
    return None


class FurnaceTimeline:
    """Answer whether the furnace state is known on/off at a timestamp."""

    def __init__(self, transitions: Iterable[tuple[datetime, bool]]) -> None:
        ordered = sorted(transitions, key=lambda item: item[0])
        self._timestamps = [timestamp for timestamp, _ in ordered]
        self._states = [state for _, state in ordered]

    def state_at(self, timestamp: datetime) -> bool | None:
        """Return ``True``/``False`` or ``None`` before the first transition."""
        index = bisect.bisect_right(self._timestamps, timestamp) - 1
        return self._states[index] if index >= 0 else None


def _split_on_gap(
    samples: list[TemperatureSample], max_gap_min: float
) -> list[list[TemperatureSample]]:
    """Split a sorted sample list when telemetry silence exceeds the limit."""
    if not samples:
        return []
    segments: list[list[TemperatureSample]] = []
    current = [samples[0]]
    for sample in samples[1:]:
        gap_min = (sample.timestamp - current[-1].timestamp).total_seconds() / 60.0
        if gap_min > max_gap_min:
            segments.append(current)
            current = []
        current.append(sample)
    segments.append(current)
    return segments


def _linear_slope(samples: list[TemperatureSample]) -> float | None:
    """Fit temperature change per minute for a sample segment."""
    if len(samples) < 2:
        return None
    first = samples[0].timestamp
    x_values = [(sample.timestamp - first).total_seconds() / 60.0 for sample in samples]
    y_values = [sample.temperature_f for sample in samples]
    mean_x = statistics.fmean(x_values)
    mean_y = statistics.fmean(y_values)
    denominator = sum((x - mean_x) ** 2 for x in x_values)
    if denominator == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(x_values, y_values)) / denominator


def build_cooling_observations(
    events: list[TimelineEvent],
    start: date,
    end: date,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_duration_min: float = DEFAULT_MIN_DURATION_MIN,
    max_gap_min: float = DEFAULT_MAX_GAP_MIN,
) -> tuple[list[CoolingObservation], dict[str, int]]:
    """Build cooling observations and data-quality counters for a date range.

    A cooling window starts at a floor call end and ends at the next call start
    for that floor. Only samples for which the furnace timeline explicitly says
    ``off`` and the thermostat action is not ``heating`` or ``cooling`` are
    eligible. The range is applied to the UTC date of the call end.
    """
    if start > end:
        raise ValueError("start date must be on or before end date")
    if min_samples < 2:
        raise ValueError("min_samples must be at least 2")
    if min_duration_min <= 0:
        raise ValueError("min_duration_min must be greater than 0")
    if max_gap_min <= 0:
        raise ValueError("max_gap_min must be greater than 0")

    starts: defaultdict[str, list[datetime]] = defaultdict(list)
    ends: defaultdict[str, list[datetime]] = defaultdict(list)
    temperatures: defaultdict[str, list[TemperatureSample]] = defaultdict(list)
    transitions: list[tuple[datetime, bool]] = []

    for event in events:
        if event.schema == FLOOR_CALL_STARTED_SCHEMA:
            zone = event.data.get("floor")
            if isinstance(zone, str) and zone:
                starts[zone].append(event.timestamp)
        elif event.schema == FLOOR_CALL_ENDED_SCHEMA:
            zone = event.data.get("floor")
            if isinstance(zone, str) and zone:
                ends[zone].append(event.timestamp)
        elif event.schema == THERMOSTAT_TEMP_SCHEMA:
            zone = _zone_from_data(event.data)
            temperature_f = _finite_number(event.data.get("current_temp"))
            if zone is not None and temperature_f is not None:
                hvac_action = event.data.get("hvac_action")
                temperatures[zone].append(
                    TemperatureSample(event.timestamp, temperature_f, hvac_action)
                )
        elif event.schema == FURNACE_STARTED_SCHEMA:
            transitions.append((event.timestamp, True))
        elif event.schema == FURNACE_ENDED_SCHEMA:
            transitions.append((event.timestamp, False))

    for zone in starts:
        starts[zone].sort()
    for zone in ends:
        ends[zone].sort()
    for zone in temperatures:
        # The loader removes exact duplicate objects; this also makes same-time
        # updates deterministic if different records carry the same source ts.
        temperatures[zone].sort(key=lambda sample: sample.timestamp)

    furnace = FurnaceTimeline(transitions)
    stats = {
        "call_windows": 0,
        "windows_with_temperature_samples": 0,
        "furnace_on_samples_excluded": 0,
        "furnace_unknown_samples_excluded": 0,
        "thermostat_active_samples_excluded": 0,
        "furnace_off_idle_samples": 0,
        "segments_too_few_samples": 0,
        "segments_too_short": 0,
        "qualifying_segments": 0,
        "non_cooling_segments": 0,
    }
    observations: list[CoolingObservation] = []

    for zone in sorted(ends):
        zone_starts = starts.get(zone, [])
        zone_temperatures = temperatures.get(zone, [])
        for call_end in ends[zone]:
            if not start <= call_end.date() <= end:
                continue
            next_start_index = bisect.bisect_right(zone_starts, call_end)
            if next_start_index >= len(zone_starts):
                continue
            next_call_start = zone_starts[next_start_index]
            stats["call_windows"] += 1

            window_samples = [
                sample
                for sample in zone_temperatures
                if call_end <= sample.timestamp < next_call_start
            ]
            if window_samples:
                stats["windows_with_temperature_samples"] += 1

            off_idle_samples: list[TemperatureSample] = []
            for sample in window_samples:
                furnace_state = furnace.state_at(sample.timestamp)
                if furnace_state is not False:
                    key = (
                        "furnace_on_samples_excluded"
                        if furnace_state is True
                        else "furnace_unknown_samples_excluded"
                    )
                    stats[key] += 1
                    continue
                if sample.hvac_action in {"heating", "cooling"}:
                    stats["thermostat_active_samples_excluded"] += 1
                    continue
                stats["furnace_off_idle_samples"] += 1
                off_idle_samples.append(sample)

            for segment in _split_on_gap(off_idle_samples, max_gap_min):
                if len(segment) < min_samples:
                    stats["segments_too_few_samples"] += 1
                    continue
                duration_min = (segment[-1].timestamp - segment[0].timestamp).total_seconds() / 60.0
                if duration_min < min_duration_min:
                    stats["segments_too_short"] += 1
                    continue
                stats["qualifying_segments"] += 1
                slope = _linear_slope(segment)
                if (
                    slope is None
                    or slope >= 0
                    or segment[-1].temperature_f >= segment[0].temperature_f
                ):
                    stats["non_cooling_segments"] += 1
                    continue
                start_temp_f = segment[0].temperature_f
                end_temp_f = segment[-1].temperature_f
                observations.append(
                    CoolingObservation(
                        zone=zone,
                        call_ended_at=call_end,
                        next_call_started_at=next_call_start,
                        observed_start_at=segment[0].timestamp,
                        observed_end_at=segment[-1].timestamp,
                        sample_count=len(segment),
                        duration_min=duration_min,
                        start_temp_f=start_temp_f,
                        end_temp_f=end_temp_f,
                        temperature_delta_f=end_temp_f - start_temp_f,
                        slope_f_per_min=slope,
                        heat_loss_rate_f_per_min=-slope,
                    )
                )

    observations.sort(key=lambda item: (item.observed_start_at, item.zone))
    return observations, stats


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def _observation_dict(observation: CoolingObservation) -> dict[str, Any]:
    return {
        "zone": observation.zone,
        "call_ended_at": observation.call_ended_at.isoformat(),
        "next_call_started_at": observation.next_call_started_at.isoformat(),
        "observed_start_at": observation.observed_start_at.isoformat(),
        "observed_end_at": observation.observed_end_at.isoformat(),
        "sample_count": observation.sample_count,
        "duration_min": _round(observation.duration_min, 1),
        "start_temp_f": _round(observation.start_temp_f, 1),
        "end_temp_f": _round(observation.end_temp_f, 1),
        "temperature_delta_f": _round(observation.temperature_delta_f, 1),
        "slope_f_per_min": _round(observation.slope_f_per_min, 5),
        "heat_loss_rate_f_per_min": _round(observation.heat_loss_rate_f_per_min, 5),
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile without external dependencies."""
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def build_report(
    events: list[TimelineEvent],
    start: date,
    end: date,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_duration_min: float = DEFAULT_MIN_DURATION_MIN,
    max_gap_min: float = DEFAULT_MAX_GAP_MIN,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    source: str = "events.jsonl",
) -> dict[str, Any]:
    """Build a JSON-serializable zone heat-loss report."""
    if min_observations < 1:
        raise ValueError("min_observations must be at least 1")
    observations, stats = build_cooling_observations(
        events,
        start,
        end,
        min_samples=min_samples,
        min_duration_min=min_duration_min,
        max_gap_min=max_gap_min,
    )

    zones = sorted(set(KNOWN_ZONES) | {observation.zone for observation in observations})
    zone_reports: list[dict[str, Any]] = []
    for zone in zones:
        zone_observations = [
            observation for observation in observations if observation.zone == zone
        ]
        rates = [observation.heat_loss_rate_f_per_min for observation in zone_observations]
        zone_reports.append(
            {
                "zone": zone,
                "observation_count": len(zone_observations),
                "min_observations": min_observations,
                "date_range": {
                    "first": (
                        zone_observations[0].observed_start_at.isoformat()
                        if zone_observations
                        else None
                    ),
                    "last": (
                        zone_observations[-1].observed_end_at.isoformat()
                        if zone_observations
                        else None
                    ),
                },
                "median_heat_loss_rate_f_per_min": _round(
                    statistics.median(rates) if rates else None, 5
                ),
                "mean_heat_loss_rate_f_per_min": _round(
                    statistics.fmean(rates) if rates else None, 5
                ),
                "p25_heat_loss_rate_f_per_min": _round(_percentile(rates, 0.25), 5),
                "p75_heat_loss_rate_f_per_min": _round(_percentile(rates, 0.75), 5),
                "status": "ok"
                if len(zone_observations) >= min_observations
                else "insufficient_data",
                "observations": [_observation_dict(item) for item in zone_observations],
            }
        )

    return {
        "schema": "homeops.zone-heat-loss-report.v1",
        "source": source,
        "method": (
            "Pair each floor-call end with the next call, keep thermostat samples while the "
            "furnace timeline is known off and HVAC action is idle, split telemetry gaps, and "
            "fit temperature °F versus elapsed minutes by ordinary least squares."
        ),
        "coverage": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "cooling_windows": stats["call_windows"],
            "qualifying_segments": stats["qualifying_segments"],
            "cooling_observations": len(observations),
            "zones": len(zones),
        },
        "configuration": {
            "min_samples": min_samples,
            "min_duration_min": min_duration_min,
            "max_gap_min": max_gap_min,
            "min_observations": min_observations,
            "rate_units": "degrees Fahrenheit per minute",
        },
        "data_quality": stats,
        "zones_detail": zone_reports,
        "interpretation_guard": (
            "A rate describes observed temperature decay during known furnace-off, thermostat-idle "
            "windows. It is not proof of insulation loss, equipment failure, or a recommended "
            "pre-heat schedule; compare future measurements with sensor and maintenance context."
        ),
    }


def _fmt_rate(value: float | None) -> str:
    return f"{value:.4f}°F/min" if value is not None else "—"


def _fmt_minutes(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}m"


def render_markdown(report: dict[str, Any], file: TextIO | None = None) -> str:
    """Render a human-readable cooling-curve report."""
    lines = [
        "# Zone cooling-curve heat loss analysis",
        "",
        f"Source: `{report['source']}`",
        (
            f"Coverage: `{report['coverage']['start']}` → `{report['coverage']['end']}`; "
            f"{report['coverage']['cooling_observations']} measurable cooling curves"
        ),
        "",
        "A positive rate is the observed temperature decay while the furnace is known off "
        "and the thermostat is idle.",
        "",
        "## Zone summaries",
        "",
        "| Zone | Curves | Median loss rate | P25–P75 | Status |",
        "|---|---:|---:|---:|---|",
    ]
    for zone in report["zones_detail"]:
        p25 = zone["p25_heat_loss_rate_f_per_min"]
        p75 = zone["p75_heat_loss_rate_f_per_min"]
        spread = f"{_fmt_rate(p25)} – {_fmt_rate(p75)}" if p25 is not None else "—"
        lines.append(
            f"| {zone['zone']} | {zone['observation_count']} | "
            f"{_fmt_rate(zone['median_heat_loss_rate_f_per_min'])} | {spread} | "
            f"{zone['status']} |"
        )

    lines.extend(["", "## Cooling curves", ""])
    observations = [
        observation for zone in report["zones_detail"] for observation in zone["observations"]
    ]
    if observations:
        lines.extend(
            [
                "| Zone | Observed start | Observed end | Samples | Duration | Temp Δ | "
                "Loss rate |",
                "|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for observation in observations:
            lines.append(
                f"| {observation['zone']} | {observation['observed_start_at']} | "
                f"{observation['observed_end_at']} | {observation['sample_count']} | "
                f"{_fmt_minutes(observation['duration_min'])} | "
                f"{observation['temperature_delta_f']:.1f}°F | "
                f"{_fmt_rate(observation['heat_loss_rate_f_per_min'])} |"
            )
    else:
        lines.append("No measurable cooling curves were found in the selected range.")

    quality = report["data_quality"]
    lines.extend(
        [
            "",
            "## Data quality",
            "",
            f"- Cooling windows with a following call: {quality['call_windows']}",
            f"- Furnace-off idle samples used: {quality['furnace_off_idle_samples']}",
            f"- Furnace-on samples excluded: {quality['furnace_on_samples_excluded']}",
            "- Unknown furnace-state samples excluded: "
            f"{quality['furnace_unknown_samples_excluded']}",
            "- Thermostat-active samples excluded: "
            f"{quality['thermostat_active_samples_excluded']}",
            f"- Segments rejected for too few samples: {quality['segments_too_few_samples']}",
            f"- Segments rejected for short duration: {quality['segments_too_short']}",
            "",
            "## Interpretation guard",
            "",
            report["interpretation_guard"],
        ]
    )
    output = "\n".join(lines).rstrip("\n")
    if file is not None:
        print(output, file=file)
    return output


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive number: {value}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _minimum_samples(value: str) -> int:
    parsed = _positive_int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("min-samples must be at least 2")
    return parsed


def _resolve_range(
    days: int = DEFAULT_DAYS,
    start: date | None = None,
    end: date | None = None,
) -> tuple[date, date]:
    """Resolve an inclusive UTC range from explicit dates or trailing days."""
    if (start is None) != (end is None):
        raise ValueError("--start and --end must be provided together")
    if start is not None and end is not None:
        if start > end:
            raise ValueError("start date must be on or before end date")
        return start, end
    if days < 1:
        raise ValueError("days must be at least 1")
    end = datetime.now(UTC).date()
    return end - timedelta(days=days - 1), end


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=_positive_int,
        default=DEFAULT_DAYS,
        help=f"Number of trailing UTC days to include (default: {DEFAULT_DAYS})",
    )
    parser.add_argument("--start", type=_parse_date, help="Inclusive UTC start date")
    parser.add_argument("--end", type=_parse_date, help="Inclusive UTC end date")
    parser.add_argument(
        "--min-samples",
        type=_minimum_samples,
        default=DEFAULT_MIN_SAMPLES,
        help=f"Minimum temperature samples per curve (default: {DEFAULT_MIN_SAMPLES})",
    )
    parser.add_argument(
        "--min-duration-min",
        type=_positive_float,
        default=DEFAULT_MIN_DURATION_MIN,
        help=f"Minimum curve duration in minutes (default: {DEFAULT_MIN_DURATION_MIN})",
    )
    parser.add_argument(
        "--max-gap-min",
        type=_positive_float,
        default=DEFAULT_MAX_GAP_MIN,
        help=f"Split curves after this telemetry gap in minutes (default: {DEFAULT_MAX_GAP_MIN})",
    )
    parser.add_argument(
        "--min-observations",
        type=_positive_int,
        default=DEFAULT_MIN_OBSERVATIONS,
        help=f"Curves required for zone status=ok (default: {DEFAULT_MIN_OBSERVATIONS})",
    )
    parser.add_argument("--log", default=None, help="Derived event JSONL path")
    parser.add_argument("--out", help="Optional output path; defaults to stdout")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        start, end = _resolve_range(args.days, args.start, args.end)
        log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
        events = load_events(log_path)
        report = build_report(
            events,
            start,
            end,
            min_samples=args.min_samples,
            min_duration_min=args.min_duration_min,
            max_gap_min=args.max_gap_min,
            min_observations=args.min_observations,
            source=str(log_path),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(
            f"Report written → {output_path} "
            f"({report['coverage']['cooling_observations']} measurable cooling curves)"
        )
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
