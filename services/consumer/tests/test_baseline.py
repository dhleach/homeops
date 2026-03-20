"""Tests for baseline.py — furnace session duration statistics."""

import json
import tempfile

from baseline import compute_baseline, load_events_from_jsonl

_SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"
_FURNACE_ENTITY = "binary_sensor.furnace_heating"


def make_session_event(
    floor: str,
    duration_s: int | None,
    entity_id: str = _FURNACE_ENTITY,
) -> dict:
    return {
        "schema": _SESSION_SCHEMA,
        "source": "consumer.v1",
        "ts": "2024-01-15T12:00:00+00:00",
        "data": {
            "floor": floor,
            "entity_id": entity_id,
            "duration_s": duration_s,
            "ended_at": "2024-01-15T12:00:00+00:00",
        },
    }


class TestComputeBaseline:
    def test_normal_case_multiple_floors(self):
        events = [
            make_session_event("floor_1", 600),
            make_session_event("floor_1", 1200),
            make_session_event("floor_1", 1800),
            make_session_event("floor_1", 2400),
            make_session_event("floor_2", 300),
            make_session_event("floor_2", 900),
        ]
        result = compute_baseline(events)

        assert set(result.keys()) == {"floor_1", "floor_2"}

        f1 = result["floor_1"]
        assert f1["count"] == 4
        assert f1["min"] == 600
        assert f1["max"] == 2400
        assert f1["median"] == 1500.0
        assert f1["p75"] == 1950.0
        assert f1["p95"] == 2310.0

        f2 = result["floor_2"]
        assert f2["count"] == 2
        assert f2["min"] == 300
        assert f2["max"] == 900

    def test_single_event_per_floor(self):
        events = [
            make_session_event("floor_1", 600),
            make_session_event("floor_2", 1200),
            make_session_event("floor_3", 1800),
        ]
        result = compute_baseline(events)

        assert result["floor_1"]["count"] == 1
        assert result["floor_1"]["min"] == 600
        assert result["floor_1"]["max"] == 600
        assert result["floor_1"]["median"] == 600
        assert result["floor_1"]["p75"] == 600
        assert result["floor_1"]["p95"] == 600

        assert result["floor_2"]["count"] == 1
        assert result["floor_3"]["count"] == 1

    def test_all_same_duration(self):
        events = [make_session_event("floor_1", 900) for _ in range(5)]
        result = compute_baseline(events)

        f1 = result["floor_1"]
        assert f1["count"] == 5
        assert f1["min"] == 900
        assert f1["max"] == 900
        assert f1["median"] == 900
        assert f1["p75"] == 900
        assert f1["p95"] == 900

    def test_skips_none_duration(self):
        events = [
            make_session_event("floor_1", 600),
            make_session_event("floor_1", None),
            make_session_event("floor_1", 1200),
        ]
        result = compute_baseline(events)
        assert result["floor_1"]["count"] == 2

    def test_empty_events(self):
        result = compute_baseline([])
        assert result == {}

    def test_all_none_durations(self):
        events = [make_session_event("floor_1", None) for _ in range(3)]
        result = compute_baseline(events)
        assert result == {}

    def test_groups_by_entity_id_when_no_floor(self):
        events = [
            {
                "schema": _SESSION_SCHEMA,
                "data": {"entity_id": _FURNACE_ENTITY, "duration_s": 600},
            },
            {
                "schema": _SESSION_SCHEMA,
                "data": {"entity_id": _FURNACE_ENTITY, "duration_s": 1200},
            },
        ]
        result = compute_baseline(events)
        assert _FURNACE_ENTITY in result
        assert result[_FURNACE_ENTITY]["count"] == 2

    def test_percentile_ordering(self):
        # 10 sessions of increasing length — verify p95 > p75 > median
        durations = list(range(100, 1100, 100))  # 100..1000
        events = [make_session_event("floor_1", d) for d in durations]
        result = compute_baseline(events)
        f1 = result["floor_1"]
        assert f1["p75"] > f1["median"]
        assert f1["p95"] > f1["p75"]


class TestLoadEventsFromJsonl:
    def _write_jsonl(self, lines: list[str]) -> str:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for line in lines:
            tmp.write(line + "\n")
        tmp.close()
        return tmp.name

    def test_filters_correct_schema(self):
        lines = [
            json.dumps({"schema": _SESSION_SCHEMA, "data": {"duration_s": 600}}),
            json.dumps(
                {"schema": "homeops.consumer.floor_call_ended.v1", "data": {"duration_s": 300}}
            ),  # noqa: E501
            json.dumps({"schema": "homeops.observer.state_changed.v1", "data": {}}),
        ]
        path = self._write_jsonl(lines)
        events = load_events_from_jsonl(path)
        assert len(events) == 1
        assert events[0]["schema"] == _SESSION_SCHEMA

    def test_skips_blank_lines(self):
        lines = [
            json.dumps({"schema": _SESSION_SCHEMA, "data": {"duration_s": 600}}),
            "",
            "   ",
            json.dumps({"schema": _SESSION_SCHEMA, "data": {"duration_s": 900}}),
        ]
        path = self._write_jsonl(lines)
        events = load_events_from_jsonl(path)
        assert len(events) == 2

    def test_skips_invalid_json(self):
        lines = [
            json.dumps({"schema": _SESSION_SCHEMA, "data": {"duration_s": 600}}),
            "not valid json {{{",
            json.dumps({"schema": _SESSION_SCHEMA, "data": {"duration_s": 900}}),
        ]
        path = self._write_jsonl(lines)
        events = load_events_from_jsonl(path)
        assert len(events) == 2

    def test_empty_file(self):
        path = self._write_jsonl([])
        events = load_events_from_jsonl(path)
        assert events == []

    def test_no_matching_events(self):
        lines = [
            json.dumps({"schema": "homeops.consumer.floor_call_ended.v1", "data": {}}),
        ]
        path = self._write_jsonl(lines)
        events = load_events_from_jsonl(path)
        assert events == []
