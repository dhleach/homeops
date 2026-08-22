"""Tests for the hourly zone-call frequency report.

Revision history:
  2026-08-22  Added coverage for malformed input, local date/timezone handling,
              floor normalization, deterministic reports, and CLI output.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from floor_hourly_heatmap import (
    KNOWN_FLOORS,
    SCHEMA,
    _normalise_floor,
    build_report,
    extract_records,
    load_events,
    main,
    render_table,
)


def _event(
    floor: str = "floor_1",
    started_at: str = "2026-08-15T12:00:00+00:00",
    *,
    entity_id: str | None = None,
) -> dict:
    data = {"floor": floor, "started_at": started_at}
    if entity_id is not None:
        data["entity_id"] = entity_id
    return {"schema": SCHEMA, "ts": started_at, "data": data}


def test_load_events_counts_bad_json_and_non_objects(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(_event()) + "\nnot-json\n42\n\n", encoding="utf-8")

    events, invalid_json_lines, non_object_lines = load_events(str(path))

    assert len(events) == 1
    assert invalid_json_lines == 1
    assert non_object_lines == 1


def test_normalise_floor_accepts_alias_and_entity_fallback():
    assert _normalise_floor("Floor 2") == "floor_2"
    assert _normalise_floor(None, "binary_sensor.floor_3_heating_call") == "floor_3"
    assert _normalise_floor("attic", "binary_sensor.unknown") is None


def test_extract_records_uses_inclusive_local_date_boundaries():
    events = [
        _event(started_at="2026-08-15T03:59:59+00:00"),  # local Aug 14
        _event(started_at="2026-08-15T04:00:00+00:00"),  # local Aug 15 00:00
        _event(started_at="2026-08-22T03:59:59+00:00"),  # local Aug 21 23:59
        _event(started_at="2026-08-22T04:00:00+00:00"),  # local Aug 22
    ]

    records, counters = extract_records(
        events,
        date(2026, 8, 15),
        date(2026, 8, 21),
        ZoneInfo("America/New_York"),
    )

    assert len(records) == 2
    assert [record["date"] for record in records] == ["2026-08-15", "2026-08-21"]
    assert [record["hour"] for record in records] == [0, 23]
    assert counters["valid_payloads"] == 4
    assert counters["included_events"] == 2
    assert counters["excluded_out_of_range"] == 2


def test_extract_records_converts_dst_timezone_before_bucket():
    records, _ = extract_records(
        [_event(started_at="2026-07-15T04:30:00+00:00")],
        date(2026, 7, 15),
        date(2026, 7, 15),
        ZoneInfo("America/New_York"),
    )

    assert records[0]["date"] == "2026-07-15"
    assert records[0]["hour"] == 0


def test_invalid_payloads_are_counted_without_crashing():
    bad_floor = _event(floor="basement")
    bad_timestamp = _event(started_at="not-a-timestamp")
    missing_data = {"schema": SCHEMA, "data": None}

    records, counters = extract_records(
        [bad_floor, bad_timestamp, missing_data],
        date(2026, 8, 1),
        date(2026, 8, 31),
        ZoneInfo("America/New_York"),
    )

    assert records == []
    assert counters["matching_events"] == 3
    assert counters["valid_payloads"] == 0
    assert counters["invalid_payloads"] == 3


def test_unrelated_schemas_are_ignored():
    unrelated = {"schema": "homeops.consumer.floor_call_ended.v1", "data": {}}

    records, counters = extract_records(
        [unrelated],
        date(2026, 8, 1),
        date(2026, 8, 31),
        ZoneInfo("America/New_York"),
    )

    assert records == []
    assert counters["matching_events"] == 0


def test_build_report_has_all_floors_and_peak_hours():
    events = [
        _event("floor_1", "2026-08-15T11:00:00+00:00"),  # 07:00 EDT
        _event("floor_1", "2026-08-16T11:30:00+00:00"),
        _event("floor_1", "2026-08-16T22:00:00+00:00"),  # 18:00 EDT
        _event("floor_2", "2026-08-15T12:00:00+00:00"),
    ]

    report = build_report(
        events,
        date(2026, 8, 15),
        date(2026, 8, 16),
        ZoneInfo("America/New_York"),
    )

    assert tuple(report["floors"]) == KNOWN_FLOORS
    assert report["floors"]["floor_1"]["hours"][7] == 2
    assert report["floors"]["floor_1"]["hours"][18] == 1
    assert report["floors"]["floor_1"]["peak_hours"] == [7]
    assert report["floors"]["floor_2"]["total"] == 1
    assert report["coverage"]["observed_dates"] == ["2026-08-15", "2026-08-16"]


def test_build_report_supports_entity_fallback():
    event = _event(floor="", entity_id="binary_sensor.floor_3_heating_call")

    report = build_report([event], date(2026, 8, 15), date(2026, 8, 15), ZoneInfo("UTC"))

    assert report["floors"]["floor_3"]["total"] == 1
    assert report["data_quality"]["invalid_payloads"] == 0


def test_empty_report_renders_rows_and_zero_summary():
    report = build_report([], date(2026, 8, 15), date(2026, 8, 21), ZoneInfo("UTC"))

    rendered = render_table(report)

    assert "00 01 02" in rendered
    assert "floor_1" in rendered
    assert "floor_2" in rendered
    assert "floor_3" in rendered
    assert "Included calls: 0 of 0 valid matching payloads" in rendered
    assert "Read-only report" in rendered


def test_render_table_is_deterministic():
    report = build_report(
        [_event("floor_2")], date(2026, 8, 15), date(2026, 8, 21), ZoneInfo("UTC")
    )

    assert render_table(report) == render_table(report)


def test_json_cli_writes_report(tmp_path: Path):
    log_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "report.json"
    log_path.write_text(json.dumps(_event()) + "\n", encoding="utf-8")

    assert (
        main(
            [
                "--log",
                str(log_path),
                "--start",
                "2026-08-15",
                "--end",
                "2026-08-21",
                "--format",
                "json",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert report["schema"] == "homeops.floor-hourly-heatmap.v1"
    assert report["date_range"]["inclusive_days"] == 7
    assert report["coverage"]["included_events"] == 1


def test_invalid_date_range_returns_error(tmp_path: Path, capsys):
    log_path = tmp_path / "events.jsonl"
    log_path.write_text("", encoding="utf-8")

    assert (
        main(
            [
                "--log",
                str(log_path),
                "--start",
                "2026-08-21",
                "--end",
                "2026-08-15",
            ]
        )
        == 1
    )
    assert "--start must be on or before --end" in capsys.readouterr().err
