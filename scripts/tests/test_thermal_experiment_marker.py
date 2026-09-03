"""Tests for the data-only natural-language thermal experiment marker bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from thermal_experiment_marker import (
    DEFAULT_DURATION_S,
    MarkerError,
    MarkerStore,
    canonical_configurations,
    checklist_status,
    configuration_for,
    parse_command,
    reconstruct_runs,
    record_message,
    render_checklist,
)

RECEIVED = datetime(2026, 9, 1, 19, 30, tzinfo=UTC)


def test_live_singleton_message_uses_exact_received_time_and_default_duration():
    parsed = parse_command(
        "Starting a cooling test on Floor 1.",
        received_at=RECEIVED,
    )

    assert parsed.action == "start"
    assert parsed.mode == "cool"
    assert parsed.active_zones == ("floor_1",)
    assert parsed.duration_s == DEFAULT_DURATION_S
    assert parsed.duration_defaulted is True
    assert parsed.start_ts == RECEIVED
    assert parsed.confidence == "exact"


@pytest.mark.parametrize(
    "message, expected_zone",
    (
        ("First floor cooling experiment started.", "floor_1"),
        ("The Floor 2 cooling test started.", "floor_2"),
        ("I started a cooling test on Floor 3.", "floor_3"),
    ),
)
def test_live_start_accepts_common_past_tense_phrasing(message: str, expected_zone: str):
    parsed = parse_command(message, received_at=RECEIVED)

    assert parsed.action == "start"
    assert parsed.mode == "cool"
    assert parsed.active_zones == (expected_zone,)
    assert parsed.duration_s == DEFAULT_DURATION_S
    assert parsed.duration_defaulted is True
    assert parsed.start_ts == RECEIVED
    assert parsed.raw_text == message


def test_live_pair_message_extracts_all_active_floors_and_declared_target():
    parsed = parse_command(
        "Start a 45 minute cooling test on Floors 1 and 3, target 72.",
        received_at=RECEIVED,
    )

    assert parsed.active_zones == ("floor_1", "floor_3")
    assert parsed.duration_s == 45 * 60
    assert parsed.duration_defaulted is False
    assert parsed.target_f == 72.0


def test_end_resolves_one_active_run_and_is_idempotent(tmp_path: Path):
    store = MarkerStore(tmp_path / "markers.jsonl")
    start = record_message(
        "Starting a 30-minute cooling test on Floor 1.",
        store=store,
        source_message_id="start-1",
        received_at=RECEIVED,
    )
    experiment_id = start["record"]["data"]["experiment_id"]

    end = record_message(
        "Floor 1 test ended.",
        store=store,
        source_message_id="end-1",
        received_at=datetime(2026, 9, 1, 20, 0, tzinfo=UTC),
    )
    assert end["duplicate"] is False
    assert end["record"]["data"]["action"] == "end"
    assert end["record"]["data"]["status"] == "completed"
    assert end["record"]["data"]["experiment_id"] == experiment_id

    retry = record_message(
        "Floor 1 test ended.",
        store=store,
        source_message_id="end-1",
        received_at=datetime(2026, 9, 1, 20, 1, tzinfo=UTC),
    )
    assert retry["duplicate"] is True
    assert retry["record"]["event_id"] == end["record"]["event_id"]
    run = reconstruct_runs(store.read())[experiment_id]
    assert run["status"] == "completed"
    assert len(run["marker_events"]) == 2


def test_abort_without_floor_is_safe_when_only_one_run_is_active(tmp_path: Path):
    store = MarkerStore(tmp_path / "markers.jsonl")
    record_message(
        "Starting a cooling test on Floors 1 and 3.",
        store=store,
        source_message_id="start-2",
        received_at=RECEIVED,
    )

    result = record_message(
        "Stop the test — the kids are getting cold.",
        store=store,
        source_message_id="abort-2",
        received_at=datetime(2026, 9, 1, 19, 45, tzinfo=UTC),
    )
    assert result["record"]["data"]["status"] == "aborted"
    assert result["record"]["data"]["abort_reason"] == "household comfort"


def test_retroactive_message_uses_local_last_night_time_and_approximate_confidence():
    parsed = parse_command(
        "I ran a Floor 1 cooling test last night around 11:30 pm for about 30 minutes.",
        received_at=datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
    )

    assert parsed.action == "retroactive"
    assert parsed.mode == "cool"
    assert parsed.active_zones == ("floor_1",)
    assert parsed.duration_s == DEFAULT_DURATION_S
    assert parsed.start_ts == datetime(2026, 9, 2, 3, 30, tzinfo=UTC)
    assert parsed.confidence == "approximate"


def test_retroactive_record_is_completed_when_duration_is_declared(tmp_path: Path):
    store = MarkerStore(tmp_path / "markers.jsonl")
    result = record_message(
        "I ran a Floor 2 cooling test yesterday at 23:00 for 30 minutes.",
        store=store,
        source_message_id="retro-1",
        received_at=datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
    )

    data = result["record"]["data"]
    assert data["action"] == "retroactive"
    assert data["status"] == "completed"
    assert data["confidence"] == "approximate"
    assert data["start_ts"] == "2026-09-02T03:00:00+00:00"
    assert data["end_ts"] == "2026-09-02T03:30:00+00:00"


def test_repeated_runs_are_distinct_and_checklist_marks_configuration_once(tmp_path: Path):
    store = MarkerStore(tmp_path / "markers.jsonl")
    for index in range(2):
        start_id = f"f1-start-{index}"
        end_id = f"f1-end-{index}"
        start_time = datetime(2026, 9, 1, 19 + index, 0, tzinfo=UTC)
        record_message(
            "Starting a 30-minute cooling test on Floor 1.",
            store=store,
            source_message_id=start_id,
            received_at=start_time,
        )
        record_message(
            "Floor 1 test ended.",
            store=store,
            source_message_id=end_id,
            received_at=datetime(2026, 9, 1, 19 + index, 30, tzinfo=UTC),
        )

    status = checklist_status(store.read())
    floor_1 = status[0]
    assert floor_1["configuration_id"] == "cool-s1-f1"
    assert floor_1["checked"] is True
    assert floor_1["completed_runs"] == 2
    assert floor_1["run_count"] == 2
    assert all(item["checked"] is False for item in status[1:])
    rendered = render_checklist(status)
    assert "[x] cool-s1-f1" in rendered
    assert "Coverage: 1/6" in rendered

    run_ids = {run["experiment_id"] for run in reconstruct_runs(store.read()).values()}
    assert len(run_ids) == 2


def test_two_overlapping_active_runs_require_unambiguous_end(tmp_path: Path):
    store = MarkerStore(tmp_path / "markers.jsonl")
    record_message(
        "Starting a cooling test on Floor 1.",
        store=store,
        source_message_id="overlap-1",
        received_at=RECEIVED,
    )
    record_message(
        "Starting a cooling test on Floor 1.",
        store=store,
        source_message_id="overlap-2",
        received_at=datetime(2026, 9, 1, 19, 31, tzinfo=UTC),
    )

    with pytest.raises(MarkerError, match="multiple active"):
        record_message(
            "Floor 1 test ended.",
            store=store,
            source_message_id="overlap-end",
            received_at=datetime(2026, 9, 1, 20, 0, tzinfo=UTC),
        )


def test_invalid_or_ambiguous_messages_fail_closed():
    with pytest.raises(MarkerError, match="must name heating or cooling"):
        parse_command("Starting a test on Floor 1.", received_at=RECEIVED)
    with pytest.raises(MarkerError, match="must name one or more floors"):
        parse_command("Starting a 30-minute cooling test.", received_at=RECEIVED)
    with pytest.raises(MarkerError, match="does not clearly"):
        parse_command("The house is comfortable.", received_at=RECEIVED)
    with pytest.raises(MarkerError, match="non-empty subset"):
        configuration_for("cool", ("floor_4",))


def test_canonical_configuration_order_is_the_agreed_singles_then_pairs():
    configurations = canonical_configurations()

    assert [item.configuration_id for item in configurations] == [
        "cool-s1-f1",
        "cool-s1-f2",
        "cool-s1-f3",
        "cool-p12",
        "cool-p13",
        "cool-p23",
    ]
