"""Contract tests for the staged Home Assistant mitigation overlay.

Revision history:
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


def test_automation_is_a_valid_single_item_list():
    automations = _load_yaml(AUTOMATION_FILE)

    assert isinstance(automations, list)
    assert len(automations) == 1
    assert automations[0]["id"] == "homeops_zone_call_stagger"
    assert automations[0]["initial_state"] is False
    assert automations[0]["mode"] == "single"
    assert automations[0]["max_exceeded"] == "silent"


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
    off_action = actions[3]
    delay_action = actions[4]
    choice = actions[5]["choose"][0]
    resume_action = choice["sequence"][0]

    assert off_action["action"] == "climate.set_hvac_mode"
    assert off_action["data"] == {"hvac_mode": "off"}
    assert off_action["target"] == {"entity_id": "{{ target_climate }}"}
    assert (
        "input_number.homeops_mitigation_zone_stagger_minutes" in delay_action["delay"]["minutes"]
    )
    assert resume_action["action"] == "climate.set_hvac_mode"
    assert resume_action["data"] == {"hvac_mode": "heat"}
    assert resume_action["target"] == {"entity_id": "{{ target_climate }}"}
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
