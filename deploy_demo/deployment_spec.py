"""The canonical JSON contract for a Fleet Deploy Lab request.

The module intentionally uses only the Python standard library.  The same
validator can therefore be imported by the FastAPI boundary and executed by a
trusted CI workflow without a second schema implementation or a dependency
that only exists in one runtime.

The public input is deliberately small and closed.  It describes a simulated
deployment profile; it never carries a command, path, URL, executable code, or
other instruction for a browser or deployer to interpret.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
MAX_TARGETS = 12

ENVIRONMENTS = ("test", "stage", "prod")
COLORS = ("blue", "green", "orange", "purple")
SHAPES = ("circle", "hexagon", "square", "triangle")
IMPLEMENTATIONS = ("python", "ansible")
STRATEGIES = ("all_at_once", "rolling", "canary")
FAILURE_MODES = ("abort", "rollback")

TARGETS_BY_ENVIRONMENT: dict[str, tuple[str, ...]] = {
    "test": tuple(f"test-vehicle-{index:02d}" for index in range(1, 5)),
    "stage": tuple(f"stage-vehicle-{index:02d}" for index in range(1, 5)),
    "prod": tuple(f"prod-vehicle-{index:02d}" for index in range(1, 5)),
}
KNOWN_TARGET_IDS = frozenset(
    target_id for targets in TARGETS_BY_ENVIRONMENT.values() for target_id in targets
)
TARGET_ORDER = tuple(
    target_id for environment in ENVIRONMENTS for target_id in TARGETS_BY_ENVIRONMENT[environment]
)
TARGET_ORDER_INDEX = {target_id: index for index, target_id in enumerate(TARGET_ORDER)}

_TOP_LEVEL_KEYS = frozenset(
    {
        "deployment_id",
        "environment",
        "failure_mode",
        "implementation",
        "profile",
        "schema_version",
        "strategy",
        "target_ids",
    }
)
_PROFILE_KEYS = frozenset({"color", "shape"})
_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_FAILURE_MODES_BY_STRATEGY = {
    "all_at_once": frozenset({"abort"}),
    "rolling": frozenset({"abort", "rollback"}),
    "canary": frozenset({"abort", "rollback"}),
}


class DeploymentSpecError(ValueError):
    """Raised when untrusted input is not a valid DeploymentSpec."""


def _error(path: str, message: str) -> DeploymentSpecError:
    return DeploymentSpecError(f"{path}: {message}")


def _require_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(path, "must be a non-empty string")
    return value


def _require_enum(value: object, path: str, allowed: Sequence[str]) -> str:
    value = _require_string(value, path)
    if value not in allowed:
        choices = ", ".join(allowed)
        raise _error(path, f"must be one of: {choices}")
    return value


def _check_exact_keys(value: Mapping[str, object], allowed: frozenset[str], path: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise _error(path, f"contains unsupported key(s): {', '.join(unexpected)}")


def _sorted_target_ids(target_ids: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(target_ids, key=TARGET_ORDER_INDEX.__getitem__))


def _validate_target_ids(value: object, path: str = "target_ids") -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error(path, "must be a JSON array")
    if not value:
        raise _error(path, "must contain at least one target")
    if len(value) > MAX_TARGETS:
        raise _error(path, f"cannot contain more than {MAX_TARGETS} targets")

    target_ids: list[str] = []
    for index, target_id in enumerate(value):
        target_id = _require_string(target_id, f"{path}[{index}]")
        if target_id not in KNOWN_TARGET_IDS:
            raise _error(f"{path}[{index}]", f"unknown target ID: {target_id}")
        target_ids.append(target_id)

    if len(set(target_ids)) != len(target_ids):
        raise _error(path, "must not contain duplicate target IDs")
    return _sorted_target_ids(target_ids)


def _validate_profile(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise _error("profile", "must be a JSON object")
    _check_exact_keys(value, _PROFILE_KEYS, "profile")
    missing = sorted(_PROFILE_KEYS - set(value))
    if missing:
        raise _error("profile", f"missing required key(s): {', '.join(missing)}")
    color = _require_enum(value["color"], "profile.color", COLORS)
    shape = _require_enum(value["shape"], "profile.shape", SHAPES)
    return color, shape


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    """A validated, immutable deployment request.

    Exactly one of ``environment`` and ``target_ids`` is populated.  The
    latter is normalized to the repository's stable target order so equivalent
    target sets have identical canonical JSON.
    """

    deployment_id: str
    profile_color: str
    profile_shape: str
    implementation: str
    strategy: str
    failure_mode: str
    schema_version: int = SCHEMA_VERSION
    environment: str | None = None
    target_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise _error("schema_version", f"must equal {SCHEMA_VERSION}")
        if not _DEPLOYMENT_ID_PATTERN.fullmatch(self.deployment_id):
            raise _error(
                "deployment_id",
                "must use lowercase letters, digits, and internal hyphens only",
            )
        if self.profile_color not in COLORS:
            raise _error("profile.color", f"must be one of: {', '.join(COLORS)}")
        if self.profile_shape not in SHAPES:
            raise _error("profile.shape", f"must be one of: {', '.join(SHAPES)}")
        if self.implementation not in IMPLEMENTATIONS:
            raise _error("implementation", f"must be one of: {', '.join(IMPLEMENTATIONS)}")
        if self.strategy not in STRATEGIES:
            raise _error("strategy", f"must be one of: {', '.join(STRATEGIES)}")
        if self.failure_mode not in FAILURE_MODES:
            raise _error("failure_mode", f"must be one of: {', '.join(FAILURE_MODES)}")
        if self.failure_mode not in _FAILURE_MODES_BY_STRATEGY[self.strategy]:
            allowed = ", ".join(sorted(_FAILURE_MODES_BY_STRATEGY[self.strategy]))
            raise _error(
                "failure_mode",
                f"{self.strategy!r} strategy supports only: {allowed}",
            )

        has_environment = self.environment is not None
        has_target_ids = self.target_ids is not None
        if has_environment == has_target_ids:
            raise _error("targets", "set exactly one of environment or target_ids")
        if has_environment and self.environment not in ENVIRONMENTS:
            raise _error("environment", f"must be one of: {', '.join(ENVIRONMENTS)}")
        if has_target_ids:
            if not isinstance(self.target_ids, tuple) or not self.target_ids:
                raise _error("target_ids", "must be a non-empty tuple after validation")
            if len(self.target_ids) > MAX_TARGETS:
                raise _error("target_ids", f"cannot contain more than {MAX_TARGETS} targets")
            if len(set(self.target_ids)) != len(self.target_ids):
                raise _error("target_ids", "must not contain duplicate target IDs")
            unknown = sorted(set(self.target_ids) - KNOWN_TARGET_IDS)
            if unknown:
                raise _error("target_ids", f"unknown target ID(s): {', '.join(unknown)}")

    @property
    def expanded_target_ids(self) -> tuple[str, ...]:
        """Return explicit targets in deterministic environment order."""
        if self.environment is not None:
            return TARGETS_BY_ENVIRONMENT[self.environment]
        assert self.target_ids is not None
        return self.target_ids

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized contract object used for serialization."""
        payload: dict[str, Any] = {
            "deployment_id": self.deployment_id,
            "failure_mode": self.failure_mode,
            "implementation": self.implementation,
            "profile": {"color": self.profile_color, "shape": self.profile_shape},
            "schema_version": self.schema_version,
            "strategy": self.strategy,
        }
        if self.environment is not None:
            payload["environment"] = self.environment
        else:
            payload["target_ids"] = list(self.expanded_target_ids)
        return payload

    def canonical_json(self) -> str:
        """Serialize the contract deterministically for a manifest commit."""
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def validate_deployment_spec(value: object) -> DeploymentSpec:
    """Validate untrusted JSON-compatible data and return an immutable spec."""
    if not isinstance(value, Mapping):
        raise _error("DeploymentSpec", "must be a JSON object")
    _check_exact_keys(value, _TOP_LEVEL_KEYS, "DeploymentSpec")

    required = {
        "deployment_id",
        "failure_mode",
        "implementation",
        "profile",
        "schema_version",
        "strategy",
    }
    missing = sorted(required - set(value))
    if missing:
        raise _error("DeploymentSpec", f"missing required key(s): {', '.join(missing)}")

    schema_version = value["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise _error("schema_version", f"must equal {SCHEMA_VERSION}")
    deployment_id = _require_string(value["deployment_id"], "deployment_id")
    if not _DEPLOYMENT_ID_PATTERN.fullmatch(deployment_id):
        raise _error(
            "deployment_id",
            "must use lowercase letters, digits, and internal hyphens only",
        )
    profile_color, profile_shape = _validate_profile(value["profile"])
    implementation = _require_enum(value["implementation"], "implementation", IMPLEMENTATIONS)
    strategy = _require_enum(value["strategy"], "strategy", STRATEGIES)
    failure_mode = _require_enum(value["failure_mode"], "failure_mode", FAILURE_MODES)

    selectors = [key for key in ("environment", "target_ids") if key in value]
    if len(selectors) != 1:
        raise _error("targets", "set exactly one of environment or target_ids")
    environment: str | None = None
    target_ids: tuple[str, ...] | None = None
    if selectors[0] == "environment":
        environment = _require_enum(value["environment"], "environment", ENVIRONMENTS)
    else:
        target_ids = _validate_target_ids(value["target_ids"])

    return DeploymentSpec(
        deployment_id=deployment_id,
        profile_color=profile_color,
        profile_shape=profile_shape,
        implementation=implementation,
        strategy=strategy,
        failure_mode=failure_mode,
        schema_version=schema_version,
        environment=environment,
        target_ids=target_ids,
    )


def canonicalize_deployment_spec(value: object) -> str:
    """Validate and return the one-line canonical JSON representation."""
    return validate_deployment_spec(value).canonical_json()


def expand_targets(value: object) -> tuple[str, ...]:
    """Validate a spec and return the target IDs selected by it."""
    return validate_deployment_spec(value).expanded_target_ids


def _parse_stdin() -> object:
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise DeploymentSpecError(f"invalid JSON: {exc.msg}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    """Validate a JSON document from stdin and print canonical JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        print(canonicalize_deployment_spec(_parse_stdin()))
    except (DeploymentSpecError, OSError, TypeError, ValueError) as exc:
        print(f"invalid DeploymentSpec: {exc}", file=sys.stderr)
        return 2
    return 0
