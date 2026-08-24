"""Tests for the validated shared insight-rule configuration loader."""

from __future__ import annotations

import json

import pytest
from rules.config import (
    DEFAULT_RULES_CONFIG_PATH,
    RulesConfigError,
    _parse_minimal_yaml,
    load_rules_config,
)


def _write_config(tmp_path, **changes):
    """Write a valid JSON-compatible YAML config with selected nested changes."""
    config = load_rules_config(DEFAULT_RULES_CONFIG_PATH).rules
    for path, value in changes.items():
        section, field = path.split(".", 1)
        config[section][field] = value
    payload = {"version": 1, "rules": config}
    path = tmp_path / "rules.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoadRulesConfig:
    def test_loads_checked_in_config(self):
        config = load_rules_config()

        assert config.path == DEFAULT_RULES_CONFIG_PATH
        assert config.is_enabled("floor_runtime_anomaly") is True
        assert config.rule("floor_runtime_anomaly")["overrun_ratio"] == 1.5
        assert config.rule("floor_no_response")["no_response_minutes"]["floor_2"] == 15.0
        assert config.rule("storm")["storm_count"] == 3

    def test_explicit_path_overrides_environment(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path, **{"storm.enabled": False})
        monkeypatch.setenv("HOMEOPS_RULES_CONFIG", str(tmp_path / "does-not-exist.yaml"))

        config = load_rules_config(path)

        assert config.path == path
        assert config.is_enabled("storm") is False

    def test_rule_settings_are_defensive_copies(self):
        config = load_rules_config()
        settings = config.rule("storm")
        settings["storm_count"] = 99

        assert config.rule("storm")["storm_count"] == 3

    def test_missing_file_is_actionable(self, tmp_path):
        with pytest.raises(RulesConfigError, match="Could not read rules config"):
            load_rules_config(tmp_path / "missing.yaml")

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"storm.storm_count": 1}, "storm_count must be >= 2"),
            ({"floor_runtime_anomaly.overrun_ratio": 0.5}, "overrun_ratio must be >= 1.0"),
            ({"floor_no_response.no_response_minutes": {"floor_1": 5}}, "invalid floors"),
        ],
    )
    def test_invalid_values_fail_validation(self, tmp_path, change, message):
        path = _write_config(tmp_path, **change)

        with pytest.raises(RulesConfigError, match=message):
            load_rules_config(path)

    def test_unknown_rule_fails_validation(self, tmp_path):
        config = load_rules_config().rules
        config["not_a_rule"] = {"enabled": True}
        path = tmp_path / "rules.yaml"
        path.write_text(json.dumps({"version": 1, "rules": config}), encoding="utf-8")

        with pytest.raises(RulesConfigError, match="unknown rules"):
            load_rules_config(path)


def test_minimal_yaml_fallback_parses_nested_scalars():
    parsed = _parse_minimal_yaml(
        """
        # comments and blank lines are allowed
        version: 1
        rules:
          storm:
            enabled: false
            storm_count: 3
            storm_window_hours: 1.5
        """
    )

    assert parsed == {
        "version": 1,
        "rules": {
            "storm": {
                "enabled": False,
                "storm_count": 3,
                "storm_window_hours": 1.5,
            }
        },
    }
