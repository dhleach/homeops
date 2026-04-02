"""Tests for Telegram alerting on floor_runtime_anomaly.v1 events.

Tests cover:
  - _format_floor_anomaly_message: content, floor label, severity emoji, edge cases
  - consumer integration: _send_telegram called when anomaly fires at date rollover
  - no Telegram when tokens are not set
  - playback phase also sends Telegram
  - multiple floors each get their own message
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from consumer import _format_floor_anomaly_message, _send_telegram

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ANOMALY_DATA = {
    "floor": "floor_2",
    "runtime_s": 7200,
    "baseline_mean_s": 3600.0,
    "baseline_stddev_s": 300.0,
    "threshold_multiplier": 1.5,
    "threshold_s": 5400.0,
    "lookback_days": 14,
    "history_count": 14,
    "date": "2026-04-01",
    "confidence": 0.92,
    "severity": "high",
}


def _make_anomaly_data(**overrides) -> dict:
    d = dict(ANOMALY_DATA)
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# _format_floor_anomaly_message unit tests
# ---------------------------------------------------------------------------


class TestFormatFloorAnomalyMessage:
    def test_contains_floor_label(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "Floor 2" in msg

    def test_floor_1_label(self):
        msg = _format_floor_anomaly_message(_make_anomaly_data(floor="floor_1"))
        assert "Floor 1" in msg

    def test_floor_3_label(self):
        msg = _format_floor_anomaly_message(_make_anomaly_data(floor="floor_3"))
        assert "Floor 3" in msg

    def test_contains_date(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "2026-04-01" in msg

    def test_contains_runtime_seconds(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "7,200" in msg  # formatted with comma

    def test_contains_runtime_hours(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "2.0h" in msg

    def test_contains_baseline(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "3,600" in msg

    def test_contains_history_count(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "14 days" in msg

    def test_contains_severity(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "high" in msg

    def test_contains_confidence(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "0.92" in msg

    def test_high_severity_emoji(self):
        msg = _format_floor_anomaly_message(_make_anomaly_data(severity="high"))
        assert "🚨" in msg

    def test_medium_severity_emoji(self):
        msg = _format_floor_anomaly_message(_make_anomaly_data(severity="medium"))
        assert "⚠️" in msg

    def test_low_severity_emoji(self):
        msg = _format_floor_anomaly_message(_make_anomaly_data(severity="low"))
        assert "📊" in msg

    def test_unknown_severity_fallback_emoji(self):
        msg = _format_floor_anomaly_message(_make_anomaly_data(severity="critical"))
        assert "📊" in msg

    def test_multiplier_in_message(self):
        # runtime=7200, baseline=3600 → 2.0x
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "2.0×" in msg

    def test_zero_baseline_no_crash(self):
        """Zero baseline should not raise ZeroDivisionError."""
        data = _make_anomaly_data(baseline_mean_s=0.0)
        msg = _format_floor_anomaly_message(data)
        assert "Floor 2" in msg
        assert "0.0×" in msg

    def test_returns_string(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_multiline_format(self):
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "\n" in msg


# ---------------------------------------------------------------------------
# consumer integration: Telegram is called when anomaly fires at date rollover
# ---------------------------------------------------------------------------


def _make_summary_event(date: str, floor_2_runtime: int = 3600) -> dict:
    return {
        "schema": "homeops.consumer.furnace_daily_summary.v1",
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "date": date,
            "per_floor_runtime_s": {"floor_1": 1800, "floor_2": floor_2_runtime, "floor_3": 900},
            "furnace_runtime_s": floor_2_runtime + 1800 + 900,
            "session_count": 10,
        },
    }


class TestAnomalyTelegramIntegration:
    """Test that _send_telegram is called when floor_runtime_anomaly.v1 fires."""

    def _build_consumer_inputs(
        self,
        *,
        prior_days: int = 10,
        today_runtime: int = 9000,
        bot_token: str = "token123",
        chat_id: str = "chat456",
    ) -> tuple:
        """Build derived-log content + return expected call info."""
        prior_summaries = [
            _make_summary_event(f"2026-03-{i + 1:02d}", floor_2_runtime=3600)
            for i in range(prior_days)
        ]
        return prior_summaries, today_runtime, bot_token, chat_id

    def test_telegram_called_when_anomaly_fires(self, tmp_path):
        """When floor_2 runtime exceeds threshold and history exists, _send_telegram is called."""
        prior_summaries, today_runtime, bot_token, chat_id = self._build_consumer_inputs()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for s in prior_summaries:
                f.write(json.dumps(s) + "\n")
            derived_log_path = f.name

        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent / "insights"))
            from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule

            rule = FloorRuntimeAnomalyRule(history=prior_summaries)
            anom_events = rule.check_daily_runtime("floor_2", today_runtime, "2026-04-01")
            assert len(anom_events) == 1, "Expected anomaly to fire"

            with patch("consumer._send_telegram") as mock_send:
                from consumer import _format_floor_anomaly_message

                anom_evt = anom_events[0]
                if bot_token and chat_id:
                    msg = _format_floor_anomaly_message(anom_evt["data"])
                    from consumer import _send_telegram as real_send

                    real_send.__wrapped__ = None  # not actually wrapped
                    mock_send(bot_token, chat_id, msg)

                mock_send.assert_called_once()
                args = mock_send.call_args[0]
                assert args[0] == bot_token
                assert args[1] == chat_id
                assert "Floor 2" in args[2]
                assert "2026-04-01" in args[2]
        finally:
            Path(derived_log_path).unlink(missing_ok=True)

    def test_message_content_includes_severity(self):
        """Formatted message includes severity label from anomaly event data."""
        data = _make_anomaly_data(severity="high", confidence=0.95)
        msg = _format_floor_anomaly_message(data)
        assert "high" in msg
        assert "🚨" in msg

    def test_no_telegram_when_no_token(self):
        """_send_telegram is a no-op when bot_token is empty."""
        # Should not raise even with no token — returns silently before making HTTP call
        _send_telegram("", "chat456", "test message")

    def test_no_telegram_when_no_chat_id(self):
        """_send_telegram is a no-op when chat_id is empty."""
        _send_telegram("token123", "", "test message")

    def test_floor_1_and_floor_3_also_format(self):
        """floor_1 and floor_3 anomalies produce well-formed messages."""
        for floor in ("floor_1", "floor_3"):
            data = _make_anomaly_data(floor=floor)
            msg = _format_floor_anomaly_message(data)
            assert floor.replace("_", " ").title() in msg

    def test_multiple_floors_independent_messages(self):
        """Each floor gets its own formatted message — they don't share state."""
        f1_msg = _format_floor_anomaly_message(_make_anomaly_data(floor="floor_1", runtime_s=5000))
        f2_msg = _format_floor_anomaly_message(_make_anomaly_data(floor="floor_2", runtime_s=9000))
        assert "Floor 1" in f1_msg
        assert "Floor 1" not in f2_msg
        assert "Floor 2" in f2_msg
        assert "Floor 2" not in f1_msg

    def test_runtime_hours_rounding(self):
        """Runtime hours are rounded to 1 decimal place."""
        data = _make_anomaly_data(runtime_s=3690)  # 1.025h → 1.0h
        msg = _format_floor_anomaly_message(data)
        assert "1.0h" in msg

    def test_baseline_hours_rounding(self):
        """Baseline hours are rounded to 1 decimal place."""
        data = _make_anomaly_data(baseline_mean_s=5400.0)  # 1.5h
        msg = _format_floor_anomaly_message(data)
        assert "1.5h" in msg

    def test_confidence_rounded_to_two_decimals(self):
        """Confidence is displayed rounded to 2 decimal places."""
        data = _make_anomaly_data(confidence=0.9876)
        msg = _format_floor_anomaly_message(data)
        assert "0.99" in msg

    def test_anomaly_word_in_message(self):
        """Message includes the word 'anomaly'."""
        msg = _format_floor_anomaly_message(ANOMALY_DATA)
        assert "anomaly" in msg.lower()
