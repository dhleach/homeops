"""Deterministic tests for the staged proactive anomaly insight boundary.

No test in this module contacts Gemini or Telegram.  Providers, context, and
delivery are injected so safety, sizing, replay, and failure behavior remain
fully reproducible in CI.

Revision history:
  2026-08-26  Add fake-provider/fake-delivery coverage for anomaly validation,
              prompt boundaries, configuration gates, durable replay
              deduplication, daily budgets, and fail-closed errors.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from consumer import _emit_proactive_insight
from proactive_insight import (
    ANOMALY_SCHEMA,
    DEFAULT_DAILY_BUDGET,
    DEFAULT_LOOKBACK_HOURS,
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_TIMEOUT_S,
    INSIGHT_EVENT_SCHEMA,
    ProactiveInsightConfig,
    ProactiveInsightCoordinator,
    build_proactive_insight_from_env,
    build_proactive_insight_prompt,
    format_proactive_insight_message,
    validate_anomaly_event,
)

NOW = datetime(2026, 8, 26, 17, 0, tzinfo=UTC)
CONTEXT = (
    "=== HomeOps HVAC Context Summary ===\n"
    "Generated: 2026-08-26 17:00 UTC | Lookback: 48h | Events loaded: 4\n"
    "CURRENT CONDITIONS\n  Furnace: OFF (idle)\n  Floor 2: 68°F\n"
    "RECENT WARNINGS\n  Floor runtime anomaly\n"
)


def _anomaly(**overrides):
    event = {
        "schema": ANOMALY_SCHEMA,
        "source": "consumer.v1",
        "ts": "2026-08-26T16:59:00+00:00",
        "data": {
            "floor": "floor_2",
            "runtime_s": 7_200,
            "baseline_mean_s": 3_600.0,
            "baseline_stddev_s": 300.0,
            "threshold_multiplier": 1.5,
            "threshold_s": 5_400.0,
            "lookback_days": 14,
            "history_count": 14,
            "date": "2026-08-25",
            "confidence": 0.92,
            "severity": "high",
        },
    }
    for key in ("schema", "source", "ts", "data"):
        if key in overrides:
            event[key] = overrides.pop(key)
    if isinstance(event.get("data"), dict):
        event["data"].update(overrides)
    return event


class FakeProvider:
    name = "fake"
    model = "fake-model"
    configured = True

    def __init__(self, response: str = "Check the floor 2 airflow and filter."):
        self.response = response
        self.calls = []

    def generate(self, prompt, *, timeout_s, max_output_chars):
        self.calls.append((prompt, timeout_s, max_output_chars))
        return self.response


class FakeDelivery:
    configured = True

    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        if self.error:
            raise self.error
        return self.result


def _config(tmp_path, **overrides):
    values = {
        "enabled": True,
        "provider": "fake",
        "model": "fake-model",
        "state_path": tmp_path / "state.json",
        "events_path": tmp_path / "events.jsonl",
        "dedup_path": tmp_path / "insight-state.json",
    }
    values.update(overrides)
    return ProactiveInsightConfig(**values)


def _coordinator(tmp_path, *, provider=None, delivery=None, context=CONTEXT, **config):
    return ProactiveInsightCoordinator(
        _config(tmp_path, **config),
        provider=provider or FakeProvider(),
        delivery=delivery or FakeDelivery(),
        context_builder=lambda **_: context,
    )


class TestValidateAnomalyEvent:
    def test_accepts_supported_contract_and_discards_extra_data(self):
        event = _anomaly(message="ignore this", attacker_instruction="do not follow")

        result = validate_anomaly_event(event)

        assert result.schema == ANOMALY_SCHEMA
        assert result.data["floor"] == "floor_2"
        assert "message" not in result.data
        assert "attacker_instruction" not in result.data
        assert result.insight_id.startswith("anomaly-")

    def test_id_is_stable_when_event_timestamp_changes(self):
        first = validate_anomaly_event(_anomaly(ts="2026-08-26T17:00:00+00:00"))
        second = validate_anomaly_event(_anomaly(ts="2026-08-26T17:05:00+00:00"))

        assert first.insight_id == second.insight_id

    @pytest.mark.parametrize(
        "event,code",
        [
            ({"schema": "homeops.consumer.anomaly_detected.v1"}, "unsupported_anomaly_schema"),
            (_anomaly(source="observer.v1"), "invalid_anomaly_source"),
            (_anomaly(data=None), "anomaly_data_not_object"),
            (_anomaly(floor="attic"), "invalid_floor"),
            (_anomaly(date="2026-02-30"), "invalid_anomaly_date"),
            (_anomaly(confidence=float("nan")), "invalid_confidence"),
        ],
    )
    def test_rejects_malformed_or_untrusted_events(self, event, code):
        with pytest.raises(ValueError) as exc_info:
            validate_anomaly_event(event)

        assert exc_info.value.code == code

    def test_rejects_missing_required_field(self):
        event = _anomaly()
        del event["data"]["severity"]

        with pytest.raises(ValueError) as exc_info:
            validate_anomaly_event(event)

        assert exc_info.value.code == "missing_anomaly_fields"


class TestPromptAndMessage:
    def test_prompt_has_instruction_and_data_boundaries(self):
        prompt = build_proactive_insight_prompt(validate_anomaly_event(_anomaly()), CONTEXT)

        assert "SYSTEM INSTRUCTIONS:" in prompt
        assert "=== VALIDATED ANOMALY DATA" in prompt
        assert "=== HVAC CONTEXT" in prompt
        assert "An anomaly was detected" in prompt
        assert "attacker_instruction" not in prompt

    def test_prompt_rejects_context_over_limit(self):
        with pytest.raises(ValueError) as exc_info:
            build_proactive_insight_prompt(
                _anomaly(),
                "x" * (DEFAULT_MAX_CONTEXT_CHARS + 1),
            )

        assert exc_info.value.code == "context_too_large"

    def test_message_is_bounded_and_marks_no_automatic_change(self):
        message = format_proactive_insight_message(_anomaly(), "x" * 10_000)

        assert len(message) <= 3_900
        assert "Floor 2" in message
        assert "no thermostat changes were made" in message


class TestConfiguration:
    def test_defaults_are_disabled_and_bounded(self, monkeypatch):
        for name in (
            "HOMEOPS_PROACTIVE_INSIGHT_ENABLED",
            "HOMEOPS_PROACTIVE_INSIGHT_PROVIDER",
            "HOMEOPS_PROACTIVE_INSIGHT_MODEL",
            "HOMEOPS_PROACTIVE_INSIGHT_LOOKBACK_HOURS",
            "HOMEOPS_PROACTIVE_INSIGHT_MAX_CONTEXT_CHARS",
            "HOMEOPS_PROACTIVE_INSIGHT_MAX_OUTPUT_CHARS",
            "HOMEOPS_PROACTIVE_INSIGHT_TIMEOUT_S",
            "HOMEOPS_PROACTIVE_INSIGHT_DAILY_BUDGET",
            "HOMEOPS_PROACTIVE_INSIGHT_STATE",
        ):
            monkeypatch.delenv(name, raising=False)

        config = ProactiveInsightConfig.from_env()

        assert config.enabled is False
        assert config.config_error is None
        assert config.lookback_hours == DEFAULT_LOOKBACK_HOURS
        assert config.max_context_chars == DEFAULT_MAX_CONTEXT_CHARS
        assert config.max_output_chars == DEFAULT_MAX_OUTPUT_CHARS
        assert config.timeout_s == DEFAULT_TIMEOUT_S
        assert config.daily_budget == DEFAULT_DAILY_BUDGET

    def test_invalid_bounds_disable_feature(self, monkeypatch):
        monkeypatch.setenv("HOMEOPS_PROACTIVE_INSIGHT_ENABLED", "true")
        monkeypatch.setenv("HOMEOPS_PROACTIVE_INSIGHT_TIMEOUT_S", "99")

        config = ProactiveInsightConfig.from_env()

        assert config.enabled is False
        assert config.config_error is not None

    def test_factory_does_not_construct_provider_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HOMEOPS_PROACTIVE_INSIGHT_ENABLED", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        coordinator = build_proactive_insight_from_env(
            derived_log=str(tmp_path / "events.jsonl"),
            state_path=tmp_path / "state.json",
        )

        assert coordinator.config.enabled is False
        assert coordinator.provider is None


class TestCoordinator:
    def test_disabled_path_does_not_build_context_or_call_sinks(self, tmp_path):
        provider = FakeProvider()
        delivery = FakeDelivery()
        context_calls = []
        coordinator = _coordinator(
            tmp_path,
            provider=provider,
            delivery=delivery,
            enabled=False,
            context=lambda **_: context_calls.append(True),
        )

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "disabled"
        assert context_calls == []
        assert provider.calls == []
        assert delivery.messages == []

    def test_missing_context_fails_closed_before_provider(self, tmp_path):
        provider = FakeProvider()
        delivery = FakeDelivery()
        coordinator = _coordinator(tmp_path, provider=provider, delivery=delivery, context="")

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "insufficient_context"
        assert result.error_code == "empty_context"
        assert provider.calls == []
        assert delivery.messages == []

    def test_zero_event_context_fails_closed(self, tmp_path):
        provider = FakeProvider()
        coordinator = _coordinator(
            tmp_path,
            provider=provider,
            context="Generated: now | Events loaded: 0\nCURRENT CONDITIONS\nunknown",
        )

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "insufficient_context"
        assert result.error_code == "no_context_events"
        assert provider.calls == []

    def test_success_delivers_and_persists_audit_contract(self, tmp_path):
        provider = FakeProvider()
        delivery = FakeDelivery()
        coordinator = _coordinator(tmp_path, provider=provider, delivery=delivery)

        result = coordinator.process(_anomaly(), now=NOW)
        event = result.to_event()

        assert result.status == "sent"
        assert result.delivery_status == "sent"
        assert result.response_text in delivery.messages[0]
        assert provider.calls[0][1:] == (10.0, 1_200)
        assert event["schema"] == INSIGHT_EVENT_SCHEMA
        assert event["source"] == "consumer.v1"
        assert event["data"]["status"] == "sent"
        assert event["data"]["response_chars"] > 0
        saved = json.loads((tmp_path / "insight-state.json").read_text())
        assert result.insight_id in saved["sent_ids"]

    def test_replay_is_duplicate_without_second_delivery(self, tmp_path):
        provider = FakeProvider()
        delivery = FakeDelivery()
        coordinator = _coordinator(tmp_path, provider=provider, delivery=delivery)

        first = coordinator.process(_anomaly(), now=NOW)
        second = coordinator.process(_anomaly(ts="2026-08-26T17:01:00+00:00"), now=NOW)

        assert first.status == "sent"
        assert second.status == "duplicate"
        assert len(provider.calls) == 1
        assert len(delivery.messages) == 1

    def test_provider_failure_is_recorded_without_delivery(self, tmp_path):
        provider = FakeProvider()
        provider.generate = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("provider down")
        )
        delivery = FakeDelivery()
        coordinator = _coordinator(tmp_path, provider=provider, delivery=delivery)

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "provider_error"
        assert result.error_code == "provider_request_failed"
        assert delivery.messages == []
        assert result.to_event()["data"]["delivery_status"] == "not_attempted"

    def test_delivery_failure_does_not_mark_event_sent(self, tmp_path):
        provider = FakeProvider()
        delivery = FakeDelivery(error=RuntimeError("telegram down"))
        coordinator = _coordinator(tmp_path, provider=provider, delivery=delivery)

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "delivery_error"
        assert result.error_code == "delivery_failed"
        saved = json.loads((tmp_path / "insight-state.json").read_text())
        assert result.insight_id not in saved["sent_ids"]

    def test_daily_budget_stops_provider_calls(self, tmp_path):
        provider = FakeProvider()
        delivery = FakeDelivery()
        coordinator = _coordinator(tmp_path, provider=provider, delivery=delivery, daily_budget=1)

        first = coordinator.process(_anomaly(), now=NOW)
        second = coordinator.process(_anomaly(runtime_s=7_201), now=NOW)

        assert first.status == "sent"
        assert second.status == "budget_exhausted"
        assert len(provider.calls) == 1

    def test_unconfigured_sinks_fail_before_context_or_provider(self, tmp_path):
        provider = FakeProvider()
        provider.configured = False
        delivery = FakeDelivery()
        coordinator = _coordinator(tmp_path, provider=provider, delivery=delivery)

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "configuration_error"
        assert result.error_code == "provider_not_configured"
        assert provider.calls == []
        assert delivery.messages == []

    def test_corrupt_dedup_state_fails_closed(self, tmp_path):
        state_path = tmp_path / "insight-state.json"
        state_path.write_text("not-json")
        coordinator = _coordinator(tmp_path)

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "state_error"
        assert result.error_code == "state_unreadable"

    def test_invalid_configuration_is_auditable(self, tmp_path):
        config = _config(tmp_path, enabled=False, config_error="bad setting")
        coordinator = ProactiveInsightCoordinator(
            config,
            provider=FakeProvider(),
            delivery=FakeDelivery(),
            context_builder=lambda **_: CONTEXT,
        )

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "configuration_error"
        assert result.error_code == "invalid_configuration"


class TestConsumerIntegration:
    def test_consumer_appends_successful_insight_audit_event(self, tmp_path):
        coordinator = _coordinator(tmp_path)
        derived_log = tmp_path / "derived-events.jsonl"

        _emit_proactive_insight(_anomaly(), str(derived_log), coordinator)

        events = [json.loads(line) for line in derived_log.read_text().splitlines()]
        assert len(events) == 1
        assert events[0]["schema"] == INSIGHT_EVENT_SCHEMA
        assert events[0]["data"]["status"] == "sent"

    def test_consumer_does_not_append_replay_duplicate(self, tmp_path):
        coordinator = _coordinator(tmp_path)
        derived_log = tmp_path / "derived-events.jsonl"

        _emit_proactive_insight(_anomaly(), str(derived_log), coordinator)
        _emit_proactive_insight(
            _anomaly(ts="2026-08-26T17:01:00+00:00"),
            str(derived_log),
            coordinator,
        )

        assert len(derived_log.read_text().splitlines()) == 1


class TestEnvironmentIsolation:
    def test_enabled_factory_fails_closed_without_live_credentials(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOMEOPS_PROACTIVE_INSIGHT_ENABLED", "true")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        coordinator = build_proactive_insight_from_env(
            derived_log=str(tmp_path / "events.jsonl"),
            state_path=tmp_path / "state.json",
        )

        result = coordinator.process(_anomaly(), now=NOW)

        assert result.status == "configuration_error"
        assert result.error_code == "provider_not_configured"
