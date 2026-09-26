"""Tests for the shared input boundary used by the Ansible deployer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deploy_demo.ansible_validation import AnsibleValidationError, validate_inputs
from deploy_demo.fleet_state import FleetProfile


def valid_spec(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": "ansible-validation-001",
        "environment": "test",
        "profile": {"color": "purple", "shape": "hexagon"},
        "implementation": "ansible",
        "strategy": "rolling",
        "failure_mode": "rollback",
    }
    payload.update(overrides)
    return payload


def write_inputs(tmp_path: Path, spec: dict[str, object] | None = None) -> tuple[Path, Path]:
    spec_value = spec or valid_spec()
    spec_path = tmp_path / "deployment-spec.json"
    artifact_path = tmp_path / "profile.json"
    spec_path.write_text(
        json.dumps(spec_value, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    profile = spec_value["profile"]
    assert isinstance(profile, dict)
    artifact_path.write_text(
        json.dumps(profile, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return spec_path, artifact_path


def test_validate_inputs_reuses_canonical_contract_and_artifact_identity(tmp_path: Path) -> None:
    spec_path, artifact_path = write_inputs(tmp_path)

    result = validate_inputs(spec_path, artifact_path)

    profile = FleetProfile(color="purple", shape="hexagon")
    assert result["deployment_id"] == "ansible-validation-001"
    assert result["target_ids"] == [
        "test-vehicle-01",
        "test-vehicle-02",
        "test-vehicle-03",
        "test-vehicle-04",
    ]
    assert result["profile"] == profile.to_dict()
    assert (
        result["artifact_sha256"]
        == hashlib.sha256(profile.canonical_json().encode("utf-8")).hexdigest()
    )


def test_validate_inputs_accepts_one_manifest_trailing_newline(tmp_path: Path) -> None:
    spec_path, artifact_path = write_inputs(tmp_path)
    spec_path.write_bytes(spec_path.read_bytes() + b"\n")

    result = validate_inputs(spec_path, artifact_path)

    assert result["spec"]["implementation"] == "ansible"


def test_validate_inputs_rejects_noncanonical_manifest(tmp_path: Path) -> None:
    spec_path, artifact_path = write_inputs(tmp_path)
    spec_path.write_text(json.dumps(valid_spec(), indent=2), encoding="utf-8")

    with pytest.raises(AnsibleValidationError, match="canonical"):
        validate_inputs(spec_path, artifact_path)


def test_validate_inputs_rejects_python_implementation(tmp_path: Path) -> None:
    spec_path, artifact_path = write_inputs(tmp_path, valid_spec(implementation="python"))

    with pytest.raises(AnsibleValidationError, match="implementation"):
        validate_inputs(spec_path, artifact_path)


def test_validate_inputs_rejects_conflicting_profile_artifact(tmp_path: Path) -> None:
    spec_path, artifact_path = write_inputs(tmp_path)
    artifact_path.write_text(
        json.dumps({"color": "green", "shape": "circle"}, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(AnsibleValidationError, match="does not match"):
        validate_inputs(spec_path, artifact_path)
