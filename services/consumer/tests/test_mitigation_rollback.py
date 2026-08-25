"""Tests for durable automatic mitigation rollback handling.

Revision history:
  2026-08-25  Cover rollback persistence, replay deduplication, urgent alert
              formatting, and notification failure isolation.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from consumer import (
    _format_mitigation_rollback_message,
    _process_mitigation_observer_event,
    _send_telegram,
)

from processors import MITIGATION_ROLLBACK_EVENT_TYPE

TS = "2026-08-25T13:00:00+00:00"


def _rollback_event(**overrides):
    event_data = {
        "event_type": MITIGATION_ROLLBACK_EVENT_TYPE,
        "incident_id": "0123456789abcdef",
        "failed_attempts": 3,
        "reason": "short_cycle_after_three_mitigation_attempts",
        "trigger_event_id": "fedcba9876543210",
        "storm_window_started_at": "2026-08-25T12:00:00+00:00",
        "mitigation_enabled": False,
        "rollback_state": "rolled_back",
        "source_event_type": "homeops.mitigation.short_cycle_detected.v1",
    }
    event_data.update(overrides)
    return {
        "schema": "homeops.observer.event.v1",
        "source": "ha.websocket",
        "ts": TS,
        "data": {
            "event_type": MITIGATION_ROLLBACK_EVENT_TYPE,
            "event_data": event_data,
        },
    }


def test_rollback_event_is_appended_and_alerted_once(tmp_path):
    derived_log = tmp_path / "derived.jsonl"
    event = _rollback_event()

    with patch("consumer._send_telegram") as send:
        first = _process_mitigation_observer_event(
            event,
            str(derived_log),
            False,
            telegram_bot_token="bot-token",
            telegram_chat_id="chat-id",
        )
        second = _process_mitigation_observer_event(
            event,
            str(derived_log),
            first[0],
            telegram_bot_token="bot-token",
            telegram_chat_id="chat-id",
        )

    assert first == (False, True)
    assert second == (False, False)
    events = [json.loads(line) for line in derived_log.read_text().splitlines()]
    assert [event["schema"] for event in events] == [MITIGATION_ROLLBACK_EVENT_TYPE]
    send.assert_called_once()
    assert "URGENT" in send.call_args.args[2]
    assert "0123456789abcdef" in send.call_args.args[2]


def test_rollback_alert_contains_operator_diagnostics():
    message = _format_mitigation_rollback_message(_rollback_event()["data"]["event_data"])

    assert "mitigation rolled back" in message
    assert "3 failed attempts" in message
    assert "short_cycle_after_three_mitigation_attempts" in message
    assert "Mitigation guard: OFF" in message


def test_rollback_notification_failure_does_not_raise():
    with patch("urllib.request.urlopen", side_effect=OSError("Telegram unavailable")):
        _send_telegram("bot-token", "chat-id", "rollback")


def test_malformed_rollback_is_not_emitted_or_alerted(tmp_path):
    event = _rollback_event(failed_attempts=2)
    derived_log = tmp_path / "derived.jsonl"

    with patch("consumer._send_telegram") as send:
        result = _process_mitigation_observer_event(
            event,
            str(derived_log),
            False,
            telegram_bot_token="bot-token",
            telegram_chat_id="chat-id",
        )

    assert result == (False, False)
    assert not derived_log.exists()
    send.assert_not_called()
