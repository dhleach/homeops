#!/usr/bin/env python3
"""Measure furnace seconds required for each zone to gain one degree.

The command replays the derived event log without starting the consumer or
writing production state.  It pairs completed ``floor_call_ended.v1`` events
with the furnace on-time that overlaps each zone call, brackets the call with
the nearest thermostat temperature readings, and joins an outdoor temperature
for deterministic temperature buckets.  Results are grouped by zone and
outdoor-temperature bucket so a future daily summary can consume the JSON
artifact without changing the live event pipeline.

An observation is deliberately discarded when its call duration is incomplete,
its thermostat boundary readings are missing or stale, its temperature change
is not positive, its call has no measured furnace on-time, or its outdoor
temperature is unavailable.  Those cases remain visible in ``data_quality``.
Furnace on-time is attributed to every overlapping zone call; therefore values
must not be summed across zones when comparing shared-furnace demand.

Usage (last 30 UTC days):
    python3 scripts/runtime_per_degree.py --log state/consumer/events.jsonl

Usage with an explicit historical range:
    python3 scripts/runtime_per_degree.py \
        --start 2026-03-20 --end 2026-05-31 \
        --log state/consumer/events.jsonl \
        --out reports/runtime-per-degree.json --format json

Revision history:
  2026-08-23  Added read-only per-zone furnace-runtime-per-degree analysis with
              outdoor-temperature buckets and explicit telemetry-quality guards
              so efficiency trends can feed future daily summaries safely.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import statistics
import sys
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

FLOOR_CALL_STARTED_SCHEMA = "homeops.consumer.floor_call_started.v1"
FLOOR_CALL_ENDED_SCHEMA = "homeops.consumer.floor_call_ended.v1"
FURNACE_STARTED_SCHEMA = "homeops.consumer.heating_session_started.v1"
FURNACE_ENDED_SCHEMA = "homeops.consumer.heating_session_ended.v1"
THERMOSTAT_SCHEMAS = frozenset(
    {
        "homeops.consumer.thermostat_current_temp_updated.v1",
        "homeops.consumer.thermostat_mode_changed.v1",
        "homeops.consumer.thermostat_setpoint_changed.v1",
    }
)
OUTDOOR_SCHEMA = "homeops.consumer.outdoor_temp_updated.v1"
RELEVANT_SCHEMAS = frozenset(
    {
        FLOOR_CALL_STARTED_SCHEMA,
        FLOOR_CALL_ENDED_SCHEMA,
        FURNACE_STARTED_SCHEMA,
        FURNACE_ENDED_SCHEMA,
        OUTDOOR_SCHEMA,
        *THERMOSTAT_SCHEMAS,
    }
)
KNOWN_ZONES = ("floor_1", "floor_2", "floor_3")
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_DAYS = 30
DEFAULT_MIN_OBSERVATIONS = 3
DEFAULT_MAX_TEMP_GAP_MIN = 30.0
DEFAULT_MAX_OUTDOOR_GAP_MIN = 180.0
DEFAULT_BUCKET_WIDTH_F = 10.0


@dataclass(frozen=True)
class TimelineEvent:
    """A relevant event with its source timestamp and stable input order."""

    timestamp: datetime
    source_order: int
    schema: str
    data: dict[str, Any]


@dataclass(frozen=True)
class TemperatureSample:
    """One thermostat temperature observation for a zone."""

    timestamp: datetime
    temperature_f: float


@dataclass(frozen=True)
class OutdoorSample:
    """One outdoor-temperature observation."""

    timestamp: datetime
    temperature_f: float


@dataclass(frozen=True)
class FurnaceRun:
    """One completed furnace run reconstructed from its end event."""

    started_at: datetime
    ended_at: datetime
    outdoor_temp_f: float | None


@dataclass(frozen=True)
class HeatingCall:
    """One completed zone call with a measured duration."""

    zone: str
    started_at: datetime
    ended_at: datetime
    duration_s: float


@dataclass(frozen=True)
class RuntimeObservation:
    """One call with all measurements required for the efficiency ratio."""

    zone: str
    call_started_at: datetime
    call_ended_at: datetime
    call_duration_s: float
    furnace_on_time_s: float
    start_temp_f: float
    end_temp_f: float
    temperature_delta_f: float
    runtime_per_degree_s: float
    outdoor_temp_f: float
    outdoor_temp_source: str
    outdoor_bucket_lower_f: float
    outdoor_bucket_upper_f: float


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
    """Choose the source timestamp, then fall back to the envelope timestamp."""
    for key in ("ts", "started_at", "ended_at", "timestamp"):
        timestamp = _parse_timestamp(data.get(key))
        if timestamp is not None:
            return timestamp
    return _parse_timestamp(event.get("ts"))


def _event_key(event: dict[str, Any]) -> str:
    """Return a stable identity for exact duplicate JSONL records."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def load_events(source: str | Path) -> list[TimelineEvent]:
    """Load relevant JSONL events, tolerating malformed and unrelated lines."""
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
    """Resolve a canonical zone from a floor field or legacy entity ID."""
    for key in ("floor", "zone"):
        zone = data.get(key)
        if isinstance(zone, str) and zone:
            return zone
    entity_id = data.get("entity_id")
    if isinstance(entity_id, str):
        for zone in KNOWN_ZONES:
            if entity_id in {
                f"binary_sensor.{zone}_heating_call",
                f"climate.{zone}_thermostat",
            }:
                return zone
    return None


def _temperature_samples(events: Iterable[TimelineEvent]) -> dict[str, list[TemperatureSample]]:
    """Collect one deterministic temperature sample per zone and timestamp."""
    by_key: dict[tuple[str, datetime], TemperatureSample] = {}
    for event in events:
        if event.schema not in THERMOSTAT_SCHEMAS:
            continue
        zone = _zone_from_data(event.data)
        temperature_f = _finite_number(event.data.get("current_temp"))
        if zone is None or temperature_f is None:
            continue
        by_key[(zone, event.timestamp)] = TemperatureSample(event.timestamp, temperature_f)

    grouped: defaultdict[str, list[TemperatureSample]] = defaultdict(list)
    for (zone, _), sample in by_key.items():
        grouped[zone].append(sample)
    for samples in grouped.values():
        samples.sort(key=lambda sample: sample.timestamp)
    return dict(grouped)


def _outdoor_samples(events: Iterable[TimelineEvent]) -> list[OutdoorSample]:
    """Collect valid outdoor readings in source order, then sort by timestamp."""
    samples = []
    for event in events:
        if event.schema != OUTDOOR_SCHEMA:
            continue
        temperature_f = _finite_number(event.data.get("temperature_f"))
        if temperature_f is not None:
            samples.append(OutdoorSample(event.timestamp, temperature_f))
    return sorted(samples, key=lambda sample: sample.timestamp)


def _furnace_runs(events: Iterable[TimelineEvent]) -> list[FurnaceRun]:
    """Reconstruct completed furnace intervals from end-event durations."""
    by_interval: dict[tuple[datetime, datetime], FurnaceRun] = {}
    for event in events:
        if event.schema != FURNACE_ENDED_SCHEMA:
            continue
        duration_s = _finite_number(event.data.get("duration_s"))
        if duration_s is None or duration_s <= 0:
            continue
        ended_at = event.timestamp
        started_at = ended_at - timedelta(seconds=duration_s)
        if started_at >= ended_at:
            continue
        run = FurnaceRun(
            started_at,
            ended_at,
            _finite_number(event.data.get("outdoor_temp_f")),
        )
        by_interval[(started_at, ended_at)] = run

    ordered = sorted(by_interval.values(), key=lambda run: (run.started_at, run.ended_at))
    if not ordered:
        return []

    # A replayed or partially duplicated log can create overlapping intervals.
    # Merge them so shared furnace on-time is never counted twice for a call.
    merged: list[FurnaceRun] = [ordered[0]]
    for run in ordered[1:]:
        previous = merged[-1]
        if run.started_at <= previous.ended_at:
            merged[-1] = FurnaceRun(
                previous.started_at,
                max(previous.ended_at, run.ended_at),
                run.outdoor_temp_f
                if run.ended_at >= previous.ended_at
                else previous.outdoor_temp_f,
            )
        else:
            merged.append(run)
    return merged


def _completed_calls(
    events: Iterable[TimelineEvent],
    start: date,
    end: date,
) -> tuple[list[HeatingCall], dict[str, int]]:
    """Pair floor starts and ends and return calls ending in the date range."""
    pending: defaultdict[str, deque[datetime]] = defaultdict(deque)
    calls: list[HeatingCall] = []
    stats = {
        "call_ends_in_range": 0,
        "incomplete_calls": 0,
        "invalid_duration_calls": 0,
        "completed_calls": 0,
        "calls_using_derived_start": 0,
    }

    for event in events:
        if event.schema == FLOOR_CALL_STARTED_SCHEMA:
            zone = _zone_from_data(event.data)
            if zone is not None:
                pending[zone].append(event.timestamp)
            continue
        if event.schema != FLOOR_CALL_ENDED_SCHEMA:
            continue
        zone = _zone_from_data(event.data)
        if zone is None or not start <= event.timestamp.date() <= end:
            continue
        stats["call_ends_in_range"] += 1
        matched_start = pending[zone].popleft() if pending[zone] else None
        duration_s = _finite_number(event.data.get("duration_s"))
        if duration_s is None:
            stats["incomplete_calls"] += 1
            continue
        if duration_s <= 0:
            stats["invalid_duration_calls"] += 1
            continue

        started_at = matched_start
        if started_at is None:
            started_at = event.timestamp - timedelta(seconds=duration_s)
            stats["calls_using_derived_start"] += 1
        if started_at >= event.timestamp:
            stats["invalid_duration_calls"] += 1
            continue
        calls.append(HeatingCall(zone, started_at, event.timestamp, duration_s))
        stats["completed_calls"] += 1

    return calls, stats


def _latest_at_or_before(
    samples: list[TemperatureSample] | list[OutdoorSample],
    timestamp: datetime,
) -> TemperatureSample | OutdoorSample | None:
    """Return the latest sample at or before a timestamp."""
    timestamps = [sample.timestamp for sample in samples]
    index = bisect.bisect_right(timestamps, timestamp) - 1
    return samples[index] if index >= 0 else None


def _earliest_at_or_after(
    samples: list[TemperatureSample], timestamp: datetime
) -> TemperatureSample | None:
    """Return the earliest thermostat sample at or after a timestamp."""
    timestamps = [sample.timestamp for sample in samples]
    index = bisect.bisect_left(timestamps, timestamp)
    return samples[index] if index < len(samples) else None


def _overlap_seconds(call: HeatingCall, runs: list[FurnaceRun]) -> float:
    """Sum furnace on-time overlapping a zone call."""
    total = 0.0
    for run in runs:
        overlap_start = max(call.started_at, run.started_at)
        overlap_end = min(call.ended_at, run.ended_at)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds()
    return total


def _bucket_bounds(temperature_f: float, width_f: float) -> tuple[float, float]:
    """Return the half-open outdoor-temperature bucket containing a reading."""
    lower = math.floor(temperature_f / width_f) * width_f
    upper = lower + width_f
    return round(lower, 6), round(upper, 6)


def _bucket_label(lower: float, upper: float) -> str:
    """Render a stable half-open bucket label for JSON and Markdown."""
    return f"[{_format_number(lower)}, {_format_number(upper)})°F"


def _format_number(value: float) -> str:
    """Format integral bucket bounds without unnecessary decimal places."""
    return f"{value:g}"


def build_observations(
    events: list[TimelineEvent],
    start: date,
    end: date,
    *,
    max_temp_gap_min: float = DEFAULT_MAX_TEMP_GAP_MIN,
    max_outdoor_gap_min: float = DEFAULT_MAX_OUTDOOR_GAP_MIN,
    bucket_width_f: float = DEFAULT_BUCKET_WIDTH_F,
) -> tuple[list[RuntimeObservation], dict[str, int]]:
    """Build valid runtime-per-degree observations and quality counters."""
    if start > end:
        raise ValueError("start date must be on or before end date")
    if max_temp_gap_min <= 0:
        raise ValueError("max_temp_gap_min must be greater than 0")
    if max_outdoor_gap_min <= 0:
        raise ValueError("max_outdoor_gap_min must be greater than 0")
    if bucket_width_f <= 0:
        raise ValueError("bucket_width_f must be greater than 0")

    calls, stats = _completed_calls(events, start, end)
    stats.update(
        {
            "calls_without_furnace_runtime": 0,
            "calls_missing_temperature_boundary": 0,
            "calls_with_stale_temperature_boundary": 0,
            "calls_with_non_positive_temperature_delta": 0,
            "calls_missing_outdoor_temperature": 0,
            "outdoor_event_temperatures_used": 0,
            "furnace_session_temperatures_used": 0,
            "eligible_observations": 0,
        }
    )
    temperatures = _temperature_samples(events)
    outdoor_samples = _outdoor_samples(events)
    furnace_runs = _furnace_runs(events)
    observations: list[RuntimeObservation] = []

    for call in calls:
        zone_samples = temperatures.get(call.zone, [])
        start_sample = _latest_at_or_before(zone_samples, call.started_at)
        end_sample = _earliest_at_or_after(zone_samples, call.ended_at)
        if start_sample is None or end_sample is None:
            stats["calls_missing_temperature_boundary"] += 1
            continue
        start_age_min = (call.started_at - start_sample.timestamp).total_seconds() / 60.0
        end_age_min = (end_sample.timestamp - call.ended_at).total_seconds() / 60.0
        if start_age_min > max_temp_gap_min or end_age_min > max_temp_gap_min:
            stats["calls_with_stale_temperature_boundary"] += 1
            continue

        temperature_delta_f = end_sample.temperature_f - start_sample.temperature_f
        if not math.isfinite(temperature_delta_f) or temperature_delta_f <= 0:
            stats["calls_with_non_positive_temperature_delta"] += 1
            continue

        furnace_on_time_s = _overlap_seconds(call, furnace_runs)
        if furnace_on_time_s <= 0:
            stats["calls_without_furnace_runtime"] += 1
            continue

        outdoor_source = "outdoor_event"
        outdoor_sample = _latest_at_or_before(outdoor_samples, call.ended_at)
        outdoor_temp_f: float | None = None
        if outdoor_sample is not None:
            outdoor_age_min = (call.ended_at - outdoor_sample.timestamp).total_seconds() / 60.0
            if outdoor_age_min <= max_outdoor_gap_min:
                outdoor_temp_f = outdoor_sample.temperature_f

        if outdoor_temp_f is None:
            matching_runs = [
                run
                for run in furnace_runs
                if call.started_at <= run.ended_at <= call.ended_at
                and run.outdoor_temp_f is not None
            ]
            if matching_runs:
                matching_runs.sort(key=lambda run: run.ended_at)
                outdoor_temp_f = matching_runs[-1].outdoor_temp_f
                outdoor_source = "furnace_session_end"

        if outdoor_temp_f is None:
            stats["calls_missing_outdoor_temperature"] += 1
            continue
        if outdoor_source == "outdoor_event":
            stats["outdoor_event_temperatures_used"] += 1
        else:
            stats["furnace_session_temperatures_used"] += 1

        lower, upper = _bucket_bounds(outdoor_temp_f, bucket_width_f)
        observations.append(
            RuntimeObservation(
                zone=call.zone,
                call_started_at=call.started_at,
                call_ended_at=call.ended_at,
                call_duration_s=call.duration_s,
                furnace_on_time_s=furnace_on_time_s,
                start_temp_f=start_sample.temperature_f,
                end_temp_f=end_sample.temperature_f,
                temperature_delta_f=temperature_delta_f,
                runtime_per_degree_s=furnace_on_time_s / temperature_delta_f,
                outdoor_temp_f=outdoor_temp_f,
                outdoor_temp_source=outdoor_source,
                outdoor_bucket_lower_f=lower,
                outdoor_bucket_upper_f=upper,
            )
        )

    observations.sort(key=lambda item: (item.call_ended_at, item.zone))
    stats["eligible_observations"] = len(observations)
    return observations, stats


def _round(value: float | None, digits: int = 3) -> float | None:
    """Round a finite report value while preserving missingness."""
    return round(value, digits) if value is not None else None


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


def _observation_dict(observation: RuntimeObservation) -> dict[str, Any]:
    """Convert an observation to stable machine-readable fields."""
    return {
        "zone": observation.zone,
        "call_started_at": observation.call_started_at.isoformat(),
        "call_ended_at": observation.call_ended_at.isoformat(),
        "call_duration_s": _round(observation.call_duration_s, 1),
        "furnace_on_time_s": _round(observation.furnace_on_time_s, 1),
        "start_temp_f": _round(observation.start_temp_f, 1),
        "end_temp_f": _round(observation.end_temp_f, 1),
        "temperature_delta_f": _round(observation.temperature_delta_f, 3),
        "runtime_per_degree_s": _round(observation.runtime_per_degree_s, 3),
        "outdoor_temp_f": _round(observation.outdoor_temp_f, 1),
        "outdoor_temp_source": observation.outdoor_temp_source,
        "outdoor_temp_bucket": _bucket_label(
            observation.outdoor_bucket_lower_f,
            observation.outdoor_bucket_upper_f,
        ),
    }


def _bucket_report(
    observations: list[RuntimeObservation],
    min_observations: int,
) -> dict[str, Any]:
    """Aggregate observations for one zone/outdoor bucket."""
    values = [item.runtime_per_degree_s for item in observations]
    first = observations[0]
    return {
        "outdoor_temp_bucket": _bucket_label(
            first.outdoor_bucket_lower_f,
            first.outdoor_bucket_upper_f,
        ),
        "lower_bound_f": first.outdoor_bucket_lower_f,
        "upper_bound_f": first.outdoor_bucket_upper_f,
        "observation_count": len(observations),
        "min_observations": min_observations,
        "mean_runtime_per_degree_s": _round(statistics.fmean(values)),
        "median_runtime_per_degree_s": _round(statistics.median(values)),
        "p25_runtime_per_degree_s": _round(_percentile(values, 0.25)),
        "p75_runtime_per_degree_s": _round(_percentile(values, 0.75)),
        "status": "ok" if len(observations) >= min_observations else "insufficient_data",
        "observations": [_observation_dict(item) for item in observations],
    }


def build_report(
    events: list[TimelineEvent],
    start: date,
    end: date,
    *,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    max_temp_gap_min: float = DEFAULT_MAX_TEMP_GAP_MIN,
    max_outdoor_gap_min: float = DEFAULT_MAX_OUTDOOR_GAP_MIN,
    bucket_width_f: float = DEFAULT_BUCKET_WIDTH_F,
    source: str = "events.jsonl",
) -> dict[str, Any]:
    """Build a JSON-serializable runtime-per-degree report."""
    if min_observations < 1:
        raise ValueError("min_observations must be at least 1")
    observations, quality = build_observations(
        events,
        start,
        end,
        max_temp_gap_min=max_temp_gap_min,
        max_outdoor_gap_min=max_outdoor_gap_min,
        bucket_width_f=bucket_width_f,
    )

    grouped: defaultdict[str, defaultdict[tuple[float, float], list[RuntimeObservation]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for observation in observations:
        grouped[observation.zone][
            (observation.outdoor_bucket_lower_f, observation.outdoor_bucket_upper_f)
        ].append(observation)

    zones = sorted(set(KNOWN_ZONES) | set(grouped))
    zone_reports: list[dict[str, Any]] = []
    bucket_count = 0
    for zone in zones:
        zone_observations = sorted(
            [item for bucket in grouped[zone].values() for item in bucket],
            key=lambda item: item.call_ended_at,
        )
        buckets = []
        for bounds, bucket_observations in sorted(grouped[zone].items()):
            buckets.append(_bucket_report(bucket_observations, min_observations))
        bucket_count += len(buckets)
        zone_reports.append(
            {
                "zone": zone,
                "observation_count": len(zone_observations),
                "min_observations": min_observations,
                "bucket_count": len(buckets),
                "status": "ok"
                if len(zone_observations) >= min_observations
                else "insufficient_data",
                "buckets": buckets,
            }
        )

    return {
        "schema": "homeops.runtime-per-degree-report.v1",
        "source": source,
        "method": (
            "Pair completed zone calls with overlapping completed furnace on-time, bracket each "
            "call with the nearest thermostat current-temperature readings, and divide furnace "
            "seconds by the positive zone temperature rise. Outdoor readings are assigned to "
            "half-open temperature buckets."
        ),
        "coverage": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "call_ends_in_range": quality["call_ends_in_range"],
            "completed_calls": quality["completed_calls"],
            "eligible_observations": len(observations),
            "zones": len(zones),
            "zone_outdoor_buckets": bucket_count,
        },
        "configuration": {
            "min_observations": min_observations,
            "max_temp_gap_min": max_temp_gap_min,
            "max_outdoor_gap_min": max_outdoor_gap_min,
            "bucket_width_f": bucket_width_f,
            "ratio_units": "furnace on-time seconds per zone temperature degree Fahrenheit",
        },
        "data_quality": quality,
        "zones": zone_reports,
        "interpretation_guard": (
            "Runtime per degree is an observed demand-normalized ratio, not a furnace combustion "
            "efficiency rating or proof of insulation loss. Furnace on-time is attributed to every "
            "overlapping zone call, so compare zones and buckets without summing shared on-time. "
            "Confirm changes against sensor quality, setpoints, weather, and maintenance history."
        ),
    }


def _fmt_ratio(value: float | None) -> str:
    """Format a ratio for Markdown output."""
    return f"{value:.1f} s/°F" if value is not None else "—"


def render_markdown(report: dict[str, Any], file: TextIO | None = None) -> str:
    """Render a compact human-readable report."""
    lines = [
        "# Furnace runtime per degree by zone",
        "",
        f"Source: `{report['source']}`",
        (
            f"Coverage: `{report['coverage']['start']}` → `{report['coverage']['end']}`; "
            f"{report['coverage']['eligible_observations']} eligible observations"
        ),
        "",
        "Lower seconds per °F means less observed furnace on-time for each degree of zone rise.",
        "",
        "## Zone and outdoor-temperature buckets",
        "",
        "| Zone | Outdoor bucket | Observations | Median | P25–P75 | Status |",
        "|---|---|---:|---:|---:|---|",
    ]
    for zone in report["zones"]:
        if not zone["buckets"]:
            lines.append(f"| {zone['zone']} | — | 0 | — | — | {zone['status']} |")
            continue
        for bucket in zone["buckets"]:
            p25 = _fmt_ratio(bucket["p25_runtime_per_degree_s"])
            p75 = _fmt_ratio(bucket["p75_runtime_per_degree_s"])
            lines.append(
                f"| {zone['zone']} | {bucket['outdoor_temp_bucket']} | "
                f"{bucket['observation_count']} | "
                f"{_fmt_ratio(bucket['median_runtime_per_degree_s'])} | "
                f"{p25} – {p75} | {bucket['status']} |"
            )

    quality = report["data_quality"]
    lines.extend(
        [
            "",
            "## Data quality",
            "",
            f"- Call ends in range: {quality['call_ends_in_range']}",
            f"- Completed calls: {quality['completed_calls']}",
            f"- Incomplete calls: {quality['incomplete_calls']}",
            f"- Invalid-duration calls: {quality['invalid_duration_calls']}",
            f"- Calls without furnace runtime: {quality['calls_without_furnace_runtime']}",
            f"- Missing thermostat boundary: {quality['calls_missing_temperature_boundary']}",
            f"- Stale thermostat boundary: {quality['calls_with_stale_temperature_boundary']}",
            "- Non-positive temperature delta: "
            f"{quality['calls_with_non_positive_temperature_delta']}",
            f"- Missing outdoor temperature: {quality['calls_missing_outdoor_temperature']}",
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
    """Parse an ISO date for argparse."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _positive_int(value: str) -> int:
    """Parse a positive integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    """Parse a finite positive float for argparse."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive number: {value}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _resolve_range(
    days: int = DEFAULT_DAYS,
    start: date | None = None,
    end: date | None = None,
) -> tuple[date, date]:
    """Resolve an inclusive UTC date range from explicit dates or trailing days."""
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
    """Parse command-line options."""
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
        "--min-observations",
        type=_positive_int,
        default=DEFAULT_MIN_OBSERVATIONS,
        help=(
            f"Observations required for zone/bucket status=ok (default: {DEFAULT_MIN_OBSERVATIONS})"
        ),
    )
    parser.add_argument(
        "--max-temp-gap-min",
        type=_positive_float,
        default=DEFAULT_MAX_TEMP_GAP_MIN,
        help=f"Maximum thermostat boundary age (default: {DEFAULT_MAX_TEMP_GAP_MIN})",
    )
    parser.add_argument(
        "--max-outdoor-gap-min",
        type=_positive_float,
        default=DEFAULT_MAX_OUTDOOR_GAP_MIN,
        help=f"Maximum outdoor reading age (default: {DEFAULT_MAX_OUTDOOR_GAP_MIN})",
    )
    parser.add_argument(
        "--bucket-width-f",
        type=_positive_float,
        default=DEFAULT_BUCKET_WIDTH_F,
        help=f"Outdoor bucket width in °F (default: {DEFAULT_BUCKET_WIDTH_F})",
    )
    parser.add_argument("--log", default=None, help="Derived event JSONL path")
    parser.add_argument("--out", help="Optional output path; defaults to stdout")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the runtime-per-degree report CLI."""
    args = _parse_args(argv)
    try:
        start, end = _resolve_range(args.days, args.start, args.end)
        log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
        events = load_events(log_path)
        report = build_report(
            events,
            start,
            end,
            min_observations=args.min_observations,
            max_temp_gap_min=args.max_temp_gap_min,
            max_outdoor_gap_min=args.max_outdoor_gap_min,
            bucket_width_f=args.bucket_width_f,
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
            f"({report['coverage']['eligible_observations']} eligible observations)"
        )
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
