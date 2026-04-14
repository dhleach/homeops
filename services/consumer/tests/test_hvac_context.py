"""Tests for hvac_context.py — HVAC context summarizer for LLM input."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hvac_context import (
    _build_current_conditions,
    _build_daily_summary_section,
    _build_recent_sessions,
    _build_today_section,
    _build_warnings_section,
    _fmt_duration,
    _fmt_temp,
    _parse_ts,
    build_context,
    load_events,
    load_state,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_STATE = {
    "furnace_on_since": None,
    "climate_state": {
        "climate.floor_1_thermostat": {
            "setpoint": 68,
            "current_temp": 72,
            "hvac_mode": "heat",
            "hvac_action": "idle",
        },
        "climate.floor_2_thermostat": {
            "setpoint": 68,
            "current_temp": 74,
            "hvac_mode": "heat",
            "hvac_action": "idle",
        },
        "climate.floor_3_thermostat": {
            "setpoint": 68,
            "current_temp": 77,
            "hvac_mode": "heat",
            "hvac_action": "idle",
        },
    },
    "daily_state": {
        "furnace_runtime_s": 1800,
        "session_count": 5,
        "floor_runtime_s": {"floor_1": 900, "floor_2": 1200, "floor_3": 0},
        "per_floor_session_count": {
            "floor_1": 3,
            "floor_2": 4,
            "floor_3": 0,
        },
        "warnings_triggered": {
            "floor_2_long_call": 0,
            "floor_2_escalation": 0,
            "floor_no_response": 0,
            "zone_slow_to_heat": 0,
            "observer_silence": 1,
            "setpoint_miss": 0,
        },
    },
    "saved_at": "2026-04-14T21:25:52.000000+00:00",
}

SAMPLE_EVENTS = [
    # Yesterday's furnace daily summary
    {
        "schema": "homeops.consumer.furnace_daily_summary.v1",
        "source": "consumer.v1",
        "ts": "2026-04-14T00:22:51.000000+00:00",
        "data": {
            "date": "2026-04-13",
            "total_furnace_runtime_s": 9677,
            "session_count": 25,
            "per_floor_runtime_s": {"floor_1": 5966, "floor_2": 6607, "floor_3": 0},
            "outdoor_temp_min_f": 45.0,
            "outdoor_temp_max_f": 80.0,
            "outdoor_temp_avg_f": 60.0,
            "per_floor_session_count": {"floor_1": 17, "floor_2": 19, "floor_3": 0},
            "per_floor_avg_setpoint_f": {"floor_1": 68.0, "floor_2": 68.0, "floor_3": 68.0},
            "warnings_triggered": {
                "floor_2_long_call": 1,
                "floor_2_escalation": 0,
                "floor_no_response": 0,
                "zone_slow_to_heat": 0,
                "observer_silence": 0,
                "setpoint_miss": 0,
            },
        },
    },
    # Yesterday's floor daily summaries
    {
        "schema": "homeops.consumer.floor_daily_summary.v1",
        "source": "consumer.v1",
        "ts": "2026-04-14T00:22:51.000000+00:00",
        "data": {
            "floor": "floor_1",
            "date": "2026-04-13",
            "total_calls": 17,
            "total_runtime_s": 5966,
            "avg_duration_s": 350.9,
            "max_duration_s": 380,
            "outdoor_temp_avg_f": 60.0,
        },
    },
    {
        "schema": "homeops.consumer.floor_daily_summary.v1",
        "source": "consumer.v1",
        "ts": "2026-04-14T00:22:51.000000+00:00",
        "data": {
            "floor": "floor_2",
            "date": "2026-04-13",
            "total_calls": 19,
            "total_runtime_s": 6607,
            "avg_duration_s": 347.7,
            "max_duration_s": 399,
            "outdoor_temp_avg_f": 60.0,
        },
    },
    {
        "schema": "homeops.consumer.floor_daily_summary.v1",
        "source": "consumer.v1",
        "ts": "2026-04-14T00:22:51.000000+00:00",
        "data": {
            "floor": "floor_3",
            "date": "2026-04-13",
            "total_calls": 0,
            "total_runtime_s": 0,
            "avg_duration_s": None,
            "max_duration_s": None,
            "outdoor_temp_avg_f": 60.0,
        },
    },
    # Recent heating sessions
    {
        "schema": "homeops.consumer.heating_session_ended.v1",
        "source": "consumer.v1",
        "ts": "2026-04-14T12:00:00.000000+00:00",
        "data": {
            "duration_s": 360,
            "outdoor_temp_f": 55.0,
            "entity_id": "binary_sensor.furnace_heating",
            "ended_at": "2026-04-14T12:00:00.000000+00:00",
        },
    },
    {
        "schema": "homeops.consumer.heating_session_ended.v1",
        "source": "consumer.v1",
        "ts": "2026-04-14T10:00:00.000000+00:00",
        "data": {
            "duration_s": 420,
            "outdoor_temp_f": 52.0,
            "entity_id": "binary_sensor.furnace_heating",
            "ended_at": "2026-04-14T10:00:00.000000+00:00",
        },
    },
    # Warning event
    {
        "schema": "homeops.consumer.floor_2_long_call_warning.v1",
        "source": "consumer.v1",
        "ts": "2026-04-14T11:30:00.000000+00:00",
        "data": {"duration_s": 2800, "zone": "floor_2"},
    },
]


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_seconds_only(self) -> None:
        assert _fmt_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        assert _fmt_duration(90) == "1m 30s"

    def test_hours_and_minutes(self) -> None:
        assert _fmt_duration(3660) == "1h 1m"

    def test_zero(self) -> None:
        assert _fmt_duration(0) == "0s"

    def test_none(self) -> None:
        assert _fmt_duration(None) == "—"

    def test_exact_minutes(self) -> None:
        assert _fmt_duration(300) == "5m 0s"

    def test_float_input(self) -> None:
        assert _fmt_duration(90.7) == "1m 30s"


class TestFmtTemp:
    def test_integer(self) -> None:
        assert _fmt_temp(72) == "72°F"

    def test_float_rounds(self) -> None:
        assert _fmt_temp(72.6) == "73°F"

    def test_none(self) -> None:
        assert _fmt_temp(None) == "—"


class TestParseTs:
    def test_utc_aware(self) -> None:
        dt = _parse_ts("2026-04-14T12:00:00+00:00")
        assert dt.tzinfo is not None
        assert dt.hour == 12

    def test_naive_treated_as_utc(self) -> None:
        dt = _parse_ts("2026-04-14T12:00:00")
        assert dt.tzinfo is not None

    def test_offset_normalized_to_utc(self) -> None:
        dt = _parse_ts("2026-04-14T08:00:00-04:00")
        assert dt.hour == 12  # converted to UTC


# ---------------------------------------------------------------------------
# load_state tests
# ---------------------------------------------------------------------------


class TestLoadState:
    def test_loads_valid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        p.write_text(json.dumps(SAMPLE_STATE))
        result = load_state(str(p))
        assert result["daily_state"]["session_count"] == 5

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = load_state(str(tmp_path / "nonexistent.json"))
        assert result == {}


# ---------------------------------------------------------------------------
# load_events tests
# ---------------------------------------------------------------------------


class TestLoadEvents:
    def _write_events(self, tmp_path: Path, events: list[dict]) -> Path:
        p = tmp_path / "events.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return p

    def test_filters_by_lookback_window(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        recent = {
            "schema": "homeops.consumer.heating_session_ended.v1",
            "ts": (now - timedelta(hours=1)).isoformat(),
            "data": {},
        }
        old = {
            "schema": "homeops.consumer.heating_session_ended.v1",
            "ts": (now - timedelta(hours=100)).isoformat(),
            "data": {},
        }
        p = self._write_events(tmp_path, [recent, old])
        since = now - timedelta(hours=48)
        events = load_events(str(p), since)
        assert len(events) == 1

    def test_includes_yesterday_daily_summaries(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        # Daily summary from yesterday, outside the 1h window
        summary = {
            "schema": "homeops.consumer.furnace_daily_summary.v1",
            "ts": (now - timedelta(hours=25)).isoformat(),
            "data": {"date": yesterday},
        }
        p = self._write_events(tmp_path, [summary])
        since = now - timedelta(hours=1)
        events = load_events(str(p), since)
        assert len(events) == 1

    def test_skips_irrelevant_schemas(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        evt = {
            "schema": "homeops.consumer.thermostat_current_temp_updated.v1",
            "ts": (now - timedelta(minutes=5)).isoformat(),
            "data": {},
        }
        p = self._write_events(tmp_path, [evt])
        since = now - timedelta(hours=48)
        events = load_events(str(p), since)
        assert len(events) == 0

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        p = tmp_path / "events.jsonl"
        p.write_text(
            "not-json\n"
            + json.dumps(
                {
                    "schema": "homeops.consumer.heating_session_ended.v1",
                    "ts": (now - timedelta(hours=1)).isoformat(),
                    "data": {},
                }
            )
            + "\n"
        )
        since = now - timedelta(hours=48)
        events = load_events(str(p), since)
        assert len(events) == 1

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        since = datetime.now(UTC) - timedelta(hours=48)
        events = load_events(str(tmp_path / "nonexistent.jsonl"), since)
        assert events == []

    def test_empty_lines_skipped(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        p = tmp_path / "events.jsonl"
        p.write_text("\n\n\n")
        since = now - timedelta(hours=48)
        assert load_events(str(p), since) == []


# ---------------------------------------------------------------------------
# Section builder tests
# ---------------------------------------------------------------------------


class TestBuildCurrentConditions:
    def test_furnace_off(self) -> None:
        result = _build_current_conditions(SAMPLE_STATE)
        assert "Furnace: OFF" in result

    def test_furnace_on(self) -> None:
        state = {**SAMPLE_STATE, "furnace_on_since": "2026-04-14T21:00:00+00:00"}
        result = _build_current_conditions(state)
        assert "Furnace: ON" in result

    def test_zone_temps_present(self) -> None:
        result = _build_current_conditions(SAMPLE_STATE)
        assert "Floor 1" in result
        assert "72°F" in result
        assert "Floor 2" in result
        assert "74°F" in result
        assert "Floor 3" in result
        assert "77°F" in result

    def test_setpoints_shown(self) -> None:
        result = _build_current_conditions(SAMPLE_STATE)
        assert "setpoint 68°F" in result

    def test_hvac_action_shown(self) -> None:
        result = _build_current_conditions(SAMPLE_STATE)
        assert "idle" in result

    def test_empty_state_handled(self) -> None:
        result = _build_current_conditions({})
        assert "CURRENT CONDITIONS" in result


class TestBuildTodaySection:
    def test_session_count(self) -> None:
        result = _build_today_section(SAMPLE_STATE, "2026-04-14")
        assert "5 furnace sessions" in result

    def test_furnace_runtime(self) -> None:
        result = _build_today_section(SAMPLE_STATE, "2026-04-14")
        assert "30m 0s" in result

    def test_floor_runtimes(self) -> None:
        result = _build_today_section(SAMPLE_STATE, "2026-04-14")
        assert "Floor 1" in result
        assert "Floor 2" in result
        assert "Floor 3" in result

    def test_active_warnings_shown(self) -> None:
        result = _build_today_section(SAMPLE_STATE, "2026-04-14")
        assert "observer_silence" in result

    def test_no_warnings(self) -> None:
        state = {**SAMPLE_STATE}
        state["daily_state"] = {
            **state["daily_state"],
            "warnings_triggered": {k: 0 for k in state["daily_state"]["warnings_triggered"]},
        }
        result = _build_today_section(state, "2026-04-14")
        assert "Warnings: none" in result

    def test_singular_session(self) -> None:
        state = {**SAMPLE_STATE, "daily_state": {**SAMPLE_STATE["daily_state"], "session_count": 1}}
        result = _build_today_section(state, "2026-04-14")
        # Header line should use singular
        header = result.split("\n")[0]
        assert "1 furnace session" in header
        assert "1 furnace sessions" not in header


class TestBuildDailySummarySection:
    def test_returns_none_when_no_matching_summary(self) -> None:
        result = _build_daily_summary_section(SAMPLE_EVENTS, "2026-01-01", "OLD")
        assert result is None

    def test_yesterday_section_built(self) -> None:
        result = _build_daily_summary_section(SAMPLE_EVENTS, "2026-04-13", "YESTERDAY")
        assert result is not None
        assert "YESTERDAY" in result
        assert "25 furnace sessions" in result

    def test_outdoor_temp_range_shown(self) -> None:
        result = _build_daily_summary_section(SAMPLE_EVENTS, "2026-04-13", "YESTERDAY")
        assert "45°F" in result
        assert "80°F" in result

    def test_floor_runtimes_shown(self) -> None:
        result = _build_daily_summary_section(SAMPLE_EVENTS, "2026-04-13", "YESTERDAY")
        assert "Floor 1" in result
        assert "Floor 2" in result
        assert "17 sessions" in result

    def test_warnings_shown(self) -> None:
        result = _build_daily_summary_section(SAMPLE_EVENTS, "2026-04-13", "YESTERDAY")
        assert "floor_2_long_call" in result


class TestBuildRecentSessions:
    def test_sessions_present(self) -> None:
        result = _build_recent_sessions(SAMPLE_EVENTS)
        assert "RECENT HEATING SESSIONS" in result
        assert "6m 0s" in result  # 360s session
        assert "7m 0s" in result  # 420s session

    def test_most_recent_first(self) -> None:
        result = _build_recent_sessions(SAMPLE_EVENTS)
        lines = result.split("\n")
        # 12:00 UTC should appear before 10:00 UTC
        idx_recent = next(i for i, ln in enumerate(lines) if "12:00" in ln)
        idx_older = next(i for i, ln in enumerate(lines) if "10:00" in ln)
        assert idx_recent < idx_older

    def test_no_sessions_message(self) -> None:
        result = _build_recent_sessions([])
        assert "No sessions in lookback window" in result

    def test_capped_at_n(self) -> None:
        many = [
            {
                "schema": "homeops.consumer.heating_session_ended.v1",
                "ts": f"2026-04-14T{h:02d}:00:00+00:00",
                "data": {"duration_s": 300, "outdoor_temp_f": 60.0},
            }
            for h in range(15)
        ]
        result = _build_recent_sessions(many, n=5)
        # Should only show 5
        count = result.count("UTC:")
        assert count == 5

    def test_outdoor_temp_shown(self) -> None:
        result = _build_recent_sessions(SAMPLE_EVENTS)
        assert "55°F" in result
        assert "52°F" in result


class TestBuildWarningsSection:
    def test_warning_shown(self) -> None:
        since = datetime(2026, 4, 14, 0, 0, 0, tzinfo=UTC)
        result = _build_warnings_section(SAMPLE_EVENTS, since)
        assert "Floor 2 long call" in result

    def test_no_warnings_message(self) -> None:
        since = datetime(2026, 4, 14, 0, 0, 0, tzinfo=UTC)
        non_warning_events = [
            e
            for e in SAMPLE_EVENTS
            if e["schema"]
            not in {
                "homeops.consumer.floor_2_long_call_warning.v1",
                "homeops.consumer.furnace_short_call_warning.v1",
            }
        ]
        result = _build_warnings_section(non_warning_events, since)
        assert "None in lookback window" in result

    def test_warnings_outside_window_excluded(self) -> None:
        # since = far future, so nothing matches
        since = datetime(2030, 1, 1, tzinfo=UTC)
        result = _build_warnings_section(SAMPLE_EVENTS, since)
        assert "None in lookback window" in result


# ---------------------------------------------------------------------------
# build_context integration tests
# ---------------------------------------------------------------------------


class TestBuildContext:
    def _write_state(self, tmp_path: Path, state: dict) -> Path:
        p = tmp_path / "state.json"
        p.write_text(json.dumps(state))
        return p

    def _write_events(self, tmp_path: Path, events: list[dict]) -> Path:
        p = tmp_path / "events.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return p

    def test_full_output_structure(self, tmp_path: Path) -> None:
        sp = self._write_state(tmp_path, SAMPLE_STATE)
        ep = self._write_events(tmp_path, SAMPLE_EVENTS)
        result = build_context(str(sp), str(ep), lookback_hours=48)
        assert "HomeOps HVAC Context Summary" in result
        assert "CURRENT CONDITIONS" in result
        assert "TODAY" in result
        assert "RECENT HEATING SESSIONS" in result
        assert "RECENT WARNINGS" in result

    def test_missing_state_handled_gracefully(self, tmp_path: Path) -> None:
        ep = self._write_events(tmp_path, SAMPLE_EVENTS)
        result = build_context(str(tmp_path / "missing.json"), str(ep), lookback_hours=48)
        assert "state.json not available" in result

    def test_missing_events_handled_gracefully(self, tmp_path: Path) -> None:
        sp = self._write_state(tmp_path, SAMPLE_STATE)
        result = build_context(str(sp), str(tmp_path / "missing.jsonl"), lookback_hours=48)
        assert "HomeOps HVAC Context Summary" in result
        assert "No sessions in lookback window" in result

    def test_output_is_string(self, tmp_path: Path) -> None:
        sp = self._write_state(tmp_path, SAMPLE_STATE)
        ep = self._write_events(tmp_path, [])
        result = build_context(str(sp), str(ep))
        assert isinstance(result, str)

    def test_output_under_token_limit(self, tmp_path: Path) -> None:
        """Rough check: output stays under ~2000 tokens (8000 chars)."""
        sp = self._write_state(tmp_path, SAMPLE_STATE)
        ep = self._write_events(tmp_path, SAMPLE_EVENTS)
        result = build_context(str(sp), str(ep), lookback_hours=48)
        assert len(result) < 8000

    def test_lookback_hours_respected(self, tmp_path: Path) -> None:
        sp = self._write_state(tmp_path, SAMPLE_STATE)
        now = datetime.now(UTC)
        # Session from 1 hour ago
        recent = {
            "schema": "homeops.consumer.heating_session_ended.v1",
            "ts": (now - timedelta(hours=1)).isoformat(),
            "data": {"duration_s": 300, "outdoor_temp_f": 60.0},
        }
        # Session from 72 hours ago
        old = {
            "schema": "homeops.consumer.heating_session_ended.v1",
            "ts": (now - timedelta(hours=72)).isoformat(),
            "data": {"duration_s": 300, "outdoor_temp_f": 60.0},
        }
        ep = self._write_events(tmp_path, [recent, old])
        result_24h = build_context(str(sp), str(ep), lookback_hours=24)
        result_96h = build_context(str(sp), str(ep), lookback_hours=96)
        # 96h window picks up both; 24h only picks up the recent one
        assert result_96h.count("UTC:") > result_24h.count("UTC:")

    def test_yesterday_daily_summary_included(self, tmp_path: Path) -> None:
        """Yesterday's daily summary appears even with short lookback window."""
        sp = self._write_state(tmp_path, SAMPLE_STATE)
        ep = self._write_events(tmp_path, SAMPLE_EVENTS)
        # 1h lookback — only events from last 1 hour — but yesterday summary should still appear
        result = build_context(str(sp), str(ep), lookback_hours=1)
        assert "YESTERDAY" in result
