"""Tests for the read-only historical anomaly validation report.

Revision history:
  2026-08-22  Added fixture coverage for JSONL loading, detector replay,
              emitted-event comparison, baseline selection, and report output.
"""

from __future__ import annotations

import json
from pathlib import Path

from validate_anomalies import (
    build_report,
    load_events,
    main,
    render_markdown,
    replay_floor_anomalies,
    replay_session_anomalies,
)

SUMMARY_SCHEMA = "homeops.consumer.furnace_daily_summary.v1"
SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"
FLOOR_ANOMALY_SCHEMA = "homeops.consumer.floor_runtime_anomaly.v1"
SHORT_WARNING_SCHEMA = "homeops.consumer.heating_short_session_warning.v1"


def make_summary(
    date_str: str,
    floor_1: int = 1000,
    floor_2: int = 1000,
    floor_3: int = 1000,
) -> dict:
    return {
        "schema": SUMMARY_SCHEMA,
        "ts": f"{date_str}T23:59:00+00:00",
        "data": {
            "date": date_str,
            "per_floor_runtime_s": {
                "floor_1": floor_1,
                "floor_2": floor_2,
                "floor_3": floor_3,
            },
            "outdoor_temp_avg_f": 40.0,
        },
    }


def make_session(ended_at: str, duration_s: int | None, floor: str | None = None) -> dict:
    return {
        "schema": SESSION_SCHEMA,
        "ts": ended_at,
        "data": {
            "ended_at": ended_at,
            "duration_s": duration_s,
            "floor": floor,
            "outdoor_temp_f": 40.0,
        },
    }


def test_load_events_counts_bad_json_and_non_objects(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"schema":"ok"}\nnot-json\n42\n\n', encoding="utf-8")

    events, invalid_json_lines, non_object_lines = load_events(str(path))

    assert len(events) == 1
    assert invalid_json_lines == 1
    assert non_object_lines == 1


def test_floor_replay_uses_prior_jsonl_summaries():
    events = [
        make_summary("2026-01-01"),
        make_summary("2026-01-02"),
        make_summary("2026-01-03"),
        make_summary("2026-01-04", floor_1=2000),
    ]

    replayed, emitted, stats = replay_floor_anomalies(events)

    assert emitted == []
    assert len(replayed) == 1
    assert replayed[0]["date"] == "2026-01-04"
    assert replayed[0]["floor"] == "floor_1"
    assert replayed[0]["baseline_mean_s"] == 1000.0
    assert replayed[0]["threshold_s"] == 1500.0
    assert stats["evaluated_floor_days"] == 12
    assert stats["replayed_alert_count"] == 1
    assert stats["replayed_not_emitted"] == [["2026-01-04", "floor_1", 2000]]


def test_session_replay_honors_short_and_long_rules():
    events = [
        make_session("2026-02-01T00:01:00+00:00", 30),
        make_session("2026-02-02T00:01:00+00:00", 2701),
        make_session("2026-02-03T00:01:00+00:00", 900),
    ]

    replayed, emitted, stats = replay_session_anomalies(events, {})

    assert emitted == []
    assert [item["warning"] for item in replayed] == ["short", "long"]
    assert stats["complete_sessions"] == 3
    assert stats["null_duration_sessions_skipped"] == 0
    assert stats["replayed_warning_counts"] == {"long": 1, "short": 1}
    assert len(stats["replayed_not_emitted"]) == 2


def test_null_duration_sessions_are_skipped():
    replayed, _, stats = replay_session_anomalies(
        [make_session("2026-02-01T00:01:00+00:00", None)], {}
    )

    assert replayed == []
    assert stats["complete_sessions"] == 0
    assert stats["null_duration_sessions_skipped"] == 1


def test_report_compares_replay_with_emitted_alerts():
    events = [
        make_summary("2026-01-01"),
        make_summary("2026-01-02"),
        make_summary("2026-01-03"),
        make_summary("2026-01-04", floor_1=2000),
        {
            "schema": FLOOR_ANOMALY_SCHEMA,
            "data": {"date": "2026-01-04", "floor": "floor_1", "runtime_s": 2000},
        },
        make_session("2026-02-01T00:01:00+00:00", 30),
        {
            "schema": SHORT_WARNING_SCHEMA,
            "data": {
                "session_ts": "2026-02-01T00:01:00+00:00",
                "floor": None,
                "duration_s": 30,
            },
        },
    ]

    report = build_report(events, source="fixture.jsonl")

    assert report["floor_runtime"]["replay_matches_emitted"] is True
    assert report["furnace_session"]["replay_matches_emitted"] is True
    assert report["conclusion"]["replayed_flags_reviewed"] == 2
    assert report["conclusion"]["fault_false_positive_rate"] is None


def test_explicit_baseline_changes_session_configuration():
    events = [make_session("2026-02-01T00:01:00+00:00", 1901, floor="floor_1")]
    baseline = {"floor_1": {"p95": 2000.0}}

    replayed, _, stats = replay_session_anomalies(events, baseline)

    assert replayed == []
    assert stats["replayed_warning_count"] == 0


def test_markdown_report_contains_review_and_decision():
    report = build_report(
        [
            make_summary("2026-01-01"),
            make_summary("2026-01-02"),
            make_summary("2026-01-03"),
            make_summary("2026-01-04", floor_2=2000),
        ],
        source="fixture.jsonl",
    )

    markdown = render_markdown(report)

    assert "# HomeOps anomaly detector validation" in markdown
    assert "floor_2" in markdown
    assert "Detector contract met" in markdown
    assert "## Decision" in markdown
    assert "No threshold change" in markdown


def test_json_cli_writes_deterministic_report(tmp_path: Path):
    log_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "report.json"
    log_path.write_text(
        "\n".join(
            json.dumps(item)
            for item in [
                make_summary("2026-01-01"),
                make_summary("2026-01-02"),
                make_summary("2026-01-03"),
                make_summary("2026-01-04", floor_3=2000),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["--log", str(log_path), "--format", "json", "--output", str(output_path)]) == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert report["source"] == str(log_path)
    assert report["floor_runtime"]["replayed_alert_count"] == 1
    assert "generated_at" not in report
