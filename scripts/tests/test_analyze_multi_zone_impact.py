"""Tests for the multi-zone contention analysis report.

Revision history:
  2026-08-22  Added fixture coverage for event extraction, contention grouping,
              sample sufficiency, comparisons, Markdown, and JSON CLI output.
"""

from __future__ import annotations

import json
from pathlib import Path

from analyze_multi_zone_impact import (
    analyse_records,
    build_report,
    extract_records,
    load_events,
    main,
    render_markdown,
)

SCHEMA = "homeops.consumer.zone_time_to_temp.v1"


def make_event(
    zone: str,
    duration_s: int,
    other_zones: list[str] | None = None,
    ts: str = "2026-04-01T10:00:00+00:00",
) -> dict:
    return {
        "schema": SCHEMA,
        "ts": ts,
        "data": {
            "zone": zone,
            "duration_s": duration_s,
            "degrees_per_min": 0.2,
            "outdoor_temp_f": 40.0,
            "other_zones_calling": other_zones or [],
        },
    }


def test_load_events_counts_bad_json_and_non_objects(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"schema":"ok"}\nnot-json\n42\n\n', encoding="utf-8")

    events, invalid_json_lines, non_object_lines = load_events(str(path))

    assert len(events) == 1
    assert invalid_json_lines == 1
    assert non_object_lines == 1


def test_extract_records_normalises_floor_call_entities():
    records, invalid_payloads = extract_records(
        [make_event("floor_2", 900, ["binary_sensor.floor_1_heating_call"])]
    )

    assert invalid_payloads == 0
    assert records[0]["zone"] == "floor_2"
    assert records[0]["other_zones"] == ("floor_1",)
    assert records[0]["duration_s"] == 900.0


def test_exact_contention_groups_are_kept_separate():
    records, _ = extract_records(
        [
            make_event("floor_2", 900),
            make_event("floor_2", 1100, ["binary_sensor.floor_1_heating_call"]),
            make_event(
                "floor_2",
                1300,
                ["binary_sensor.floor_1_heating_call", "binary_sensor.floor_3_heating_call"],
            ),
        ]
    )

    report = analyse_records(records, min_samples=1)

    assert len(report["groups"]) == 3
    assert report["groups"][0]["other_zones"] == []
    assert report["groups"][1]["other_zones"] == ["floor_1"]
    assert report["groups"][2]["other_zones"] == ["floor_1", "floor_3"]


def test_sparse_history_refuses_scheduling_conclusion():
    report = build_report([make_event("floor_1", 687)])

    assert report["coverage"]["valid_records"] == 1
    assert report["conclusion"]["status"] == "insufficient_data"
    assert report["conclusion"]["supports_scheduling_conclusion"] is False
    assert report["zone_comparisons"][0]["contended_count"] == 0


def test_sufficient_groups_compute_median_duration_delta():
    events = [make_event("floor_2", duration) for duration in (100, 110, 120, 130, 140)] + [
        make_event("floor_2", duration, ["binary_sensor.floor_1_heating_call"])
        for duration in (200, 210, 220, 230, 240)
    ]

    report = build_report(events, min_samples=5)
    comparison = report["zone_comparisons"][0]

    assert comparison["sufficient_samples"] is True
    assert comparison["uncontended_median_duration_s"] == 120.0
    assert comparison["contended_median_duration_s"] == 220.0
    assert comparison["median_duration_delta_s"] == 100.0
    assert report["conclusion"]["status"] == "comparison_available"


def test_invalid_zone_time_payload_is_counted():
    event = make_event("floor_1", 687)
    event["data"]["duration_s"] = "unknown"

    report = build_report([event])

    assert report["coverage"]["valid_records"] == 0
    assert report["data_quality"]["invalid_zone_time_payloads"] == 1

    missing_contention = make_event("floor_1", 687)
    del missing_contention["data"]["other_zones_calling"]
    report = build_report([missing_contention])
    assert report["data_quality"]["invalid_zone_time_payloads"] == 1


def test_markdown_report_contains_insufficiency_reason():
    markdown = render_markdown(build_report([make_event("floor_1", 687)]))

    assert "# HomeOps multi-zone call impact analysis" in markdown
    assert "insufficient_data" in markdown
    assert "No zone has at least the minimum sample size" in markdown
    assert "No HA automation" in markdown


def test_json_cli_writes_report(tmp_path: Path):
    log_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "report.json"
    log_path.write_text(json.dumps(make_event("floor_1", 687)) + "\n", encoding="utf-8")

    assert main(["--log", str(log_path), "--format", "json", "--output", str(output_path)]) == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert report["schema"] == "homeops.multi-zone-impact-analysis.v1"
    assert report["coverage"]["valid_records"] == 1
