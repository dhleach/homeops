"""Validate the shared DeploymentSpec and profile artifact for Ansible.

Ansible owns inventory resolution and HTTP orchestration. This small
controller-side bridge deliberately reuses the same manifest parser and
immutable artifact checks as the Python deployer so the two implementations
cannot silently drift at their input boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .deployer import DeploymentArtifact
from .fleet_state import FleetProfile
from .trusted_artifact import TrustedArtifactError, parse_manifest_bytes


class AnsibleValidationError(ValueError):
    """Raised when Ansible inputs do not satisfy the shared contract."""


def _read(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AnsibleValidationError(f"unable to read {label}: {type(exc).__name__}") from exc


def validate_inputs(spec_path: str | Path, artifact_path: str | Path) -> dict[str, Any]:
    """Validate exact Ansible inputs and return the bounded playbook context."""
    raw_spec = _read(Path(spec_path), "DeploymentSpec")
    try:
        spec = parse_manifest_bytes(raw_spec)
    except TrustedArtifactError as exc:
        raise AnsibleValidationError(str(exc)) from exc

    if spec.implementation != "ansible":
        raise AnsibleValidationError("implementation must be 'ansible' for the Ansible playbook")

    try:
        artifact = DeploymentArtifact.from_path(artifact_path)
    except (OSError, TypeError, ValueError) as exc:
        raise AnsibleValidationError(f"invalid profile artifact: {exc}") from exc

    expected_profile = FleetProfile(spec.profile_color, spec.profile_shape)
    if artifact.profile != expected_profile:
        raise AnsibleValidationError(
            "profile artifact does not match the validated DeploymentSpec profile"
        )

    return {
        "artifact_sha256": artifact.sha256,
        "deployment_id": spec.deployment_id,
        "profile": artifact.profile.to_dict(),
        "spec": spec.to_dict(),
        "target_ids": list(spec.expanded_target_ids),
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate inputs and emit the JSON context consumed by Ansible."""
    try:
        args = _parse_args(argv)
        print(json.dumps(validate_inputs(args.spec, args.artifact), sort_keys=True))
    except (AnsibleValidationError, OSError, TypeError, ValueError) as exc:
        print(f"invalid Ansible Fleet Deploy input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["AnsibleValidationError", "main", "validate_inputs"]
