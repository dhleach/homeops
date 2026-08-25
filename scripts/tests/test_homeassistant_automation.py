"""Contract tests for the staged Home Assistant mitigation overlay.

Revision history:
  2026-08-25  Cover durable attempt bookkeeping and automatic rollback after
              a continued short-cycle event within the incident window.
  2026-08-25  Require applied and skipped mitigation events to carry the zone,
              reason, delay, trigger reference, and outcome fields.
  2026-08-25  Added YAML and configuration-projection checks so the opt-in HA
              automation cannot lose its guard, timing source, or fail-closed
              resume conditions while the later mitigation slices are built.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from rules.config import load_rules_config

ROOT = Path(__file__).parents[2]
AUTOMATION_FILE = ROOT / "homeassistant" / "automations.yaml"
HELPERS_FILE = ROOT / "homeassistant" / "helpers.yaml"


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _template_conditions(conditions):
    return [item["value_template"] for item in conditions if item.get("condition") == "template"]


def test_automations_are_a_valid_staged_list():
    automations = _load_yaml(AUTOMATION_FILE)

    assert isinstance(automations, list)
    assert len(automations) == 2
    assert {automation["id"] for automation in automations} == {
        "homeops_zone_call_stagger",
        "homeops_mitigation_automatic_rollback",
    }
    assert all(automation["initial_state"] is False for automation in automations)
    assert all(automation["mode"] == "single" for automation in automations)
    assert all(automation["max_exceeded"] == "silent" for automation in automations)


def test_triggers_cover_each_zone_call_transition():
    automation = _load_yaml(AUTOMATION_FILE)[0]
    triggers = automation["triggers"]

    assert len(triggers) == 3
    assert {trigger["id"] for trigger in triggers} == {"floor_1", "floor_2", "floor_3"}
    assert {trigger["entity_id"] for trigger in triggers} == {
        "binary_sensor.floor_1_heating_call",
        "binary_sensor.floor_2_heating_call",
        "binary_sensor.floor_3_heating_call",
    }
    assert all(trigger["trigger"] == "state" for trigger in triggers)
    assert all(trigger["from"] == "off" and trigger["to"] == "on" for trigger in triggers)


def test_conditions_are_guarded_and_require_a_secondary_call_and_recent_furnace_start():
    automation = _load_yaml(AUTOMATION_FILE)[0]
    conditions = automation["conditions"]
    templates = _template_conditions(conditions)
    state_conditions = {
        (condition["entity_id"], condition["state"])
        for condition in conditions
        if condition["condition"] == "state"
    }

    assert state_conditions == {
        ("input_boolean.mitigation_enabled", "on"),
        ("binary_sensor.furnace_heating", "on"),
    }
    assert any(
        "active_entities" in template and "trigger_entity" in template for template in templates
    )
    assert any(
        "input_number.homeops_mitigation_furnace_warmup_minutes" in template
        and "last_changed" in template
        for template in templates
    )


def test_action_turns_off_waits_for_configured_delay_and_resumes_climate_only():
    automation = _load_yaml(AUTOMATION_FILE)[0]
    actions = automation["actions"]
    off_action = actions[6]
    delay_action = actions[7]
    choice = actions[8]["choose"][0]
    resume_action = choice["sequence"][0]

    assert off_action["action"] == "climate.set_hvac_mode"
    assert off_action["data"] == {"hvac_mode": "off"}
    assert off_action["target"] == {"entity_id": "{{ target_climate }}"}
    assert (
        "input_number.homeops_mitigation_zone_stagger_minutes"
        in actions[0]["variables"]["stagger_minutes"]
    )
    assert delay_action["delay"]["minutes"] == "{{ stagger_minutes }}"
    assert resume_action["action"] == "climate.set_hvac_mode"
    assert resume_action["data"] == {"hvac_mode": "heat"}
    assert resume_action["target"] == {"entity_id": "{{ target_climate }}"}
    applied_event = next(
        action
        for action in choice["sequence"]
        if action.get("event") == "homeops.mitigation.zone_stagger_applied.v1"
    )
    assert applied_event["event_data"] == {
        "event_type": "homeops.mitigation.zone_stagger_applied.v1",
        "zone": "{{ trigger.id }}",
        "reason": "{{ mitigation_reason }}",
        "delay_minutes": "{{ stagger_minutes }}",
        "trigger_event_id": "{{ trigger_event_id }}",
        "incident_id": "{{ incident_id }}",
        "attempt_number": "{{ attempt_number }}",
        "outcome": "applied",
    }
    assert actions[8]["default"][0]["event"] == "homeops.mitigation.zone_stagger_applied.v1"
    assert actions[8]["default"][0]["event_data"] == {
        "event_type": "homeops.mitigation.zone_stagger_applied.v1",
        "zone": "{{ trigger.id }}",
        "reason": "resume_gate_failed",
        "delay_minutes": "{{ stagger_minutes }}",
        "trigger_event_id": "{{ trigger_event_id }}",
        "incident_id": "{{ incident_id }}",
        "attempt_number": "{{ attempt_number }}",
        "outcome": "skipped",
    }
    assert {
        (condition["entity_id"], condition["state"])
        for condition in choice["conditions"]
        if condition["condition"] == "state"
    } == {
        ("input_boolean.mitigation_enabled", "on"),
        ("binary_sensor.furnace_heating", "on"),
    }
    resume_template = _template_conditions(choice["conditions"])[0]
    assert "original_setpoint" in resume_template
    assert "current_temperature" in resume_template
    assert "is_state(target_climate, 'off')" in resume_template


def test_stagger_tracks_attempt_number_and_starts_a_new_incident_after_expiry():
    automation = _load_yaml(AUTOMATION_FILE)[0]
    actions = automation["actions"]

    attempt_condition = actions[3]["value_template"]
    assert "homeops_mitigation_attempt_count" in attempt_condition
    assert "homeops_mitigation_storm_started_at" in attempt_condition
    assert "3600" in attempt_condition

    tracking = actions[4]["variables"]
    assert "active_incident" in tracking
    assert "incident_id" in tracking
    assert "attempt_number" in tracking
    assert "homeops_mitigation_incident_id" in tracking["active_incident"]

    bookkeeping = actions[5]["choose"][0]["sequence"]
    assert bookkeeping[0]["action"] == "input_number.set_value"
    assert bookkeeping[0]["target"]["entity_id"] == "input_number.homeops_mitigation_attempt_count"
    assert bookkeeping[1]["action"] == "input_text.set_value"
    assert bookkeeping[2]["action"] == "input_datetime.set_datetime"
    assert bookkeeping[2]["data"] == {"timestamp": "{{ now().timestamp() }}"}
    assert bookkeeping[3]["action"] == "input_text.set_value"
    assert actions[5]["default"][0]["action"] == "input_number.set_value"


def test_automatic_rollback_disables_guard_and_emits_auditable_event():
    rollback = _load_yaml(AUTOMATION_FILE)[1]

    assert rollback["id"] == "homeops_mitigation_automatic_rollback"
    assert rollback["triggers"] == [
        {
            "trigger": "event",
            "event_type": "homeops.mitigation.short_cycle_detected.v1",
        }
    ]
    assert rollback["conditions"][0] == {
        "condition": "state",
        "entity_id": "input_boolean.mitigation_enabled",
        "state": "on",
    }
    rollback_condition = rollback["conditions"][1]["value_template"]
    assert "attempts >= 3" in rollback_condition
    assert "age <= 3600" in rollback_condition
    assert "incident_id" in rollback_condition
    assert "last_rollback_trigger_id" in rollback_condition

    actions = rollback["actions"]
    assert actions[1] == {
        "action": "input_boolean.turn_off",
        "target": {"entity_id": "input_boolean.mitigation_enabled"},
    }
    assert actions[2] == {
        "event": "homeops.mitigation.rollback.v1",
        "event_data": {
            "event_type": "homeops.mitigation.rollback.v1",
            "incident_id": "{{ rollback_incident_id }}",
            "failed_attempts": "{{ rollback_failed_attempts }}",
            "reason": "{{ rollback_reason }}",
            "trigger_event_id": "{{ rollback_trigger_event_id }}",
            "storm_window_started_at": "{{ rollback_storm_window_started_at }}",
            "mitigation_enabled": False,
            "rollback_state": "rolled_back",
            "source_event_type": "homeops.mitigation.short_cycle_detected.v1",
            "short_cycle_duration_s": "{{ rollback_duration_s }}",
            "short_cycle_threshold_s": "{{ rollback_threshold_s }}",
        },
    }
    assert actions[3]["action"] == "input_text.set_value"
    assert (
        actions[3]["target"]["entity_id"]
        == "input_text.homeops_mitigation_last_rollback_trigger_id"
    )


def test_helper_projection_matches_validated_mitigation_settings_and_starts_safe():
    config = load_rules_config().rule("mitigation")
    helpers = _load_yaml(HELPERS_FILE)

    assert config["enabled"] is False
    assert helpers["input_boolean"]["mitigation_enabled"]["initial"] is False
    furnace_helper = helpers["input_number"]["homeops_mitigation_furnace_warmup_minutes"]
    stagger_helper = helpers["input_number"]["homeops_mitigation_zone_stagger_minutes"]
    assert furnace_helper["initial"] == config["furnace_warmup_minutes"]
    assert stagger_helper["initial"] == config["zone_stagger_minutes"]
    assert furnace_helper["max"] == 60
    assert stagger_helper["max"] == 15

    attempt_helper = helpers["input_number"]["homeops_mitigation_attempt_count"]
    assert attempt_helper["min"] == 0
    assert attempt_helper["max"] == 3
    assert attempt_helper["step"] == 1
    assert "initial" not in attempt_helper

    storm_helper = helpers["input_datetime"]["homeops_mitigation_storm_started_at"]
    assert storm_helper["has_date"] is True
    assert storm_helper["has_time"] is True
    assert "initial" not in storm_helper

    incident_helper = helpers["input_text"]["homeops_mitigation_incident_id"]
    rollback_helper = helpers["input_text"]["homeops_mitigation_last_rollback_trigger_id"]
    assert incident_helper["max"] == 64
    assert rollback_helper["max"] == 64
    assert "initial" not in incident_helper
    assert "initial" not in rollback_helper
