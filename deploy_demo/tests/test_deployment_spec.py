"""Regression tests for the strict Fleet Deploy Lab DeploymentSpec contract."""

from __future__ import annotations

import io
import json
import sys
from copy import deepcopy

import pytest

from deploy_demo.deployment_spec import (
    TARGETS_BY_ENVIRONMENT,
    DeploymentSpecError,
    canonicalize_deployment_spec,
    expand_targets,
    main,
    validate_deployment_spec,
)


def valid_spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": "demo-20260925-001",
        "environment": "test",
        "profile": {"color": "blue", "shape": "circle"},
        "implementation": "python",
        "strategy": "rolling",
        "failure_mode": "rollback",
    }
    spec.update(overrides)
    if overrides.get("environment", "__missing__") is None:
        spec.pop("environment", None)
    if overrides.get("target_ids", "__missing__") is None:
        spec.pop("target_ids", None)
    if "target_ids" in overrides and overrides["target_ids"] is not None:
        spec.pop("environment", None)
    return spec


def test_valid_environment_spec_expands_to_four_known_targets() -> None:
    spec = validate_deployment_spec(valid_spec())

    assert spec.expanded_target_ids == TARGETS_BY_ENVIRONMENT["test"]
    assert len(spec.expanded_target_ids) == 4


def test_explicit_target_selection_is_normalized_to_stable_order() -> None:
    target_ids = ["prod-vehicle-02", "test-vehicle-01", "stage-vehicle-04"]

    assert expand_targets(valid_spec(environment=None, target_ids=target_ids)) == (
        "test-vehicle-01",
        "stage-vehicle-04",
        "prod-vehicle-02",
    )


def test_canonical_json_is_stable_for_equivalent_input_orderings() -> None:
    first = valid_spec(environment=None, target_ids=["prod-vehicle-01", "test-vehicle-02"])
    second = {
        "failure_mode": "rollback",
        "target_ids": ["test-vehicle-02", "prod-vehicle-01"],
        "strategy": "rolling",
        "profile": {"shape": "circle", "color": "blue"},
        "implementation": "python",
        "deployment_id": "demo-20260925-001",
        "schema_version": 1,
    }

    assert canonicalize_deployment_spec(first) == canonicalize_deployment_spec(second)


def test_canonical_json_is_one_line_sorted_json() -> None:
    canonical = canonicalize_deployment_spec(valid_spec())

    assert "\n" not in canonical
    assert canonical == (
        '{"deployment_id":"demo-20260925-001","environment":"test",'
        '"failure_mode":"rollback","implementation":"python",'
        '"profile":{"color":"blue","shape":"circle"},"schema_version":1,'
        '"strategy":"rolling"}'
    )
    assert json.loads(canonical) == validate_deployment_spec(valid_spec()).to_dict()


def test_round_trip_from_normalized_dict() -> None:
    spec = validate_deployment_spec(valid_spec(environment=None, target_ids=["test-vehicle-03"]))

    assert validate_deployment_spec(spec.to_dict()) == spec


@pytest.mark.parametrize(
    "extra_key",
    ["command", "code", "path", "url"],
)
def test_executable_or_routing_fields_are_rejected(extra_key: str) -> None:
    payload = valid_spec()
    payload[extra_key] = "untrusted-value"

    with pytest.raises(DeploymentSpecError, match="unsupported key"):
        validate_deployment_spec(payload)


def test_profile_cannot_carry_a_url_or_other_unbounded_data() -> None:
    payload = valid_spec()
    profile = deepcopy(payload["profile"])
    assert isinstance(profile, dict)
    profile["url"] = "https://example.invalid"
    payload["profile"] = profile

    with pytest.raises(DeploymentSpecError, match="profile.*unsupported key"):
        validate_deployment_spec(payload)


@pytest.mark.parametrize(
    "missing_key",
    ["schema_version", "deployment_id", "profile", "implementation", "strategy", "failure_mode"],
)
def test_required_fields_are_enforced(missing_key: str) -> None:
    payload = valid_spec()
    del payload[missing_key]

    with pytest.raises(DeploymentSpecError, match="missing required"):
        validate_deployment_spec(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {**valid_spec(), "environment": None, "target_ids": ["test-vehicle-01"]},
        {**valid_spec(), "target_ids": ["test-vehicle-01"]},
    ],
)
def test_exactly_one_target_selector_is_required(payload: dict[str, object]) -> None:
    with pytest.raises(DeploymentSpecError, match="exactly one"):
        validate_deployment_spec(payload)


def test_empty_target_selection_is_rejected() -> None:
    with pytest.raises(DeploymentSpecError, match="at least one"):
        validate_deployment_spec(valid_spec(environment=None, target_ids=[]))


def test_more_than_twelve_targets_is_rejected_before_duplicate_check() -> None:
    all_targets = [target for targets in TARGETS_BY_ENVIRONMENT.values() for target in targets]
    with pytest.raises(DeploymentSpecError, match="more than 12"):
        validate_deployment_spec(
            valid_spec(environment=None, target_ids=all_targets + [all_targets[0]])
        )


def test_duplicate_target_ids_are_rejected() -> None:
    with pytest.raises(DeploymentSpecError, match="duplicate"):
        validate_deployment_spec(
            valid_spec(environment=None, target_ids=["test-vehicle-01", "test-vehicle-01"])
        )


def test_unknown_target_id_is_rejected() -> None:
    with pytest.raises(DeploymentSpecError, match="unknown target"):
        validate_deployment_spec(valid_spec(environment=None, target_ids=["prod-vehicle-99"]))


@pytest.mark.parametrize("environment", ["dev", "production"])
def test_unknown_environment_is_rejected(environment: str) -> None:
    with pytest.raises(DeploymentSpecError, match="environment.*one of"):
        validate_deployment_spec(valid_spec(environment=environment))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile", {"color": "red", "shape": "circle"}),
        ("profile", {"color": "blue", "shape": "star"}),
        ("implementation", "shell"),
        ("strategy", "blue_green"),
        ("failure_mode", "continue"),
    ],
)
def test_finite_enum_values_are_enforced(field: str, value: object) -> None:
    payload = valid_spec()
    if field == "profile":
        payload[field] = value
    else:
        payload[field] = value

    with pytest.raises(DeploymentSpecError, match="one of"):
        validate_deployment_spec(payload)


def test_invalid_strategy_failure_mode_combination_is_rejected() -> None:
    with pytest.raises(DeploymentSpecError, match="all_at_once.*supports only: abort"):
        validate_deployment_spec(valid_spec(strategy="all_at_once", failure_mode="rollback"))


@pytest.mark.parametrize("deployment_id", ["", "Demo-1", "has spaces", "ends-", "-starts"])
def test_deployment_id_is_a_bounded_lowercase_identifier(deployment_id: str) -> None:
    with pytest.raises(DeploymentSpecError, match="deployment_id"):
        validate_deployment_spec(valid_spec(deployment_id=deployment_id))


def test_schema_version_rejects_boolean_and_unknown_versions() -> None:
    for version in (True, 2):
        with pytest.raises(DeploymentSpecError, match="schema_version"):
            validate_deployment_spec(valid_spec(schema_version=version))


def test_cli_validates_stdin_and_emits_canonical_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(valid_spec())))

    assert main([]) == 0
    assert capsys.readouterr().out.strip() == canonicalize_deployment_spec(valid_spec())


def test_cli_fails_closed_for_invalid_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(valid_spec(command="rm -rf /"))))

    assert main([]) == 2
    assert "invalid DeploymentSpec" in capsys.readouterr().err
