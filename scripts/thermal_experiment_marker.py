#!/usr/bin/env python3
"""Record data-only HomeOps thermal experiments from plain-language messages.

The marker log is an append-only sidecar to the observer and consumer logs.
It records operator intent and provenance; it never calls Home Assistant or
changes a thermostat.  The offline thermal exporter can join these markers to
the observed floor sessions after the run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MARKER_SCHEMA = "homeops.thermal.experiment_marker.v1"
DEFAULT_MARKER_LOG = Path("state/experiments/markers.jsonl")
DEFAULT_DURATION_S = 30 * 60
MAX_DURATION_S = 24 * 60 * 60
LOCAL_TIMEZONE = ZoneInfo("America/New_York")
ZONES = ("floor_1", "floor_2", "floor_3")
ZONE_NUMBERS = {zone: zone.removeprefix("floor_") for zone in ZONES}

_WORD_NUMBERS = {
    "first": "1",
    "one": "1",
    "second": "2",
    "two": "2",
    "third": "3",
    "three": "3",
}
_DURATION_PATTERN = re.compile(
    r"\b(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b"
)
_CLOCK_PATTERN = re.compile(
    r"(?:around|about|at|@)\s*(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?\s*(?P<meridiem>a\.?m\.?|p\.?m\.?)?",
    re.IGNORECASE,
)
_ISO_PATTERN = re.compile(
    r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})\b"
)


class MarkerError(ValueError):
    """Raised when a marker cannot be safely interpreted or persisted."""


@dataclass(frozen=True)
class ExperimentConfiguration:
    """Stable checklist identity for one mode and active-floor combination."""

    configuration_id: str
    name: str
    mode: str
    active_zones: tuple[str, ...]
    suppressed_zones: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "configuration_id": self.configuration_id,
            "name": self.name,
            "mode": self.mode,
            "active_zones": list(self.active_zones),
            "suppressed_zones": list(self.suppressed_zones),
        }


@dataclass(frozen=True)
class ParsedCommand:
    """Normalized interpretation of one operator message."""

    action: str
    mode: str | None
    active_zones: tuple[str, ...]
    duration_s: int | None
    duration_defaulted: bool
    target_f: float | None
    start_ts: datetime | None
    confidence: str
    source_type: str
    abort_reason: str | None
    raw_text: str


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise MarkerError(f"invalid ISO timestamp: {value!r}") from exc
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(timestamp: datetime | None) -> str | None:
    return timestamp.isoformat() if timestamp is not None else None


def _normalize_text(text: str) -> str:
    normalized = text.casefold().replace("–", "-").replace("—", "-")
    for word, number in _WORD_NUMBERS.items():
        normalized = re.sub(rf"\b{word}\b", number, normalized)
    return normalized


def _extract_zones(text: str) -> tuple[str, ...]:
    """Extract explicit floor identifiers without mistaking durations for floors."""

    zones: set[str] = set()

    for match in re.finditer(r"\bfloors?\s*[-_ ]?\s*([123])(?:st|nd|rd)?\b", text):
        zones.add(f"floor_{match.group(1)}")
    for match in re.finditer(r"\b([123])(?:st|nd|rd)?\s+floors?\b", text):
        zones.add(f"floor_{match.group(1)}")
    for match in re.finditer(r"\bf([123])\b", text):
        zones.add(f"floor_{match.group(1)}")

    # A plural phrase such as "floors 1 and 3" has the second floor number
    # after the first match. Stop before the mode/test/duration clause.
    for match in re.finditer(r"\bfloors?\b", text):
        tail = text[match.end() : match.end() + 36]
        tail = re.split(
            r"\b(?:cool(?:ing)?|heat(?:ing)?|test|experiment|for|lasting|"
            r"duration|target|setpoint|degree|degrees?|minute|minutes?|"
            r"min|mins?|hour|hours?|hr|hrs?)\b",
            tail,
            maxsplit=1,
        )[0]
        for number in re.findall(r"(?<!\d)([123])(?:st|nd|rd)?(?!\d)", tail):
            zones.add(f"floor_{number}")

    if re.search(r"\ball\s+(?:three|3)?\s*floors?\b", text):
        zones.update(ZONES)
    return tuple(sorted(zones, key=lambda zone: ZONES.index(zone)))


def _extract_mode(text: str) -> str | None:
    cooling = re.search(r"\bcool(?:ing)?\b|\bair\s*conditioning\b|\ba/?c\b", text)
    heating = re.search(r"\bheat(?:ing)?\b", text)
    if cooling and heating:
        raise MarkerError("message names both heating and cooling")
    if cooling:
        return "cool"
    if heating:
        return "heat"
    return None


def _extract_duration(text: str) -> int | None:
    if re.search(r"\bhalf\s+(?:an?\s+)?hour\b", text):
        return 30 * 60
    match = _DURATION_PATTERN.search(text)
    if not match:
        return None
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    multiplier = 1
    if unit.startswith("m"):
        multiplier = 60
    elif unit.startswith("h"):
        multiplier = 60 * 60
    duration_s = round(value * multiplier)
    if duration_s < 1 or duration_s > MAX_DURATION_S:
        raise MarkerError("duration must be between 1 second and 24 hours")
    return duration_s


def _extract_target(text: str) -> float | None:
    match = re.search(
        r"\b(?:target|setpoint|set\s+point|thermostat)\s*"
        r"(?:to|at|=)?\s*(?P<value>\d{2}(?:\.\d+)?)\b",
        text,
    )
    if not match:
        return None
    return float(match.group("value"))


def _is_retroactive(text: str) -> bool:
    has_past_reference = re.search(
        r"\b(?:last\s+night|yesterday|earlier|previously|the\s+other\s+night)\b",
        text,
    )
    has_retro_verb = re.search(r"\b(?:ran|did|completed|finished|performed|retroactive)\b", text)
    return bool(has_past_reference and has_retro_verb) or "retroactive" in text


def _action(text: str) -> str:
    if (
        re.search(r"\b(?:abort|stop|cancel|interrupt)\b", text)
        or re.search(r"\b(?:kids?|children|family)\b.{0,24}\b(?:cold|uncomfortable)\b", text)
        or re.search(r"\btoo\s+cold\b", text)
    ):
        return "abort"
    if _is_retroactive(text):
        return "retroactive"
    if re.search(r"\b(?:end(?:ed)?|finish(?:ed)?|done|complete(?:d)?)\b", text):
        return "end"
    if re.search(r"\b(?:start(?:ing)?|begin(?:ning)?|run(?:ning)?)\b", text):
        return "start"
    raise MarkerError("message does not clearly start, end, abort, or describe a past test")


def _retroactive_start(
    text: str,
    received_at: datetime,
    start_at_override: str | None,
) -> datetime:
    if start_at_override:
        parsed = _parse_timestamp(start_at_override)
        if parsed is not None:
            return parsed

    iso_match = _ISO_PATTERN.search(text)
    if iso_match:
        parsed = _parse_timestamp(iso_match.group(0))
        if parsed is not None:
            return parsed

    clock_match = None
    for candidate in _CLOCK_PATTERN.finditer(text):
        hour = int(candidate.group("hour"))
        if hour <= 23:
            clock_match = candidate
            break
    if clock_match is None:
        raise MarkerError("retroactive test needs an approximate clock time")

    hour = int(clock_match.group("hour"))
    minute = int(clock_match.group("minute") or 0)
    meridiem = (clock_match.group("meridiem") or "").replace(".", "")
    if meridiem:
        if hour < 1 or hour > 12:
            raise MarkerError("12-hour retroactive time has an invalid hour")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif hour > 23:
        raise MarkerError("retroactive time has an invalid hour")
    elif hour <= 12 and re.search(r"\b(?:night|evening)\b", text):
        if hour != 12:
            hour += 12

    local_received = received_at.astimezone(LOCAL_TIMEZONE)
    local_date: date = local_received.date()
    if re.search(r"\b(?:yesterday|last\s+night|the\s+other\s+night)\b", text):
        local_date -= timedelta(days=1)
    local_start = datetime.combine(local_date, time(hour, minute), tzinfo=LOCAL_TIMEZONE)
    if not re.search(r"\b(?:yesterday|last\s+night|the\s+other\s+night|today)\b", text):
        if local_start > local_received + timedelta(minutes=5):
            local_start -= timedelta(days=1)
    return local_start.astimezone(UTC)


def parse_command(
    text: str,
    *,
    received_at: datetime | None = None,
    start_at: str | None = None,
) -> ParsedCommand:
    """Interpret the deliberately small natural-language marker vocabulary."""

    if not isinstance(text, str) or not text.strip():
        raise MarkerError("message must be non-empty text")
    if len(text) > 4_000:
        raise MarkerError("message is too long to retain as marker provenance")
    received = received_at or datetime.now(UTC)
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    received = received.astimezone(UTC)

    normalized = _normalize_text(text)
    action = _action(normalized)
    mode = _extract_mode(normalized)
    active_zones = _extract_zones(normalized)
    duration = _extract_duration(normalized)
    target_f = _extract_target(normalized)

    if action in {"start", "retroactive"}:
        if mode is None:
            raise MarkerError("a start message must name heating or cooling")
        if not active_zones:
            raise MarkerError("a start message must name one or more floors")
    duration_defaulted = action == "start" and duration is None
    if duration_defaulted:
        duration = DEFAULT_DURATION_S

    if action == "retroactive":
        start_timestamp = _retroactive_start(normalized, received, start_at)
        confidence = "approximate"
        source_type = "retroactive"
    elif action == "start":
        start_timestamp = received
        confidence = "exact"
        source_type = "live"
    else:
        start_timestamp = None
        confidence = "exact"
        source_type = "lifecycle"

    abort_reason = None
    if action == "abort":
        if re.search(r"\b(?:cold|uncomfortable)\b", normalized):
            abort_reason = "household comfort"
        else:
            reason_match = re.search(r"\b(?:because|reason(?:\s+is)?)\b\s*(.+)$", normalized)
            abort_reason = reason_match.group(1).strip() if reason_match else "operator abort"

    return ParsedCommand(
        action=action,
        mode=mode,
        active_zones=active_zones,
        duration_s=duration,
        duration_defaulted=duration_defaulted,
        target_f=target_f,
        start_ts=start_timestamp,
        confidence=confidence,
        source_type=source_type,
        abort_reason=abort_reason,
        raw_text=text.strip(),
    )


def configuration_for(mode: str, active_zones: Iterable[str]) -> ExperimentConfiguration:
    """Return a stable configuration identity for any valid floor subset."""

    if mode not in {"heat", "cool"}:
        raise MarkerError(f"unsupported HVAC mode: {mode!r}")
    raw_active = tuple(set(active_zones))
    if not raw_active or any(zone not in ZONES for zone in raw_active):
        raise MarkerError("active zones must be a non-empty subset of floor_1..floor_3")
    active = tuple(sorted(raw_active, key=lambda zone: ZONES.index(zone)))
    numbers = "".join(ZONE_NUMBERS[zone] for zone in active)
    if len(active) == 1:
        suffix = f"s1-f{numbers}"
        scope = f"Floor {numbers} only"
    elif len(active) == 2:
        suffix = f"p{numbers}"
        scope = f"Floors {numbers[0]}+{numbers[1]}"
    else:
        suffix = "all-f123"
        scope = "all floors"
    mode_name = "Cooling" if mode == "cool" else "Heating"
    suppressed = tuple(zone for zone in ZONES if zone not in active)
    return ExperimentConfiguration(
        configuration_id=f"{mode}-{suffix}",
        name=f"{mode_name} — {scope}",
        mode=mode,
        active_zones=active,
        suppressed_zones=suppressed,
    )


def canonical_configurations() -> tuple[ExperimentConfiguration, ...]:
    """Return the six first-suite cooling configurations in run order."""

    return tuple(
        configuration_for("cool", active)
        for active in (
            ("floor_1",),
            ("floor_2",),
            ("floor_3",),
            ("floor_1", "floor_2"),
            ("floor_1", "floor_3"),
            ("floor_2", "floor_3"),
        )
    )


def _event_id(action: str, data: dict[str, Any], source_message_id: str | None) -> str:
    dedupe_key = source_message_id or json.dumps(
        {"action": action, "data": data}, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]
    return f"thermal-marker-{digest}"


def _experiment_id(
    configuration: ExperimentConfiguration,
    start_timestamp: datetime,
    raw_text: str,
    source_message_id: str | None,
) -> str:
    seed = source_message_id or raw_text
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    stamp = start_timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{configuration.configuration_id}-{stamp}-{digest}"


def _record(
    action: str,
    data: dict[str, Any],
    *,
    received_at: datetime,
    source_message_id: str | None,
) -> dict[str, Any]:
    payload = dict(data)
    payload["source_message_id"] = source_message_id
    return {
        "schema": MARKER_SCHEMA,
        "source": "telegram.experiment_marker",
        "ts": received_at.isoformat(),
        "event_id": _event_id(action, payload, source_message_id),
        "data": payload,
    }


def validate_marker_record(record: dict[str, Any]) -> None:
    """Fail closed on malformed marker records before they enter the log."""

    if not isinstance(record, dict) or record.get("schema") != MARKER_SCHEMA:
        raise MarkerError("marker record has an invalid schema")
    if not isinstance(record.get("source"), str) or not record["source"].strip():
        raise MarkerError("marker source is required")
    if not isinstance(record.get("event_id"), str) or not record["event_id"].strip():
        raise MarkerError("marker event_id is required")
    if _parse_timestamp(record.get("ts")) is None:
        raise MarkerError("marker ts is required and must be ISO timestamp")
    data = record.get("data")
    if not isinstance(data, dict):
        raise MarkerError("marker record data must be an object")
    action = data.get("action")
    status = data.get("status")
    if action not in {"start", "end", "abort", "retroactive"}:
        raise MarkerError("marker action is invalid")
    expected_status = {
        "start": "active",
        "end": "completed",
        "abort": "aborted",
    }
    if action in expected_status and status != expected_status[action]:
        raise MarkerError(f"{action} marker must have status {expected_status[action]!r}")
    if action == "retroactive" and status not in {"completed", "needs_review"}:
        raise MarkerError("retroactive marker has an invalid status")
    if not isinstance(data.get("experiment_id"), str) or not data["experiment_id"].strip():
        raise MarkerError("marker experiment_id is required")
    if data.get("mode") not in {"heat", "cool"}:
        raise MarkerError("marker mode must be heat or cool")
    active = data.get("active_zones")
    suppressed = data.get("suppressed_zones")
    if (
        not isinstance(active, list)
        or not active
        or any(zone not in ZONES for zone in active)
        or len(set(active)) != len(active)
        or not isinstance(suppressed, list)
        or any(zone not in ZONES for zone in suppressed)
        or set(active) & set(suppressed)
        or set(active) | set(suppressed) != set(ZONES)
    ):
        raise MarkerError("marker active/suppressed zones are invalid")
    if _parse_timestamp(data.get("start_ts")) is None:
        raise MarkerError("marker start_ts is required and must be ISO timestamp")
    end_ts = data.get("end_ts")
    parsed_start = _parse_timestamp(data.get("start_ts"))
    parsed_end = _parse_timestamp(end_ts) if end_ts is not None else None
    if end_ts is not None and parsed_end is None:
        raise MarkerError("marker end_ts must be an ISO timestamp or null")
    if parsed_end is not None and parsed_start is not None and parsed_end < parsed_start:
        raise MarkerError("marker end_ts cannot precede start_ts")
    duration = data.get("planned_duration_s")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 1
    ):
        raise MarkerError("marker planned_duration_s must be positive or null")
    if data.get("confidence") not in {"exact", "approximate"}:
        raise MarkerError("marker confidence must be exact or approximate")
    if not isinstance(data.get("raw_text"), str) or not data["raw_text"].strip():
        raise MarkerError("marker raw_text is required")
    if action == "retroactive" and status == "completed" and end_ts is None:
        raise MarkerError("completed retroactive markers need an end_ts")


def _read_handle(handle: Any) -> list[dict[str, Any]]:
    handle.seek(0)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MarkerError(f"invalid marker JSON on line {line_number}") from exc
        validate_marker_record(record)
        records.append(record)
    return records


class MarkerStore:
    """Append-only JSONL store with file locking and source-message deduplication."""

    def __init__(self, path: str | Path = DEFAULT_MARKER_LOG):
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _read_handle(handle)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def find_by_source_message_id(self, source_message_id: str | None) -> dict[str, Any] | None:
        if not source_message_id:
            return None
        return next(
            (
                record
                for record in self.read()
                if record.get("data", {}).get("source_message_id") == source_message_id
            ),
            None,
        )

    def append(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        validate_marker_record(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                existing = _read_handle(handle)
                source_message_id = record["data"].get("source_message_id")
                for prior in existing:
                    if prior.get("event_id") == record.get("event_id"):
                        return prior, True
                    if (
                        source_message_id
                        and prior.get("data", {}).get("source_message_id") == source_message_id
                    ):
                        return prior, True
                handle.seek(0, 2)
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                return record, False
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _intervention(configuration: ExperimentConfiguration, duration_s: int | None) -> dict[str, Any]:
    return {
        "source": "operator",
        "mode": configuration.mode,
        "active_zones": list(configuration.active_zones),
        "suppressed_zones": list(configuration.suppressed_zones),
        "planned_duration_s": duration_s,
    }


def reconstruct_runs(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rebuild current run state from append-only lifecycle records."""

    runs: dict[str, dict[str, Any]] = {}
    for record in records:
        data = record.get("data", {})
        experiment_id = data.get("experiment_id")
        action = data.get("action")
        if not isinstance(experiment_id, str):
            continue
        if action in {"start", "retroactive"}:
            run = dict(data)
            run["marker_events"] = [record]
            runs[experiment_id] = run
            continue
        if action not in {"end", "abort"} or experiment_id not in runs:
            continue
        run = runs[experiment_id]
        run["status"] = "completed" if action == "end" else "aborted"
        run["end_ts"] = data.get("end_ts")
        if action == "abort":
            run["abort_reason"] = data.get("abort_reason")
        run["received_at"] = data.get("received_at")
        run["marker_events"].append(record)
    return runs


def _start_data(
    parsed: ParsedCommand,
    configuration: ExperimentConfiguration,
    *,
    received_at: datetime,
    source_message_id: str | None,
) -> dict[str, Any]:
    if parsed.start_ts is None:
        raise MarkerError("start marker has no start timestamp")
    experiment_id = _experiment_id(
        configuration, parsed.start_ts, parsed.raw_text, source_message_id
    )
    return {
        "action": parsed.action,
        "status": "active" if parsed.action == "start" else "needs_review",
        "experiment_id": experiment_id,
        "configuration_id": configuration.configuration_id,
        "experiment_name": configuration.name,
        "test_id": configuration.configuration_id,
        "operation_type": "controlled_thermal_experiment",
        "mode": configuration.mode,
        "active_zones": list(configuration.active_zones),
        "suppressed_zones": list(configuration.suppressed_zones),
        "planned_duration_s": parsed.duration_s,
        "target_f": parsed.target_f,
        "start_ts": parsed.start_ts.isoformat(),
        "end_ts": None,
        "received_at": received_at.isoformat(),
        "confidence": parsed.confidence,
        "source_type": parsed.source_type,
        "duration_defaulted": parsed.duration_defaulted,
        "abort_reason": None,
        "intervention": _intervention(configuration, parsed.duration_s),
        "raw_text": parsed.raw_text,
        "source_message_id": source_message_id,
    }


def record_message(
    message: str,
    *,
    store: MarkerStore,
    source_message_id: str | None = None,
    received_at: str | datetime | None = None,
    start_at: str | None = None,
) -> dict[str, Any]:
    """Parse and append one message, resolving end/abort against active runs."""

    parsed_received = (
        _parse_timestamp(received_at) if received_at is not None else datetime.now(UTC)
    )
    if parsed_received is None:
        raise MarkerError("received_at is required when supplied")
    normalized_source_id = str(source_message_id).strip() if source_message_id else None
    prior = store.find_by_source_message_id(normalized_source_id)
    if prior is not None:
        return {"duplicate": True, "record": prior}

    parsed = parse_command(message, received_at=parsed_received, start_at=start_at)
    records = store.read()
    if parsed.action in {"start", "retroactive"}:
        if parsed.mode is None:
            raise MarkerError("experiment mode is required")
        configuration = configuration_for(parsed.mode, parsed.active_zones)
        data = _start_data(
            parsed,
            configuration,
            received_at=parsed_received,
            source_message_id=normalized_source_id,
        )
        if parsed.action == "retroactive":
            if parsed.duration_s is not None and parsed.start_ts is not None:
                data["end_ts"] = (
                    parsed.start_ts + timedelta(seconds=parsed.duration_s)
                ).isoformat()
                data["status"] = "completed"
        record = _record(
            parsed.action,
            data,
            received_at=parsed_received,
            source_message_id=normalized_source_id,
        )
        saved, duplicate = store.append(record)
        return {"duplicate": duplicate, "record": saved}

    runs = reconstruct_runs(records)
    candidates = [
        run
        for run in runs.values()
        if run.get("status") == "active"
        and (parsed.mode is None or run.get("mode") == parsed.mode)
        and (
            not parsed.active_zones or set(run.get("active_zones", [])) == set(parsed.active_zones)
        )
    ]
    if not candidates:
        raise MarkerError("no matching active experiment was found")
    if len(candidates) > 1:
        ids = ", ".join(sorted(run["experiment_id"] for run in candidates))
        raise MarkerError(f"multiple active experiments match; specify the run ({ids})")

    run = candidates[0]
    action = parsed.action
    data = {key: value for key, value in run.items() if key != "marker_events"}
    data.update(
        {
            "action": action,
            "status": "completed" if action == "end" else "aborted",
            "end_ts": parsed_received.isoformat(),
            "received_at": parsed_received.isoformat(),
            "confidence": run.get("confidence", "exact"),
            "source_type": "lifecycle",
            "raw_text": parsed.raw_text,
            "source_message_id": normalized_source_id,
        }
    )
    if action == "abort":
        data["abort_reason"] = parsed.abort_reason or "operator abort"
    record = _record(
        action,
        data,
        received_at=parsed_received,
        source_message_id=normalized_source_id,
    )
    saved, duplicate = store.append(record)
    return {"duplicate": duplicate, "record": saved}


def checklist_status(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return derived completion/repeat counts for the canonical six tests."""

    runs = list(reconstruct_runs(records).values())
    result = []
    for configuration in canonical_configurations():
        matching = [
            run for run in runs if run.get("configuration_id") == configuration.configuration_id
        ]
        completed = sum(run.get("status") == "completed" for run in matching)
        active = sum(run.get("status") == "active" for run in matching)
        aborted = sum(run.get("status") == "aborted" for run in matching)
        result.append(
            {
                **configuration.to_dict(),
                "checked": completed > 0,
                "status": (
                    "completed"
                    if completed
                    else "active"
                    if active
                    else "aborted"
                    if aborted
                    else "not_started"
                ),
                "run_count": len(matching),
                "completed_runs": completed,
                "active_runs": active,
                "aborted_runs": aborted,
            }
        )
    return result


def render_checklist(status: list[dict[str, Any]]) -> str:
    """Render a human-readable checklist without mutating the canonical log."""

    lines = ["Canonical cooling experiment checklist", ""]
    for item in status:
        marker = "x" if item["checked"] else " "
        lines.append(
            f"[{marker}] {item['configuration_id']} — {item['name']} "
            f"(completed {item['completed_runs']}, active {item['active_runs']}, "
            f"aborted {item['aborted_runs']})"
        )
    checked = sum(item["checked"] for item in status)
    lines.extend(["", f"Coverage: {checked}/{len(status)} configurations have a completed run."])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="record a natural-language marker")
    record.add_argument("--message", required=True, help="plain-language operator message")
    record.add_argument("--store", type=Path, default=DEFAULT_MARKER_LOG)
    record.add_argument("--source-message-id")
    record.add_argument("--received-at", help="ISO timestamp from the Telegram message")
    record.add_argument(
        "--start-at",
        help="normalized ISO start for a retroactive declaration (optional)",
    )

    checklist = subparsers.add_parser(
        "checklist", aliases=["status"], help="render the canonical test checklist"
    )
    checklist.add_argument("--store", type=Path, default=DEFAULT_MARKER_LOG)
    checklist.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = MarkerStore(args.store)
        if args.command == "record":
            result = record_message(
                args.message,
                store=store,
                source_message_id=args.source_message_id,
                received_at=args.received_at,
                start_at=args.start_at,
            )
            print(json.dumps(result, sort_keys=True))
        else:
            status = checklist_status(store.read())
            if args.format == "json":
                print(json.dumps(status, indent=2, sort_keys=True))
            else:
                print(render_checklist(status))
    except (MarkerError, OSError) as exc:
        print(f"marker failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
