"""
Tests for telegram_commands.py — summary formatting and command handler logic.

Telegram API calls (get_updates, send_message) are mocked throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from telegram_commands import (
    _fmt_duration,
    format_status_summary,
    handle_telegram_commands,
)

# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_zero_seconds(self) -> None:
        assert _fmt_duration(0) == "0m"

    def test_negative_seconds(self) -> None:
        assert _fmt_duration(-10) == "0m"

    def test_minutes_only(self) -> None:
        assert _fmt_duration(600) == "10m"

    def test_exactly_one_hour(self) -> None:
        assert _fmt_duration(3600) == "1h 0m"

    def test_hours_and_minutes(self) -> None:
        assert _fmt_duration(4320) == "1h 12m"

    def test_two_hours_thirty_minutes(self) -> None:
        assert _fmt_duration(9000) == "2h 30m"

    def test_seconds_truncated(self) -> None:
        # 125 s = 2m 5s → shows as "2m"
        assert _fmt_duration(125) == "2m"

    def test_just_under_one_hour(self) -> None:
        assert _fmt_duration(3599) == "59m"


# ---------------------------------------------------------------------------
# format_status_summary
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 31, 20, 45, 0, tzinfo=UTC)

_CLIMATE_STATE_ALL_IDLE = {
    "climate.floor_1_thermostat": {
        "current_temp": 68.0,
        "setpoint": 68.0,
        "hvac_action": "idle",
    },
    "climate.floor_2_thermostat": {
        "current_temp": 67.0,
        "setpoint": 70.0,
        "hvac_action": "idle",
    },
    "climate.floor_3_thermostat": {
        "current_temp": 72.0,
        "setpoint": 68.0,
        "hvac_action": "idle",
    },
}

_FLOOR_ON_SINCE_ALL_IDLE = {
    "binary_sensor.floor_1_heating_call": None,
    "binary_sensor.floor_2_heating_call": None,
    "binary_sensor.floor_3_heating_call": None,
}

_DAILY_STATE_BASIC = {
    "furnace_runtime_s": 4320,  # 1h 12m
    "session_count": 3,
    "floor_runtime_s": {
        "binary_sensor.floor_1_heating_call": 4320,
        "binary_sensor.floor_2_heating_call": 3120,
        "binary_sensor.floor_3_heating_call": 480,
    },
    "last_outdoor_temp_f": 35.0,
}


class TestFormatStatusSummary:
    def test_header_contains_time(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "HomeOps Status" in msg
        assert "8:45 PM" in msg

    def test_furnace_off(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "💤 Furnace: OFF" in msg
        assert "🔥" not in msg

    def test_furnace_on_running_duration(self) -> None:
        furnace_on_since = _NOW - timedelta(minutes=8)
        msg = format_status_summary(
            furnace_on_since=furnace_on_since,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "🔥 Furnace: ON (running 8 min)" in msg

    def test_outdoor_temp_shown(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "🌡️ Outdoor: 35°F" in msg

    def test_outdoor_temp_omitted_when_none(self) -> None:
        daily_no_outdoor = {**_DAILY_STATE_BASIC, "last_outdoor_temp_f": None}
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=daily_no_outdoor,
            now=_NOW,
        )
        assert "Outdoor" not in msg

    def test_floor_temps_and_setpoints(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "Floor 1: 68°F → 68°F setpoint (idle)" in msg
        assert "Floor 2: 67°F → 70°F setpoint (idle)" in msg
        assert "Floor 3: 72°F → 68°F setpoint (idle)" in msg

    def test_floor_heating_action(self) -> None:
        climate_heating = {
            **_CLIMATE_STATE_ALL_IDLE,
            "climate.floor_1_thermostat": {
                "current_temp": 67.0,
                "setpoint": 70.0,
                "hvac_action": "heating",
            },
        }
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=climate_heating,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "Floor 1: 67°F → 70°F setpoint (heating)" in msg

    def test_floor_runtime_today(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "today: 1h 12m" in msg  # floor 1: 4320s
        assert "today: 52m" in msg  # floor 2: 3120s
        assert "today: 8m" in msg  # floor 3: 480s

    def test_daily_totals_furnace_off(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "📊 Today: 3 sessions, 1h 12m total" in msg

    def test_daily_totals_furnace_on_adds_session_and_runtime(self) -> None:
        furnace_on_since = _NOW - timedelta(minutes=15)
        msg = format_status_summary(
            furnace_on_since=furnace_on_since,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        # 3 completed + 1 active = 4 sessions; 4320 + 900 = 5220s = 1h 27m
        assert "4 sessions" in msg
        assert "1h 27m total" in msg

    def test_floor_2_warn_flag_when_active(self) -> None:
        floor_on_since_f2_active = {
            **_FLOOR_ON_SINCE_ALL_IDLE,
            "binary_sensor.floor_2_heating_call": _NOW - timedelta(minutes=50),
        }
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=floor_on_since_f2_active,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        # Floor 2 active → ⚠️ flag
        assert "Floor 2" in msg
        assert "⚠️" in msg

    def test_floor_2_warn_flag_not_shown_when_idle(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "⚠️" not in msg

    def test_in_flight_floor_runtime_added(self) -> None:
        floor_on_since = {
            **_FLOOR_ON_SINCE_ALL_IDLE,
            "binary_sensor.floor_1_heating_call": _NOW - timedelta(minutes=10),
        }
        daily_no_floor_runtime = {
            **_DAILY_STATE_BASIC,
            "floor_runtime_s": {},
        }
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=floor_on_since,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=daily_no_floor_runtime,
            now=_NOW,
        )
        # 10 min in-flight should appear in today's runtime
        assert "today: 10m" in msg

    def test_empty_climate_state_shows_question_marks(self) -> None:
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state={},
            daily_state=_DAILY_STATE_BASIC,
            now=_NOW,
        )
        assert "?°F" in msg

    def test_singular_session_label(self) -> None:
        daily_one_session = {**_DAILY_STATE_BASIC, "session_count": 1}
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=daily_one_session,
            now=_NOW,
        )
        assert "1 session," in msg
        assert "sessions" not in msg

    def test_defaults_now_to_utc_when_none(self) -> None:
        """Should not raise even when now=None (uses datetime.now(UTC))."""
        msg = format_status_summary(
            furnace_on_since=None,
            floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
            climate_state=_CLIMATE_STATE_ALL_IDLE,
            daily_state=_DAILY_STATE_BASIC,
            now=None,
        )
        assert "HomeOps Status" in msg


# ---------------------------------------------------------------------------
# handle_telegram_commands
# ---------------------------------------------------------------------------


def _make_update(update_id: int, text: str, chat_id: str = "12345") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


class TestHandleTelegramCommands:
    """Tests for the command handler — Telegram API calls are mocked."""

    def _call(
        self,
        updates: list[dict],
        last_update_id: int | None = None,
        chat_id: str = "12345",
        bot_token: str = "tok",
    ) -> tuple[int | None, list[str]]:
        """Helper: patch get_updates + send_message, call handle_telegram_commands."""
        sent: list[str] = []

        def _capture_send(t: str, c: str, m: str) -> bool:
            sent.append(m)
            return True

        with (
            patch("telegram_commands.get_updates", return_value=updates),
            patch("telegram_commands.send_message", side_effect=_capture_send),
        ):
            new_id = handle_telegram_commands(
                bot_token=bot_token,
                chat_id=chat_id,
                last_update_id=last_update_id,
                furnace_on_since=None,
                floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
                climate_state=_CLIMATE_STATE_ALL_IDLE,
                daily_state=_DAILY_STATE_BASIC,
                now=_NOW,
            )
        return new_id, sent

    def test_no_updates_returns_same_id(self) -> None:
        new_id, sent = self._call([], last_update_id=42)
        assert new_id == 42
        assert sent == []

    def test_summary_command_sends_message(self) -> None:
        updates = [_make_update(100, "/summary")]
        new_id, sent = self._call(updates, last_update_id=99)
        assert new_id == 100
        assert len(sent) == 1
        assert "HomeOps Status" in sent[0]

    def test_summary_command_with_args_still_handled(self) -> None:
        updates = [_make_update(101, "/summary extra")]
        _, sent = self._call(updates)
        assert len(sent) == 1

    def test_unknown_command_ignored(self) -> None:
        updates = [_make_update(102, "/help")]
        new_id, sent = self._call(updates, last_update_id=None)
        assert new_id == 102
        assert sent == []

    def test_plain_text_ignored(self) -> None:
        updates = [_make_update(103, "hello there")]
        _, sent = self._call(updates)
        assert sent == []

    def test_wrong_chat_id_ignored(self) -> None:
        updates = [_make_update(104, "/summary", chat_id="99999")]
        _, sent = self._call(updates, chat_id="12345")
        assert sent == []

    def test_update_id_advances_on_non_command(self) -> None:
        updates = [_make_update(200, "hi")]
        new_id, _ = self._call(updates, last_update_id=150)
        assert new_id == 200

    def test_no_bot_token_skips_entirely(self) -> None:
        with patch("telegram_commands.get_updates") as mock_get:
            new_id = handle_telegram_commands(
                bot_token="",
                chat_id="12345",
                last_update_id=None,
                furnace_on_since=None,
                floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
                climate_state=_CLIMATE_STATE_ALL_IDLE,
                daily_state=_DAILY_STATE_BASIC,
                now=_NOW,
            )
        mock_get.assert_not_called()
        assert new_id is None

    def test_offset_passed_as_last_id_plus_one(self) -> None:
        with (
            patch("telegram_commands.get_updates", return_value=[]) as mock_get,
            patch("telegram_commands.send_message"),
        ):
            handle_telegram_commands(
                bot_token="tok",
                chat_id="12345",
                last_update_id=50,
                furnace_on_since=None,
                floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
                climate_state=_CLIMATE_STATE_ALL_IDLE,
                daily_state=_DAILY_STATE_BASIC,
                now=_NOW,
            )
        mock_get.assert_called_once_with("tok", offset=51, timeout=0)

    def test_first_call_no_offset(self) -> None:
        with (
            patch("telegram_commands.get_updates", return_value=[]) as mock_get,
            patch("telegram_commands.send_message"),
        ):
            handle_telegram_commands(
                bot_token="tok",
                chat_id="12345",
                last_update_id=None,
                furnace_on_since=None,
                floor_on_since=_FLOOR_ON_SINCE_ALL_IDLE,
                climate_state=_CLIMATE_STATE_ALL_IDLE,
                daily_state=_DAILY_STATE_BASIC,
                now=_NOW,
            )
        mock_get.assert_called_once_with("tok", offset=None, timeout=0)

    def test_multiple_updates_highest_id_returned(self) -> None:
        updates = [
            _make_update(10, "hi"),
            _make_update(15, "/summary"),
            _make_update(20, "bye"),
        ]
        new_id, sent = self._call(updates)
        assert new_id == 20
        assert len(sent) == 1  # only /summary triggers a reply
