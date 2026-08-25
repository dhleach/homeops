"""Validated configuration for HomeOps insight rules.

The consumer loads one :class:`RulesConfig` instance at startup.  Keeping the
loader here avoids spreading environment-variable parsing and threshold
validation across individual rules.

Revision history:
  2026-08-25  Validate the mitigation timing settings that feed the staged
              Home Assistant helper projection, keeping automation thresholds
              out of the automation itself.
  2026-08-24  Added a dependency-free YAML-compatible loader and strict schema
              validation so rule thresholds and enabled flags are safe on the
              Pi, whose service virtualenv does not include PyYAML.
"""

from __future__ import annotations

import ast
import copy
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_RULES_CONFIG_PATH = Path(__file__).resolve().parent.parent / "rules.yaml"
_ZONE_NAMES = ("floor_1", "floor_2", "floor_3")


class RulesConfigError(ValueError):
    """Raised when the rules configuration is missing or invalid."""


class RulesConfig:
    """Immutable-at-the-boundary, validated rule settings loaded from YAML."""

    def __init__(self, rules: Mapping[str, Mapping[str, Any]], path: Path) -> None:
        self._rules = copy.deepcopy(dict(rules))
        self.path = path

    def is_enabled(self, rule_name: str) -> bool:
        """Return whether *rule_name* may emit findings or alerts."""
        return bool(self.rule(rule_name)["enabled"])

    def rule(self, rule_name: str) -> dict[str, Any]:
        """Return a defensive copy of one validated rule section."""
        try:
            return copy.deepcopy(self._rules[rule_name])
        except KeyError as exc:
            raise RulesConfigError(f"Unknown rule requested: {rule_name}") from exc

    @property
    def rules(self) -> dict[str, dict[str, Any]]:
        """Return all settings as a defensive copy, primarily for diagnostics."""
        return copy.deepcopy(self._rules)


def _strip_comment(value: str) -> str:
    """Remove an unquoted YAML comment from a scalar value."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if quote is not None:
            if char == quote and not escaped:
                quote = None
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _parse_scalar(value: str, line_number: int) -> Any:
    """Parse the scalar subset used by the checked-in rules file."""
    value = _strip_comment(value)
    if not value:
        return ""

    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None

    if value[0] in ("'", '"'):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise RulesConfigError(f"Invalid quoted scalar on line {line_number}") from exc
        if not isinstance(parsed, str):
            raise RulesConfigError(f"Expected a string scalar on line {line_number}")
        return parsed

    try:
        if any(marker in value for marker in (".", "e", "E")):
            return float(value)
        return int(value)
    except ValueError:
        return value


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    """Parse nested mappings when PyYAML is unavailable.

    The production configuration intentionally uses only YAML mappings and
    scalar values.  Supporting that small subset keeps the consumer's runtime
    dependency-free while the optional PyYAML path still accepts normal YAML
    syntax during development.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip(" "))]:
            raise RulesConfigError(f"Tabs are not valid indentation on line {line_number}")

        content = raw_line.lstrip(" ")
        indent = len(raw_line) - len(content)
        content = _strip_comment(content)
        if not content:
            continue
        if content.startswith("-") or ":" not in content:
            raise RulesConfigError(
                f"Only mapping entries are supported on line {line_number}: {raw_line!r}"
            )

        key, raw_value = content.split(":", 1)
        key = key.strip()
        if not key or key[0] in ("'", '"'):
            raise RulesConfigError(f"Invalid mapping key on line {line_number}")

        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            raise RulesConfigError(f"Duplicate key {key!r} on line {line_number}")

        raw_value = _strip_comment(raw_value).strip()
        if raw_value:
            parent[key] = _parse_scalar(raw_value, line_number)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))

    return root


def _parse_document(text: str, path: Path) -> Mapping[str, Any]:
    """Parse YAML using PyYAML when available, with a safe local fallback."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        parsed = _parse_minimal_yaml(text)
    else:
        try:
            parsed = yaml.safe_load(text)
        except Exception as exc:
            raise RulesConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(parsed, Mapping):
        raise RulesConfigError(f"{path} must contain a mapping at its root")
    return parsed


def _number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> int | float:
    """Validate and normalize one finite numeric setting."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RulesConfigError(f"{field} must be a number")
    if integer and not isinstance(value, int):
        raise RulesConfigError(f"{field} must be an integer")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise RulesConfigError(f"{field} must be finite")
    if minimum is not None and numeric < minimum:
        raise RulesConfigError(f"{field} must be >= {minimum}")
    if maximum is not None and numeric > maximum:
        raise RulesConfigError(f"{field} must be <= {maximum}")
    return int(value) if integer else numeric


def _zone_numbers(
    section: Mapping[str, Any],
    field: str,
    *,
    minimum: float,
    integer: bool,
) -> dict[str, int | float]:
    """Validate a required per-zone numeric mapping."""
    value = section.get(field)
    if not isinstance(value, Mapping):
        raise RulesConfigError(f"rules.*.{field} must be a mapping by floor")
    if set(value) != set(_ZONE_NAMES):
        missing = sorted(set(_ZONE_NAMES) - set(value))
        extra = sorted(set(value) - set(_ZONE_NAMES))
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unknown {extra}")
        raise RulesConfigError(f"rules.*.{field} has invalid floors ({'; '.join(details)})")
    return {
        zone: _number(value[zone], f"rules.*.{field}.{zone}", minimum=minimum, integer=integer)
        for zone in _ZONE_NAMES
    }


def _validate_section(rule_name: str, section: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one named rule section and return normalized values."""
    if not isinstance(section, Mapping):
        raise RulesConfigError(f"rules.{rule_name} must be a mapping")
    common = {"enabled"}
    fields = {
        "floor_runtime_anomaly": common
        | {"overrun_ratio", "lookback_days", "minimum_baseline_seconds"},
        "floor_no_response": common | {"no_response_minutes"},
        "furnace_session_anomaly": common
        | {"short_session_seconds", "long_session_fallback_seconds"},
        "furnace_short_call": common | {"threshold_seconds"},
        "floor_2_long_call": common
        | {"threshold_minutes", "escalation_count", "telegram_rate_limit_minutes"},
        "slow_to_heat": common | {"thresholds_minutes"},
        "observer_silence": common | {"threshold_minutes"},
        "heating_efficiency": common | {"min_sessions", "min_duration_seconds", "lookback_days"},
        "efficiency_degradation": common
        | {"min_weeks", "min_events_per_week", "slope_threshold_seconds_per_week"},
        "time_of_day_pattern": common | {"threshold_ratio", "min_events", "min_window_events"},
        "storm": common
        | {"storm_count", "storm_window_hours", "outdoor_drop_f", "runtime_change_ratio"},
        "mitigation": common | {"furnace_warmup_minutes", "zone_stagger_minutes"},
    }
    expected = fields[rule_name]
    unknown = set(section) - expected
    missing = expected - set(section)
    if unknown:
        raise RulesConfigError(f"rules.{rule_name} has unknown keys: {sorted(unknown)}")
    if missing:
        raise RulesConfigError(f"rules.{rule_name} is missing keys: {sorted(missing)}")
    if not isinstance(section["enabled"], bool):
        raise RulesConfigError(f"rules.{rule_name}.enabled must be true or false")

    result: dict[str, Any] = {"enabled": section["enabled"]}
    if rule_name == "floor_runtime_anomaly":
        result.update(
            overrun_ratio=_number(
                section["overrun_ratio"],
                "rules.floor_runtime_anomaly.overrun_ratio",
                minimum=1.0,
            ),
            lookback_days=_number(
                section["lookback_days"],
                "rules.floor_runtime_anomaly.lookback_days",
                minimum=1,
                integer=True,
            ),
            minimum_baseline_seconds=_number(
                section["minimum_baseline_seconds"],
                "rules.floor_runtime_anomaly.minimum_baseline_seconds",
                minimum=0,
                integer=True,
            ),
        )
    elif rule_name == "floor_no_response":
        result["no_response_minutes"] = _zone_numbers(
            section, "no_response_minutes", minimum=0.1, integer=False
        )
    elif rule_name == "furnace_session_anomaly":
        result.update(
            short_session_seconds=_number(
                section["short_session_seconds"],
                "rules.furnace_session_anomaly.short_session_seconds",
                minimum=1,
                integer=True,
            ),
            long_session_fallback_seconds=_zone_numbers(
                section, "long_session_fallback_seconds", minimum=1, integer=True
            ),
        )
    elif rule_name == "furnace_short_call":
        result["threshold_seconds"] = _number(
            section["threshold_seconds"],
            "rules.furnace_short_call.threshold_seconds",
            minimum=1,
            integer=True,
        )
    elif rule_name == "floor_2_long_call":
        result.update(
            threshold_minutes=_number(
                section["threshold_minutes"],
                "rules.floor_2_long_call.threshold_minutes",
                minimum=1,
                integer=True,
            ),
            escalation_count=_number(
                section["escalation_count"],
                "rules.floor_2_long_call.escalation_count",
                minimum=1,
                integer=True,
            ),
            telegram_rate_limit_minutes=_number(
                section["telegram_rate_limit_minutes"],
                "rules.floor_2_long_call.telegram_rate_limit_minutes",
                minimum=0,
                integer=True,
            ),
        )
    elif rule_name == "slow_to_heat":
        result["thresholds_minutes"] = _zone_numbers(
            section, "thresholds_minutes", minimum=0.1, integer=False
        )
    elif rule_name == "observer_silence":
        result["threshold_minutes"] = _number(
            section["threshold_minutes"],
            "rules.observer_silence.threshold_minutes",
            minimum=1,
            integer=True,
        )
    elif rule_name == "heating_efficiency":
        result.update(
            min_sessions=_number(
                section["min_sessions"],
                "rules.heating_efficiency.min_sessions",
                minimum=1,
                integer=True,
            ),
            min_duration_seconds=_number(
                section["min_duration_seconds"],
                "rules.heating_efficiency.min_duration_seconds",
                minimum=0,
                integer=True,
            ),
            lookback_days=_number(
                section["lookback_days"],
                "rules.heating_efficiency.lookback_days",
                minimum=1,
                integer=True,
            ),
        )
    elif rule_name == "efficiency_degradation":
        result.update(
            min_weeks=_number(
                section["min_weeks"],
                "rules.efficiency_degradation.min_weeks",
                minimum=2,
                integer=True,
            ),
            min_events_per_week=_number(
                section["min_events_per_week"],
                "rules.efficiency_degradation.min_events_per_week",
                minimum=1,
                integer=True,
            ),
            slope_threshold_seconds_per_week=_number(
                section["slope_threshold_seconds_per_week"],
                "rules.efficiency_degradation.slope_threshold_seconds_per_week",
                minimum=0,
            ),
        )
    elif rule_name == "time_of_day_pattern":
        result.update(
            threshold_ratio=_number(
                section["threshold_ratio"], "rules.time_of_day_pattern.threshold_ratio", minimum=0
            ),
            min_events=_number(
                section["min_events"],
                "rules.time_of_day_pattern.min_events",
                minimum=1,
                integer=True,
            ),
            min_window_events=_number(
                section["min_window_events"],
                "rules.time_of_day_pattern.min_window_events",
                minimum=1,
                integer=True,
            ),
        )
    elif rule_name == "storm":
        result.update(
            storm_count=_number(
                section["storm_count"], "rules.storm.storm_count", minimum=2, integer=True
            ),
            storm_window_hours=_number(
                section["storm_window_hours"], "rules.storm.storm_window_hours", minimum=0.01
            ),
            outdoor_drop_f=_number(
                section["outdoor_drop_f"], "rules.storm.outdoor_drop_f", minimum=0.1
            ),
            runtime_change_ratio=_number(
                section["runtime_change_ratio"],
                "rules.storm.runtime_change_ratio",
                minimum=0,
                maximum=1,
            ),
        )
    elif rule_name == "mitigation":
        result.update(
            furnace_warmup_minutes=_number(
                section["furnace_warmup_minutes"],
                "rules.mitigation.furnace_warmup_minutes",
                minimum=1,
                maximum=60,
                integer=True,
            ),
            zone_stagger_minutes=_number(
                section["zone_stagger_minutes"],
                "rules.mitigation.zone_stagger_minutes",
                minimum=1,
                maximum=15,
                integer=True,
            ),
        )
    return result


def _validate_document(document: Mapping[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    """Validate the complete configuration document."""
    unknown_top_level = set(document) - {"version", "rules"}
    if unknown_top_level:
        raise RulesConfigError(f"{path} has unknown top-level keys: {sorted(unknown_top_level)}")
    if document.get("version") != 1:
        raise RulesConfigError(f"{path} must declare version: 1")
    rules = document.get("rules")
    if not isinstance(rules, Mapping):
        raise RulesConfigError(f"{path} must contain a rules mapping")

    expected_rules = {
        "floor_runtime_anomaly",
        "floor_no_response",
        "furnace_session_anomaly",
        "furnace_short_call",
        "floor_2_long_call",
        "slow_to_heat",
        "observer_silence",
        "heating_efficiency",
        "efficiency_degradation",
        "time_of_day_pattern",
        "storm",
        "mitigation",
    }
    unknown = set(rules) - expected_rules
    missing = expected_rules - set(rules)
    if unknown:
        raise RulesConfigError(f"{path} has unknown rules: {sorted(unknown)}")
    if missing:
        raise RulesConfigError(f"{path} is missing rules: {sorted(missing)}")
    return {name: _validate_section(name, rules[name]) for name in sorted(expected_rules)}


def load_rules_config(path: str | os.PathLike[str] | None = None) -> RulesConfig:
    """Load and validate the rules file once for a service/CLI startup.

    ``HOMEOPS_RULES_CONFIG`` overrides the checked-in file, which makes a
    controlled test or operator rollback possible without editing source.
    Missing files and malformed values raise :class:`RulesConfigError` so the
    service cannot silently start with unknown safety thresholds.
    """
    selected = Path(path or os.environ.get("HOMEOPS_RULES_CONFIG", DEFAULT_RULES_CONFIG_PATH))
    try:
        text = selected.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesConfigError(f"Could not read rules config {selected}: {exc}") from exc
    document = _parse_document(text, selected)
    return RulesConfig(_validate_document(document, selected), selected)


__all__ = [
    "DEFAULT_RULES_CONFIG_PATH",
    "RulesConfig",
    "RulesConfigError",
    "load_rules_config",
    "_parse_minimal_yaml",
]
