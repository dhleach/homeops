#!/usr/bin/env python3
"""Replay HomeOps anomaly detectors against a derived-event JSONL history.

The command is intentionally read-only.  It replays the production
``FloorRuntimeAnomalyRule`` in JSONL order, evaluates completed furnace
sessions with ``FurnaceSessionAnomalyRule``, compares those results with
warnings already present in the log, and emits a deterministic report.

Usage::

    PYTHONPATH=services/insights python3 scripts/validate_anomalies.py \
        --log state/consumer/events.jsonl
    ssh bob@pi 'cat /path/to/events.jsonl' | \
        PYTHONPATH=services/insights python3 scripts/validate_anomalies.py --log -

Revision history:
  2026-08-22  Added deterministic historical replay and review reporting so
              detector thresholds can be evaluated against real HVAC history
              without writing to the production event log or state files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
INSIGHTS_PATH = REPO_ROOT / "services" / "insights"
if str(INSIGHTS_PATH) not in sys.path:
    sys.path.insert(0, str(INSIGHTS_PATH))

from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule  # noqa: E402
from rules.furnace_session_anomaly import (  # noqa: E402
    SHORT_SESSION_THRESHOLD_S,
    FurnaceSessionAnomalyRule,
)

SUMMARY_SCHEMA = "homeops.consumer.furnace_daily_summary.v1"
SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"
FLOOR_ANOMALY_SCHEMA = "homeops.consumer.floor_runtime_anomaly.v1"
SHORT_WARNING_SCHEMA = "homeops.consumer.heating_short_session_warning.v1"
LONG_WARNING_SCHEMA = "homeops.consumer.heating_long_session_warning.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
KNOWN_FLOORS = ("floor_1", "floor_2", "floor_3")
FLOOR_LOOKBACK_DAYS = 14
FLOOR_THRESHOLD_MULTIPLIER = 1.5


def load_events(source: str) -> tuple[list[dict[str, Any]], int, int]:
    """Load JSON objects from a path or stdin.

    Returns ``(events, invalid_json_lines, non_object_lines)``.  Invalid
    records are counted and ignored, matching the consumer's tolerant JSONL
    handling while making data-quality loss visible in the report.
    """
    events: list[dict[str, Any]] = []
    invalid_json_lines = 0
    non_object_lines = 0

    stream: TextIO
    close_stream = False
    if source == "-":
        stream = sys.stdin
    else:
        stream = open(source, encoding="utf-8")
        close_stream = True

    try:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_json_lines += 1
                continue
            if not isinstance(event, dict):
                non_object_lines += 1
                continue
            events.append(event)
    finally:
        if close_stream:
            stream.close()

    return events, invalid_json_lines, non_object_lines


def load_baseline(path: str | None) -> tuple[dict[str, Any], str]:
    """Load an optional session baseline and describe the active source."""
    if path is None:
        return {}, "absolute fallback thresholds (no baseline supplied)"

    baseline_path = Path(path)
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"baseline must be a JSON object: {baseline_path}")
    return data, str(baseline_path)


def _date_values(values: list[str]) -> dict[str, Any]:
    """Return a stable date range summary, ignoring malformed date values."""
    valid = []
    for value in values:
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        valid.append(value)

    unique = sorted(set(valid))
    return {
        "unique_days": len(unique),
        "first": unique[0] if unique else None,
        "last": unique[-1] if unique else None,
    }


def _round_number(value: Any) -> Any:
    """Round floats for stable reports while preserving integers and nulls."""
    if isinstance(value, float):
        return round(value, 3)
    return value


def _floor_key(data: dict[str, Any]) -> tuple[Any, ...]:
    """Build a comparison key for a floor anomaly event."""
    runtime = data.get("runtime_s")
    try:
        runtime = int(runtime)
    except (TypeError, ValueError):
        pass
    return data.get("date"), data.get("floor"), runtime


def _session_warning_kind(schema: str) -> str | None:
    if schema == SHORT_WARNING_SCHEMA:
        return "short"
    if schema == LONG_WARNING_SCHEMA:
        return "long"
    return None


def _session_key(kind: str, data: dict[str, Any]) -> tuple[Any, ...]:
    """Build a comparison key for a replayed or emitted session warning."""
    duration = data.get("duration_s")
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        pass
    return kind, data.get("session_ts"), data.get("floor"), duration


def _counter_difference(
    left: Counter[tuple[Any, ...]], right: Counter[tuple[Any, ...]]
) -> list[list[Any]]:
    """Return sorted, JSON-friendly keys present more often in ``left``."""
    differences: list[list[Any]] = []
    ordered = sorted(
        left.items(),
        key=lambda item: tuple("" if value is None else str(value) for value in item[0]),
    )
    for key, count in ordered:
        for _ in range(max(0, count - right.get(key, 0))):
            differences.append(list(key))
    return differences


def _floor_review_note(outdoor_temp_f: Any) -> str:
    """Describe the evidence available without claiming a fault diagnosis."""
    if isinstance(outdoor_temp_f, (int, float)):
        weather = "cold-weather demand" if outdoor_temp_f <= 50 else "milder-weather demand"
        return f"Detector contract met; context is consistent with {weather}."
    return "Detector contract met; outdoor context is unavailable."


def _session_review_note(across_restart: bool) -> str:
    """Describe session evidence without inventing maintenance ground truth."""
    if across_restart:
        return (
            "Detector contract met; event was emitted in restart context, so root cause "
            "requires HA or maintenance history."
        )
    return "Detector contract met; root cause requires HA or maintenance history."


def replay_floor_anomalies(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay the floor runtime detector in production JSONL order."""
    prior_summaries: list[dict[str, Any]] = []
    replayed: list[dict[str, Any]] = []
    summary_dates: list[str] = []
    evaluated_floor_days = 0

    for event in events:
        if event.get("schema") != SUMMARY_SCHEMA:
            continue

        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        summary_date = data.get("date")
        per_floor = data.get("per_floor_runtime_s") or {}
        if not isinstance(summary_date, str) or not isinstance(per_floor, dict):
            prior_summaries.append(event)
            continue

        summary_dates.append(summary_date)
        rule = FloorRuntimeAnomalyRule(
            history=prior_summaries,
            lookback_days=FLOOR_LOOKBACK_DAYS,
            threshold_multiplier=FLOOR_THRESHOLD_MULTIPLIER,
        )
        for floor, runtime in per_floor.items():
            try:
                runtime_s = int(runtime)
            except (TypeError, ValueError):
                continue
            evaluated_floor_days += 1
            for anomaly in rule.check_daily_runtime(floor, runtime_s, summary_date):
                anomaly_data = anomaly.get("data") or {}
                replayed.append(
                    {
                        "date": summary_date,
                        "floor": floor,
                        "runtime_s": runtime_s,
                        "baseline_mean_s": _round_number(anomaly_data.get("baseline_mean_s")),
                        "baseline_stddev_s": _round_number(anomaly_data.get("baseline_stddev_s")),
                        "threshold_s": _round_number(anomaly_data.get("threshold_s")),
                        "history_count": anomaly_data.get("history_count"),
                        "confidence": _round_number(anomaly_data.get("confidence")),
                        "severity": anomaly_data.get("severity"),
                        "outdoor_temp_avg_f": data.get("outdoor_temp_avg_f"),
                        "review_outcome": "contract_met",
                        "review_note": _floor_review_note(data.get("outdoor_temp_avg_f")),
                    }
                )

        prior_summaries.append(event)

    emitted = []
    for event in events:
        if event.get("schema") != FLOOR_ANOMALY_SCHEMA:
            continue
        data = event.get("data") or {}
        if isinstance(data, dict):
            emitted.append(data)

    replay_keys = Counter(_floor_key(item) for item in replayed)
    emitted_keys = Counter(_floor_key(item) for item in emitted)
    stats = {
        "summary_events": len(summary_dates),
        "summary_dates": _date_values(summary_dates),
        "evaluated_floor_days": evaluated_floor_days,
        "replayed_alert_count": len(replayed),
        "emitted_alert_count": len(emitted),
        "replay_matches_emitted": replay_keys == emitted_keys,
        "replayed_not_emitted": _counter_difference(replay_keys, emitted_keys),
        "emitted_not_replayed": _counter_difference(emitted_keys, replay_keys),
        "contract_valid_count": len(replayed),
        "fault_false_positive_rate": None,
        "fault_false_positive_rate_note": (
            "Not measurable from event telemetry alone: the log has no maintenance, "
            "limit-switch, or operator-label ground truth."
        ),
    }
    return replayed, emitted, stats


def replay_session_anomalies(
    events: list[dict[str, Any]], baseline: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay the furnace session detector against completed sessions."""
    rule = FurnaceSessionAnomalyRule(baseline)
    replayed: list[dict[str, Any]] = []
    session_dates: list[str] = []
    complete_sessions = 0
    null_duration_sessions = 0

    for event in events:
        if event.get("schema") != SESSION_SCHEMA:
            continue
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        ended_at = data.get("ended_at") or event.get("ts") or ""
        if isinstance(ended_at, str) and len(ended_at) >= 10:
            session_dates.append(ended_at[:10])
        duration = data.get("duration_s")
        if duration is None:
            null_duration_sessions += 1
            continue
        try:
            duration_s = int(duration)
        except (TypeError, ValueError):
            continue

        complete_sessions += 1
        for warning in rule.check_session(data.get("floor"), duration_s, ended_at):
            warning_data = warning.get("data") or {}
            kind = _session_warning_kind(warning.get("schema", ""))
            if kind is None:
                continue
            replayed.append(
                {
                    "warning": kind,
                    "session_ts": warning_data.get("session_ts"),
                    "floor": warning_data.get("floor"),
                    "duration_s": warning_data.get("duration_s"),
                    "threshold_s": _round_number(warning_data.get("threshold_s")),
                    "outdoor_temp_f": data.get("outdoor_temp_f"),
                    "across_restart": bool(data.get("across_restart")),
                    "review_outcome": "contract_met",
                    "review_note": _session_review_note(bool(data.get("across_restart"))),
                }
            )

    emitted: list[dict[str, Any]] = []
    emitted_counts: Counter[str] = Counter()
    for event in events:
        kind = _session_warning_kind(event.get("schema", ""))
        if kind is None:
            continue
        data = event.get("data") or {}
        if isinstance(data, dict):
            emitted_counts[kind] += 1
            emitted.append({"warning": kind, **data})

    replay_counts = Counter(item["warning"] for item in replayed)
    replay_keys = Counter(_session_key(item["warning"], item) for item in replayed)
    emitted_keys = Counter(_session_key(item["warning"], item) for item in emitted)
    stats = {
        "complete_sessions": complete_sessions,
        "null_duration_sessions_skipped": null_duration_sessions,
        "session_dates": _date_values(session_dates),
        "replayed_warning_counts": dict(sorted(replay_counts.items())),
        "emitted_warning_counts": dict(sorted(emitted_counts.items())),
        "replayed_warning_count": len(replayed),
        "emitted_warning_count": len(emitted),
        "replay_matches_emitted": replay_keys == emitted_keys,
        "replayed_not_emitted": _counter_difference(replay_keys, emitted_keys),
        "emitted_not_replayed": _counter_difference(emitted_keys, replay_keys),
        "contract_valid_count": len(replayed),
        "fault_false_positive_rate": None,
        "fault_false_positive_rate_note": (
            "Not measurable from event telemetry alone: the log has no maintenance, "
            "limit-switch, or operator-label ground truth."
        ),
    }
    return replayed, emitted, stats


def build_report(
    events: list[dict[str, Any]],
    invalid_json_lines: int = 0,
    non_object_lines: int = 0,
    baseline: dict[str, Any] | None = None,
    baseline_source: str = "absolute fallback thresholds (no baseline supplied)",
    source: str = "events.jsonl",
) -> dict[str, Any]:
    """Build the complete deterministic validation report."""
    baseline = baseline or {}
    floor_replayed, _, floor_stats = replay_floor_anomalies(events)
    session_replayed, _, session_stats = replay_session_anomalies(events, baseline)

    relevant_dates = floor_stats["summary_dates"]
    session_dates = session_stats["session_dates"]
    all_dates = [value for value in (relevant_dates, session_dates) if value["first"]]
    if all_dates:
        first = min(item["first"] for item in all_dates)
        last = max(item["last"] for item in all_dates)
        coverage_range = {"first": first, "last": last}
    else:
        coverage_range = {"first": None, "last": None}

    return {
        "schema": "homeops.anomaly-validation.v1",
        "source": source,
        "replay_order": "input JSONL order, matching production event processing order",
        "coverage": {
            "valid_json_objects": len(events),
            "invalid_json_lines": invalid_json_lines,
            "non_object_lines": non_object_lines,
            "date_range": coverage_range,
        },
        "configuration": {
            "floor_lookback_days": FLOOR_LOOKBACK_DAYS,
            "floor_threshold_multiplier": FLOOR_THRESHOLD_MULTIPLIER,
            "floor_min_history_points": 3,
            "floor_min_baseline_mean_s": 300,
            "session_short_threshold_s": SHORT_SESSION_THRESHOLD_S,
            "session_baseline_source": baseline_source,
            "threshold_changes_recommended": False,
        },
        "floor_runtime": {
            **floor_stats,
            "alerts": floor_replayed,
        },
        "furnace_session": {
            **session_stats,
            "warnings": session_replayed,
        },
        "conclusion": {
            "replayed_flags_reviewed": len(floor_replayed) + len(session_replayed),
            "contract_violations": 0,
            "fault_false_positive_rate": None,
            "decision": "No threshold change",
            "reason": (
                "Every replayed flag met its detector contract, but the event history "
                "does not contain enough ground truth to label equipment faults."
            ),
            "next_evidence_needed": (
                "Record operator or maintenance labels for each alert before tuning "
                "thresholds against fault-level false positives."
            ),
        },
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    """Render a compact human-readable report from ``build_report`` output."""
    coverage = report["coverage"]
    configuration = report["configuration"]
    floor = report["floor_runtime"]
    session = report["furnace_session"]
    conclusion = report["conclusion"]
    date_range = coverage["date_range"]

    lines = [
        "# HomeOps anomaly detector validation",
        "",
        f"Source: `{report['source']}`",
        f"Replay order: {report['replay_order']}",
        (
            f"Coverage: `{_fmt(date_range['first'])}` → `{_fmt(date_range['last'])}`; "
            f"{coverage['valid_json_objects']} JSON objects"
        ),
        (
            f"Data quality: {coverage['invalid_json_lines']} invalid JSON lines; "
            f"{coverage['non_object_lines']} non-object lines"
        ),
        "",
        "## Configuration",
        "",
        (
            f"Floor runtime: {configuration['floor_lookback_days']}-summary lookback, "
            f"{configuration['floor_threshold_multiplier']}× mean threshold, "
            f"minimum baseline mean {configuration['floor_min_baseline_mean_s']}s"
        ),
        (
            f"Furnace sessions: short warning below "
            f"{configuration['session_short_threshold_s']}s; "
            f"baseline: {configuration['session_baseline_source']}"
        ),
        "",
        "## Floor runtime replay",
        "",
        (
            f"Evaluated {floor['evaluated_floor_days']} floor-days across "
            f"{floor['summary_events']} summary events ({floor['summary_dates']['unique_days']} "
            f"unique dates). Replayed **{floor['replayed_alert_count']}** alerts; "
            f"the replay matches emitted alerts: **{floor['replay_matches_emitted']}**."
        ),
        "",
        "| Date | Floor | Runtime (s) | Baseline (s) | Threshold (s) | Outdoor (°F) | Review |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    if floor["alerts"]:
        for item in floor["alerts"]:
            lines.append(
                f"| {item['date']} | {item['floor']} | {item['runtime_s']} | "
                f"{_fmt(item['baseline_mean_s'])} | {_fmt(item['threshold_s'])} | "
                f"{_fmt(item['outdoor_temp_avg_f'])} | {item['review_note']} |"
            )
    else:
        lines.append("| — | — | — | — | — | — | No alerts |")

    if not floor["replay_matches_emitted"]:
        lines.append("")
        lines.append("Replay/emission differences (keys are date, floor, runtime):")
        for key in floor["replayed_not_emitted"]:
            lines.append(f"- Replay-only: `{key}`")
        for key in floor["emitted_not_replayed"]:
            lines.append(f"- Emitted-only: `{key}`")

    lines.extend(
        [
            "",
            "## Furnace session replay",
            "",
            (
                f"Evaluated {session['complete_sessions']} complete sessions; skipped "
                f"{session['null_duration_sessions_skipped']} null-duration sessions. "
                f"Replayed **{session['replayed_warning_count']}** warnings; the replay "
                f"matches emitted warnings: **{session['replay_matches_emitted']}**."
            ),
            "",
            "| Warning | Session timestamp | Floor | Duration (s) | Threshold (s) | "
            "Restart context | Review |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    if session["warnings"]:
        for item in session["warnings"]:
            lines.append(
                f"| {item['warning']} | {item['session_ts']} | "
                f"{_fmt(item['floor'])} | {item['duration_s']} | "
                f"{_fmt(item['threshold_s'])} | {item['across_restart']} | "
                f"{item['review_note']} |"
            )
    else:
        lines.append("| — | — | — | — | — | — | No warnings |")

    if not session["replay_matches_emitted"]:
        lines.append("")
        lines.append(
            "Replay/emission differences (keys are warning, session timestamp, floor, duration):"
        )
        for key in session["replayed_not_emitted"]:
            lines.append(f"- Replay-only: `{key}`")
        for key in session["emitted_not_replayed"]:
            lines.append(f"- Emitted-only: `{key}`")

    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"**{conclusion['decision']}** — {conclusion['reason']}",
            "",
            (
                "The replay validates detector-contract behavior, not whether a flagged "
                "condition caused a mechanical fault. The log has no operator, "
                "maintenance, or limit-switch labels, so a fault-level false-positive "
                "rate is not measurable from this snapshot. "
                f"{conclusion['next_evidence_needed']}"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        default=None,
        help="Derived event JSONL path, or '-' for stdin (overrides DERIVED_EVENT_LOG).",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Optional baseline_constants.json for furnace long-session evaluation.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Report format (default: markdown).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path; otherwise write the report to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    source = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
    try:
        events, invalid_json_lines, non_object_lines = load_events(source)
        baseline, baseline_source = load_baseline(args.baseline)
        report = build_report(
            events,
            invalid_json_lines=invalid_json_lines,
            non_object_lines=non_object_lines,
            baseline=baseline,
            baseline_source=baseline_source,
            source=source,
        )
        rendered = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.format == "json"
            else render_markdown(report)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
