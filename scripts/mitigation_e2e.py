#!/usr/bin/env python3
"""Replay the staged Home Assistant mitigation flow without live writes.

The harness models only the Home Assistant state and action boundary, then
passes the resulting event-bus records through the real observer and consumer
implementations.  It is intentionally deterministic: no Home Assistant,
thermostat, or Telegram network connection is opened.

Usage::

    python3 scripts/mitigation_e2e.py

Revision history:
  2026-08-25  Add a deterministic HA-compatible replay that exercises three
              short-cycle attempts, observer/consumer persistence, rollback,
              Telegram alerting, and replay deduplication without live writes.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "services" / "consumer", ROOT / "services" / "observer"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
INSIGHTS = ROOT / "services" / "insights"
if str(INSIGHTS) not in sys.path:
    sys.path.insert(0, str(INSIGHTS))

from consumer import _process_mitigation_observer_event  # noqa: E402
from observer import _build_observer_record  # noqa: E402
from rules.config import load_rules_config  # noqa: E402
from validate_schema import validate_line  # noqa: E402

from processors import (  # noqa: E402
    MITIGATION_EVENT_TYPE,
    MITIGATION_ROLLBACK_EVENT_TYPE,
    MITIGATION_SHORT_CYCLE_EVENT_TYPE,
)

ZONE = "floor_2"
TARGET_CLIMATE = "climate.floor_2_thermostat"
PRIMARY_ZONE = "floor_1"


@dataclass
class ClimateState:
    """Small subset of a climate entity needed by the resume gate."""

    mode: str = "heat"
    setpoint_f: float = 68.0
    current_f: float = 65.0


@dataclass
class MitigationSimulator:
    """Deterministic model of the staged overlay's HA state/action boundary."""

    furnace_warmup_minutes: float
    stagger_minutes: float
    automation_enabled: bool = False
    rollback_automation_enabled: bool = False
    mitigation_enabled: bool = False
    furnace_on: bool = False
    furnace_started_at: datetime | None = None
    active_zones: set[str] = field(default_factory=lambda: {PRIMARY_ZONE})
    climates: dict[str, ClimateState] = field(
        default_factory=lambda: {TARGET_CLIMATE: ClimateState()}
    )
    attempt_count: int = 0
    storm_started_at: datetime | None = None
    incident_id: str = ""
    last_rollback_trigger_id: str = ""
    event_bus: list[dict[str, Any]] = field(default_factory=list)
    service_calls: list[dict[str, Any]] = field(default_factory=list)

    def enable_isolated_test(self) -> None:
        """Enable both YAML automations only inside this in-memory test."""
        self.automation_enabled = True
        self.rollback_automation_enabled = True
        self.mitigation_enabled = True

    def start_furnace(self, now: datetime) -> None:
        """Start a fresh simulated furnace session."""
        self.furnace_on = True
        self.furnace_started_at = now

    def stop_furnace(self) -> None:
        """End the simulated furnace session."""
        self.furnace_on = False

    def trigger_zone_stagger(
        self,
        now: datetime,
        *,
        trigger_event_id: str,
        resume_gate: bool = True,
    ) -> dict[str, Any] | None:
        """Replay one secondary-zone state trigger and return its HA event."""
        climate = self.climates[TARGET_CLIMATE]
        furnace_age_s = (
            (now - self.furnace_started_at).total_seconds()
            if self.furnace_started_at is not None
            else None
        )
        if not (
            self.automation_enabled
            and self.mitigation_enabled
            and self.furnace_on
            and furnace_age_s is not None
            and 0 <= furnace_age_s < self.furnace_warmup_minutes * 60
            and PRIMARY_ZONE in self.active_zones
            and ZONE not in {PRIMARY_ZONE}
            and climate.mode == "heat"
            and climate.setpoint_f >= 0
            and self.stagger_minutes > 0
            and (
                self.attempt_count < 3
                or self.storm_started_at is None
                or (now - self.storm_started_at).total_seconds() > 3600
            )
        ):
            return None

        if self.storm_started_at is None or (now - self.storm_started_at).total_seconds() > 3600:
            self.attempt_count = 1
            self.incident_id = trigger_event_id
            self.storm_started_at = now
            self.last_rollback_trigger_id = ""
        else:
            self.attempt_count = min(self.attempt_count + 1, 3)

        self.service_calls.append(
            {
                "service": "climate.set_hvac_mode",
                "entity_id": TARGET_CLIMATE,
                "hvac_mode": "off",
            }
        )
        climate.mode = "off"

        if resume_gate and self.mitigation_enabled and self.furnace_on:
            self.service_calls.append(
                {
                    "service": "climate.set_hvac_mode",
                    "entity_id": TARGET_CLIMATE,
                    "hvac_mode": "heat",
                }
            )
            climate.mode = "heat"
            outcome = "applied"
            reason = "secondary_zone_call_during_furnace_warmup"
        else:
            outcome = "skipped"
            reason = "resume_gate_failed"

        payload = {
            "event_type": MITIGATION_EVENT_TYPE,
            "zone": ZONE,
            "reason": reason,
            "delay_minutes": self.stagger_minutes,
            "trigger_event_id": trigger_event_id,
            "incident_id": self.incident_id,
            "attempt_number": self.attempt_count,
            "outcome": outcome,
        }
        self.event_bus.append(
            {
                "event_type": MITIGATION_EVENT_TYPE,
                "data": payload,
                "context": {"id": f"ha-context-{trigger_event_id}"},
                "fired_at": now,
            }
        )
        return payload

    def emit_short_cycle(
        self,
        now: datetime,
        *,
        trigger_event_id: str,
        duration_s: int = 90,
        threshold_s: int = 120,
    ) -> dict[str, Any] | None:
        """Inject a short-cycle event and run the rollback automation model."""
        payload = {
            "event_type": MITIGATION_SHORT_CYCLE_EVENT_TYPE,
            "incident_id": self.incident_id,
            "trigger_event_id": trigger_event_id,
            "reason": "short_cycle_after_mitigation_attempt",
            "duration_s": duration_s,
            "threshold_s": threshold_s,
        }

        started = self.storm_started_at
        age_s = (now - started).total_seconds() if started is not None else None
        qualifies = (
            self.rollback_automation_enabled
            and self.mitigation_enabled
            and self.attempt_count >= 3
            and age_s is not None
            and 0 <= age_s <= 3600
            and self.incident_id not in {"", "unknown", "unavailable"}
            and payload["incident_id"] == self.incident_id
            and trigger_event_id != self.last_rollback_trigger_id
            and bool(payload["reason"])
        )
        if not qualifies:
            return None

        self.mitigation_enabled = False
        rollback = {
            "event_type": MITIGATION_ROLLBACK_EVENT_TYPE,
            "incident_id": self.incident_id,
            "failed_attempts": self.attempt_count,
            "reason": "short_cycle_after_three_mitigation_attempts",
            "trigger_event_id": trigger_event_id,
            "storm_window_started_at": self.storm_started_at.isoformat(),
            "mitigation_enabled": False,
            "rollback_state": "rolled_back",
            "source_event_type": MITIGATION_SHORT_CYCLE_EVENT_TYPE,
            "short_cycle_duration_s": duration_s,
            "short_cycle_threshold_s": threshold_s,
        }
        self.last_rollback_trigger_id = trigger_event_id
        self.event_bus.append(
            {
                "event_type": MITIGATION_ROLLBACK_EVENT_TYPE,
                "data": rollback,
                "context": {"id": f"ha-context-{trigger_event_id}"},
                "fired_at": now,
            }
        )
        return rollback


def _load_overlay_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the staged YAML and assert the two automations remain opt-in."""
    path = ROOT / "homeassistant" / "automations.yaml"
    automations = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(automations, list):
        raise AssertionError("homeassistant/automations.yaml must be a list")
    by_id = {automation.get("id"): automation for automation in automations}
    expected = {"homeops_zone_call_stagger", "homeops_mitigation_automatic_rollback"}
    if set(by_id) != expected:
        raise AssertionError(f"unexpected staged automation IDs: {set(by_id)}")
    if any(automation.get("initial_state") is not False for automation in automations):
        raise AssertionError("the replay must not bypass disabled-by-default automation state")
    return by_id["homeops_zone_call_stagger"], by_id["homeops_mitigation_automatic_rollback"]


def _observer_record(ha_event: dict[str, Any]) -> dict[str, Any]:
    """Convert one simulated HA event through the real observer adapter."""
    fired_at = ha_event["fired_at"].isoformat()
    record = _build_observer_record(ha_event, set(), timestamp=fired_at)
    if record is None:
        raise AssertionError(f"observer dropped expected event: {ha_event['event_type']}")
    errors = validate_line(json.dumps(record))
    if errors:
        raise AssertionError(f"observer schema rejected {ha_event['event_type']}: {errors}")
    return record


def run_scenario() -> dict[str, Any]:
    """Run the three-cycle replay through HA model → observer → consumer."""
    zone_automation, rollback_automation = _load_overlay_contract()
    rules = load_rules_config().rule("mitigation")
    if rules["enabled"] is not False:
        raise AssertionError("production mitigation configuration must remain disabled")
    if (
        zone_automation["initial_state"] is not False
        or rollback_automation["initial_state"] is not False
    ):
        raise AssertionError("staged automations must remain disabled in the checked-in overlay")

    start = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)
    simulator = MitigationSimulator(
        furnace_warmup_minutes=rules["furnace_warmup_minutes"],
        stagger_minutes=rules["zone_stagger_minutes"],
    )
    simulator.enable_isolated_test()
    cycles: list[dict[str, Any]] = []

    for index in range(1, 4):
        cycle_start = start + timedelta(minutes=(index - 1) * 10)
        simulator.start_furnace(cycle_start)
        zone_payload = simulator.trigger_zone_stagger(
            cycle_start + timedelta(seconds=30),
            trigger_event_id=f"stagger-{index}",
        )
        if zone_payload is None:
            raise AssertionError(f"cycle {index} did not trigger the zone-stagger automation")
        short_cycle_time = cycle_start + timedelta(minutes=6)
        rollback_payload = simulator.emit_short_cycle(
            short_cycle_time,
            trigger_event_id=f"short-cycle-{index}",
        )
        cycles.append(
            {
                "cycle": index,
                "attempt": simulator.attempt_count,
                "zone_outcome": zone_payload["outcome"],
                "rollback_triggered": rollback_payload is not None,
            }
        )
        simulator.stop_furnace()

    if [cycle["attempt"] for cycle in cycles] != [1, 2, 3]:
        raise AssertionError(f"unexpected attempt sequence: {cycles}")
    if [cycle["rollback_triggered"] for cycle in cycles] != [False, False, True]:
        raise AssertionError(f"unexpected rollback sequence: {cycles}")
    rollback_events = [
        event
        for event in simulator.event_bus
        if event["event_type"] == MITIGATION_ROLLBACK_EVENT_TYPE
    ]
    if len(rollback_events) != 1:
        raise AssertionError(f"expected exactly one HA rollback event, got {len(rollback_events)}")
    if (
        _build_observer_record(
            {
                "event_type": MITIGATION_SHORT_CYCLE_EVENT_TYPE,
                "data": {"event_type": MITIGATION_SHORT_CYCLE_EVENT_TYPE},
            },
            set(),
        )
        is not None
    ):
        raise AssertionError("short-cycle input must not be treated as an observed output event")

    derived_log = Path(tempfile.mkdtemp(prefix="homeops-mitigation-e2e-")) / "derived.jsonl"
    observer_records = [_observer_record(event) for event in simulator.event_bus]
    emitted_count = 0
    consumer_logger = logging.getLogger("consumer")
    previous_log_level = consumer_logger.level
    consumer_logger.setLevel(logging.CRITICAL)
    with patch("consumer._send_telegram") as send_telegram:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                for record in observer_records:
                    _, emitted = _process_mitigation_observer_event(
                        record,
                        str(derived_log),
                        False,
                        telegram_bot_token="test-bot-token",
                        telegram_chat_id="test-chat-id",
                    )
                    emitted_count += int(emitted)

                rollback_record = observer_records[-1]
                _, replay_emitted = _process_mitigation_observer_event(
                    rollback_record,
                    str(derived_log),
                    False,
                    telegram_bot_token="test-bot-token",
                    telegram_chat_id="test-chat-id",
                )
        finally:
            consumer_logger.setLevel(previous_log_level)

    derived_events = [json.loads(line) for line in derived_log.read_text().splitlines()]
    derived_schemas = [event["schema"] for event in derived_events]
    expected_schemas = [MITIGATION_EVENT_TYPE] * 3 + [MITIGATION_ROLLBACK_EVENT_TYPE]
    if derived_schemas != expected_schemas:
        raise AssertionError(f"unexpected derived schemas: {derived_schemas}")
    if emitted_count != 4 or replay_emitted or len(send_telegram.call_args_list) != 1:
        raise AssertionError(
            "consumer persistence/replay contract failed: "
            f"emitted={emitted_count}, replay={replay_emitted}, "
            f"alerts={len(send_telegram.call_args_list)}"
        )
    alert_text = send_telegram.call_args.args[2]
    if "URGENT" not in alert_text or "Mitigation guard: OFF" not in alert_text:
        raise AssertionError("rollback alert did not contain urgent operator diagnostics")

    return {
        "incident_id": simulator.incident_id,
        "cycles": cycles,
        "ha_event_count": len(simulator.event_bus),
        "observer_event_count": len(observer_records),
        "derived_event_count": len(derived_events),
        "mitigation_enabled_after_rollback": simulator.mitigation_enabled,
        "telegram_alert_count": len(send_telegram.call_args_list),
        "replay_emitted": replay_emitted,
        "service_call_count": len(simulator.service_calls),
    }


def main() -> int:
    """Run the replay and print a concise operator-facing result."""
    report = run_scenario()
    print("PASS: staged mitigation end-to-end replay")
    for cycle in report["cycles"]:
        print(
            f"  cycle {cycle['cycle']}: attempt {cycle['attempt']}, "
            f"stagger {cycle['zone_outcome']}, "
            f"rollback={'yes' if cycle['rollback_triggered'] else 'no'}"
        )
    print(
        "  final: mitigation_enabled="
        f"{report['mitigation_enabled_after_rollback']}, "
        f"derived_events={report['derived_event_count']}, "
        f"telegram_alerts={report['telegram_alert_count']}, "
        f"replay_emitted={report['replay_emitted']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
