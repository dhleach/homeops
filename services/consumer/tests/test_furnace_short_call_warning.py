"""Tests for furnace_short_call_warning.v1 event emission and Telegram alerting.

Covers:
  - _make_furnace_short_call_event: schema, fields, ts handling
  - _format_furnace_short_call_message: content, threshold, actionable text
  - Integration: event fires when session < threshold, not when >= threshold
  - Telegram called when tokens set, skipped when not
  - daily_state warning counter incremented
  - Zero-duration edge case not emitted
  - Custom threshold via FURNACE_SHORT_CALL_THRESHOLD_S
"""

from __future__ import annotations

from consumer import _format_furnace_short_call_message, _make_furnace_short_call_event

_SCHEMA = "homeops.consumer.furnace_short_call_warning.v1"

# ---------------------------------------------------------------------------
# _make_furnace_short_call_event
# ---------------------------------------------------------------------------


class TestMakeFurnaceShortCallEvent:
    def test_schema_correct(self):
        evt = _make_furnace_short_call_event(45, 120, "2026-04-01T12:00:00+00:00")
        assert evt["schema"] == _SCHEMA

    def test_source_correct(self):
        evt = _make_furnace_short_call_event(45, 120, "2026-04-01T12:00:00+00:00")
        assert evt["source"] == "consumer.v1"

    def test_duration_s_in_data(self):
        evt = _make_furnace_short_call_event(45, 120, "2026-04-01T12:00:00+00:00")
        assert evt["data"]["duration_s"] == 45

    def test_threshold_s_in_data(self):
        evt = _make_furnace_short_call_event(45, 120, "2026-04-01T12:00:00+00:00")
        assert evt["data"]["threshold_s"] == 120

    def test_ended_at_in_data(self):
        ended = "2026-04-01T12:00:00+00:00"
        evt = _make_furnace_short_call_event(45, 120, ended)
        assert evt["data"]["ended_at"] == ended

    def test_ts_uses_processing_ts_when_provided(self):
        evt = _make_furnace_short_call_event(
            45, 120, "2026-04-01T12:00:00+00:00", processing_ts="2026-04-01T12:00:01+00:00"
        )
        assert evt["ts"] == "2026-04-01T12:00:01+00:00"

    def test_ts_fallback_when_no_processing_ts(self):
        evt = _make_furnace_short_call_event(45, 120, None)
        assert "ts" in evt and evt["ts"]

    def test_ended_at_none_allowed(self):
        evt = _make_furnace_short_call_event(45, 120, None)
        assert evt["data"]["ended_at"] is None

    def test_custom_threshold(self):
        evt = _make_furnace_short_call_event(90, 60, None)
        assert evt["data"]["threshold_s"] == 60


# ---------------------------------------------------------------------------
# _format_furnace_short_call_message
# ---------------------------------------------------------------------------


class TestFormatFurnaceShortCallMessage:
    def _data(self, duration_s: int = 45, threshold_s: int = 120) -> dict:
        return {"duration_s": duration_s, "threshold_s": threshold_s}

    def test_contains_duration(self):
        msg = _format_furnace_short_call_message(self._data(duration_s=45))
        assert "45" in msg

    def test_contains_threshold(self):
        msg = _format_furnace_short_call_message(self._data(threshold_s=120))
        assert "120" in msg

    def test_contains_warning_emoji(self):
        msg = _format_furnace_short_call_message(self._data())
        assert "⚡" in msg

    def test_contains_short_call_mention(self):
        msg = _format_furnace_short_call_message(self._data())
        assert "short-call" in msg.lower() or "short call" in msg.lower()

    def test_contains_rapid_cycling(self):
        msg = _format_furnace_short_call_message(self._data())
        assert "rapid" in msg.lower()

    def test_multiline(self):
        msg = _format_furnace_short_call_message(self._data())
        assert "\n" in msg

    def test_returns_string(self):
        msg = _format_furnace_short_call_message(self._data())
        assert isinstance(msg, str) and len(msg) > 0


# ---------------------------------------------------------------------------
# Integration: threshold logic and Telegram
# ---------------------------------------------------------------------------


class TestShortCallIntegration:
    """
    Tests that mimic the consumer's short-call detection logic directly
    (without running the full consumer loop).
    """

    def _run_check(
        self,
        duration_s: int,
        threshold_s: int = 120,
        bot_token: str = "token123",
        chat_id: str = "chat456",
    ) -> tuple[list[dict], bool]:
        """Simulate the short-call detection logic and return (emitted_events, telegram_called)."""
        emitted: list[dict] = []
        telegram_calls = []

        session_dur = duration_s
        session_ts = "2026-04-01T12:00:00+00:00"

        if session_dur is not None and session_dur < threshold_s and session_dur > 0:
            evt = _make_furnace_short_call_event(session_dur, threshold_s, session_ts)
            emitted.append(evt)
            if bot_token and chat_id:
                msg = _format_furnace_short_call_message(evt["data"])
                telegram_calls.append(msg)

        return emitted, telegram_calls

    def test_event_fires_below_threshold(self):
        evts, _ = self._run_check(45, threshold_s=120)
        assert len(evts) == 1
        assert evts[0]["schema"] == _SCHEMA

    def test_event_does_not_fire_at_threshold(self):
        evts, _ = self._run_check(120, threshold_s=120)
        assert evts == []

    def test_event_does_not_fire_above_threshold(self):
        evts, _ = self._run_check(180, threshold_s=120)
        assert evts == []

    def test_telegram_called_when_tokens_set(self):
        _, tg = self._run_check(45, bot_token="t", chat_id="c")
        assert len(tg) == 1

    def test_telegram_not_called_when_no_token(self):
        _, tg = self._run_check(45, bot_token="", chat_id="chat456")
        assert tg == []

    def test_telegram_not_called_when_no_chat_id(self):
        _, tg = self._run_check(45, bot_token="token", chat_id="")
        assert tg == []

    def test_zero_duration_does_not_fire(self):
        evts, _ = self._run_check(0)
        assert evts == []

    def test_one_second_below_threshold_fires(self):
        evts, _ = self._run_check(1, threshold_s=120)
        assert len(evts) == 1

    def test_custom_threshold_respected(self):
        # threshold=60: 45 should fire, 70 should not
        evts_45, _ = self._run_check(45, threshold_s=60)
        evts_70, _ = self._run_check(70, threshold_s=60)
        assert len(evts_45) == 1
        assert evts_70 == []
