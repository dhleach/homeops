"""Dependency-free Python deployer for the Fleet Deploy Lab simulator.

The deployer is intentionally a small control-plane client rather than a
second simulator implementation.  It validates the shared ``DeploymentSpec``,
loads an immutable canonical profile artifact, calls the protected queue/apply
API, and performs an independent fresh readback before reporting success.
HTTP 200 is therefore only transport success; the observed fleet state must
also match the requested version, profile, target set, and digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .deployment_spec import KNOWN_TARGET_IDS, DeploymentSpec, validate_deployment_spec
from .fleet_state import FleetProfile

API_BASE_URL_ENV = "FLEET_DEPLOY_API_URL"
API_KEY_ENV = "FLEET_DEPLOY_API_KEY"
MAX_ARTIFACT_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_048_576
_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DeploymentEventType = Literal[
    "deployment_started",
    "deployment_queued",
    "deployment_applying",
    "deployment_verified",
    "deployment_failed",
]


class DeployerError(RuntimeError):
    """Base class for safe, user-facing deployer failures."""


class ArtifactIdentityError(DeployerError):
    """Raised when the profile artifact is not immutable and canonical."""


class FleetApiError(DeployerError):
    """Raised when the Fleet API cannot provide a usable JSON response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DeploymentVerificationError(DeployerError):
    """Raised when readback does not prove the requested deployment succeeded."""


def _require_sha256(value: object, path: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ArtifactIdentityError(f"{path} must be a lowercase SHA-256 digest")
    return value


def _require_deployment_id(value: object) -> str:
    if not isinstance(value, str) or _DEPLOYMENT_ID_PATTERN.fullmatch(value) is None:
        raise DeployerError(
            "deployment_id must use lowercase letters, digits, and internal hyphens only"
        )
    return value


@dataclass(frozen=True, slots=True)
class DeploymentArtifact:
    """An immutable canonical ``profile.json`` plus its content identity.

    The artifact is deliberately only the finite profile object.  Its exact
    bytes must equal ``FleetProfile.canonical_json()``; accepting semantically
    equivalent but byte-different JSON would make the displayed digest
    meaningless.  The digest also equals the simulator's desired/observed
    profile digest.
    """

    content: bytes
    sha256: str
    profile: FleetProfile

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise ArtifactIdentityError("artifact content must be bytes")
        digest = hashlib.sha256(self.content).hexdigest()
        if self.sha256 != digest:
            raise ArtifactIdentityError("artifact SHA-256 does not match its exact bytes")
        _require_sha256(self.sha256)
        canonical = self.profile.canonical_json().encode("utf-8")
        if self.content != canonical:
            raise ArtifactIdentityError("artifact bytes are not canonical profile JSON")
        if self.profile.digest != self.sha256:
            raise ArtifactIdentityError("artifact digest does not match the profile digest")

    @property
    def digest(self) -> str:
        """Alias used by callers that refer to an artifact's content digest."""
        return self.sha256

    @property
    def artifact_sha256(self) -> str:
        """Explicit alias for event and workflow provenance code."""
        return self.sha256

    @classmethod
    def from_bytes(
        cls,
        content: bytes,
        *,
        expected_sha256: str | None = None,
    ) -> DeploymentArtifact:
        """Parse and identity-check exact canonical profile bytes."""
        if not isinstance(content, bytes) or not content:
            raise ArtifactIdentityError("profile artifact must be non-empty bytes")
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ArtifactIdentityError("profile artifact is too large")
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactIdentityError("profile artifact must be UTF-8 JSON") from exc
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise ArtifactIdentityError("profile artifact must contain valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ArtifactIdentityError("profile artifact must be a JSON object")
        try:
            profile = FleetProfile.from_mapping(payload)
        except (TypeError, ValueError) as exc:
            raise ArtifactIdentityError(f"invalid profile artifact: {exc}") from exc
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None:
            expected = _require_sha256(expected_sha256, "expected_sha256")
            if digest != expected:
                raise ArtifactIdentityError("profile artifact digest does not match expectation")
        canonical = profile.canonical_json().encode("utf-8")
        if content != canonical:
            raise ArtifactIdentityError(
                "profile artifact must use compact, sorted-key canonical JSON bytes"
            )
        return cls(content=content, sha256=digest, profile=profile)

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        expected_sha256: str | None = None,
    ) -> DeploymentArtifact:
        """Read an artifact once and bind all later checks to its digest."""
        try:
            content = Path(path).read_bytes()
        except OSError as exc:
            raise ArtifactIdentityError(
                f"unable to read profile artifact: {type(exc).__name__}"
            ) from exc
        return cls.from_bytes(content, expected_sha256=expected_sha256)


# A profile-specific name is useful to callers while keeping one public type.
ProfileArtifact = DeploymentArtifact


@dataclass(frozen=True, slots=True)
class DeploymentEvent:
    """Structured lifecycle evidence; every event is keyed by deployment ID."""

    deployment_id: str
    event_type: DeploymentEventType
    schema_version: int
    artifact_sha256: str
    target_ids: tuple[str, ...]
    status: str
    detail: str | None = None

    @property
    def type(self) -> DeploymentEventType:
        """Short compatibility alias for event consumers."""
        return self.event_type

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "event_type": self.event_type,
            "deployment_id": self.deployment_id,
            "schema_version": self.schema_version,
            "artifact_sha256": self.artifact_sha256,
            "target_ids": list(self.target_ids),
            "status": self.status,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class DeploymentApi(Protocol):
    """The small API surface needed by the deployer, easy to fake in tests."""

    def queue_deployment(self, spec: DeploymentSpec) -> Mapping[str, Any]:
        """Persist the desired deployment state."""

    def apply_deployment(self, deployment_id: str) -> Mapping[str, Any]:
        """Apply the queued state through the protected simulator API."""

    def read_deployment(self, deployment_id: str) -> Mapping[str, Any]:
        """Read fresh observed state for independent verification."""


RequestOpener = Callable[..., Any]


class FleetApiClient:
    """Standard-library HTTP client for the protected Fleet Deploy API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        opener: RequestOpener | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise FleetApiError("Fleet API base URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise FleetApiError("Fleet API base URL must not contain a query or fragment")
        if not isinstance(api_key, str) or not api_key.strip():
            raise FleetApiError("Fleet API credential is required")
        if timeout <= 0:
            raise FleetApiError("Fleet API timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self.timeout = timeout
        self._opener = opener or build_opener().open

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status_code = int(response.getcode())
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise FleetApiError(
                f"Fleet API returned HTTP {exc.code} for {method} {path}",
                status_code=exc.code,
            ) from None
        except (OSError, URLError) as exc:
            raise FleetApiError(
                f"Fleet API request failed for {method} {path}: {type(exc).__name__}"
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FleetApiError(f"Fleet API response was too large for {method} {path}")
        if not 200 <= status_code < 300:
            raise FleetApiError(
                f"Fleet API returned HTTP {status_code} for {method} {path}",
                status_code=status_code,
            )
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FleetApiError(f"Fleet API returned invalid JSON for {method} {path}") from None
        if not isinstance(decoded, Mapping):
            raise FleetApiError(f"Fleet API returned a non-object response for {method} {path}")
        return decoded

    def queue_deployment(self, spec: DeploymentSpec) -> Mapping[str, Any]:
        """Queue a normalized DeploymentSpec using the dedicated bearer key."""
        return self._request_json("POST", "deployments", spec.to_dict())

    def apply_deployment(self, deployment_id: str) -> Mapping[str, Any]:
        """Apply one validated deployment ID."""
        deployment_id = _require_deployment_id(deployment_id)
        return self._request_json("POST", f"deployments/{deployment_id}/apply")

    def read_deployment(self, deployment_id: str) -> Mapping[str, Any]:
        """Read one deployment without credentials so verification uses the public path."""
        deployment_id = _require_deployment_id(deployment_id)
        return self._request_json("GET", f"deployments/{deployment_id}")

    # Concise aliases make the client convenient in small workflow adapters.
    queue = queue_deployment
    apply = apply_deployment
    read = read_deployment


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    """Successful deployment result with the final fresh readback and events."""

    spec: DeploymentSpec
    artifact: DeploymentArtifact
    response: Mapping[str, Any]
    events: tuple[DeploymentEvent, ...]

    @property
    def deployment_id(self) -> str:
        return self.spec.deployment_id

    @property
    def target_ids(self) -> tuple[str, ...]:
        return self.spec.expanded_target_ids

    @property
    def artifact_sha256(self) -> str:
        return self.artifact.sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "schema_version": self.spec.schema_version,
            "artifact_sha256": self.artifact.sha256,
            "target_ids": list(self.target_ids),
            "status": self.response.get("status"),
            "verified": self.response.get("verified"),
            "response": dict(self.response),
            "events": [event.to_dict() for event in self.events],
        }


EventSink = Callable[[DeploymentEvent], None]


def _as_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeploymentVerificationError(f"{path} must be a JSON object")
    return value


def _as_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeploymentVerificationError(f"{path} must be a non-empty string")
    return value


def _as_target_ids(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeploymentVerificationError(f"{path} must be a JSON array of strings")
    target_ids = tuple(value)
    if any(target_id not in KNOWN_TARGET_IDS for target_id in target_ids):
        raise DeploymentVerificationError(f"{path} contains an unknown target")
    if len(set(target_ids)) != len(target_ids):
        raise DeploymentVerificationError(f"{path} contains duplicate targets")
    return target_ids


def _assert_profile(value: object, expected: FleetProfile, path: str) -> None:
    profile = _as_mapping(value, path)
    try:
        actual = FleetProfile.from_mapping(profile)
    except (TypeError, ValueError) as exc:
        raise DeploymentVerificationError(f"{path} is invalid: {exc}") from exc
    if actual != expected:
        raise DeploymentVerificationError(
            f"{path} mismatch: expected {expected.to_dict()}, got {actual.to_dict()}"
        )


def _assert_optional_version(payload: Mapping[str, Any], expected: int, path: str) -> None:
    for key in ("schema_version", "version"):
        if key in payload and payload[key] != expected:
            raise DeploymentVerificationError(
                f"{path}.{key} mismatch: expected {expected}, got {payload[key]!r}"
            )


class FleetDeployer:
    """Apply a validated spec and fail closed unless fresh readback verifies it."""

    def __init__(self, client: DeploymentApi, *, event_sink: EventSink | None = None) -> None:
        self.client = client
        self.event_sink = event_sink

    @staticmethod
    def _normalize_spec(value: DeploymentSpec | Mapping[str, object]) -> DeploymentSpec:
        if isinstance(value, DeploymentSpec):
            return value
        return validate_deployment_spec(value)

    @staticmethod
    def _normalize_artifact(
        value: DeploymentArtifact | bytes | bytearray | str | Path,
    ) -> DeploymentArtifact:
        if isinstance(value, DeploymentArtifact):
            return value
        if isinstance(value, (bytes, bytearray)):
            return DeploymentArtifact.from_bytes(bytes(value))
        if isinstance(value, (str, Path)):
            return DeploymentArtifact.from_path(value)
        raise ArtifactIdentityError(
            "artifact must be canonical bytes, a path, or DeploymentArtifact"
        )

    @staticmethod
    def _validate_artifact_matches_spec(
        spec: DeploymentSpec,
        artifact: DeploymentArtifact,
    ) -> None:
        expected = FleetProfile(spec.profile_color, spec.profile_shape)
        if artifact.profile != expected:
            raise ArtifactIdentityError(
                "profile artifact does not match the validated DeploymentSpec"
            )
        if artifact.sha256 != expected.digest:
            raise ArtifactIdentityError(
                "profile artifact digest does not match the requested profile"
            )
        target_ids = spec.expanded_target_ids
        if not target_ids or any(target_id not in KNOWN_TARGET_IDS for target_id in target_ids):
            raise ArtifactIdentityError("DeploymentSpec resolved to an unknown target")
        if len(set(target_ids)) != len(target_ids):
            raise ArtifactIdentityError("DeploymentSpec resolved to duplicate targets")

    def _emit(
        self,
        events: list[DeploymentEvent],
        spec: DeploymentSpec,
        artifact: DeploymentArtifact,
        event_type: DeploymentEventType,
        status: str,
        detail: str | None = None,
    ) -> None:
        event = DeploymentEvent(
            deployment_id=spec.deployment_id,
            event_type=event_type,
            schema_version=spec.schema_version,
            artifact_sha256=artifact.sha256,
            target_ids=spec.expanded_target_ids,
            status=status,
            detail=detail,
        )
        events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

    @staticmethod
    def _validate_snapshot(
        payload: Mapping[str, Any],
        spec: DeploymentSpec,
        artifact: DeploymentArtifact,
        *,
        phase: str,
        allowed_statuses: frozenset[str],
        require_observed: bool,
    ) -> str:
        deployment_id = _as_string(payload.get("deployment_id"), f"{phase}.deployment_id")
        if deployment_id != spec.deployment_id:
            raise DeploymentVerificationError(
                f"{phase}.deployment_id mismatch: expected {spec.deployment_id}, "
                f"got {deployment_id}"
            )
        _assert_optional_version(payload, spec.schema_version, phase)
        if payload.get("simulated", True) is not True:
            raise DeploymentVerificationError(f"{phase} is not marked simulated")
        if payload.get("target_kind", "simulated") != "simulated":
            raise DeploymentVerificationError(f"{phase} has an unexpected target kind")

        target_ids = _as_target_ids(payload.get("target_ids"), f"{phase}.target_ids")
        expected_target_ids = spec.expanded_target_ids
        if target_ids != expected_target_ids:
            raise DeploymentVerificationError(
                f"{phase}.target_ids mismatch: expected {list(expected_target_ids)}, "
                f"got {list(target_ids)}"
            )
        _assert_profile(
            payload.get("desired"),
            FleetProfile(spec.profile_color, spec.profile_shape),
            f"{phase}.desired",
        )
        if payload.get("desired_digest") != artifact.sha256:
            raise DeploymentVerificationError(
                f"{phase}.desired_digest mismatch: expected {artifact.sha256}, "
                f"got {payload.get('desired_digest')!r}"
            )
        status = _as_string(payload.get("status"), f"{phase}.status")
        if status not in allowed_statuses:
            raise DeploymentVerificationError(
                f"{phase}.status must be one of {sorted(allowed_statuses)}, got {status!r}"
            )

        targets = payload.get("targets")
        if not isinstance(targets, list):
            raise DeploymentVerificationError(f"{phase}.targets must be a JSON array")
        actual_target_ids: list[str] = []
        expected_profile = FleetProfile(spec.profile_color, spec.profile_shape)
        for index, target_value in enumerate(targets):
            target = _as_mapping(target_value, f"{phase}.targets[{index}]")
            target_id = _as_string(target.get("target_id"), f"{phase}.targets[{index}].target_id")
            actual_target_ids.append(target_id)
            if target_id not in expected_target_ids:
                raise DeploymentVerificationError(
                    f"{phase}.targets[{index}] contains an unexpected target"
                )
            if target.get("simulated", True) is not True:
                raise DeploymentVerificationError(
                    f"{phase}.targets[{index}] is not marked simulated"
                )
            _assert_profile(
                target.get("desired"), expected_profile, f"{phase}.targets[{index}].desired"
            )
            if target.get("desired_digest") != artifact.sha256:
                raise DeploymentVerificationError(
                    f"{phase}.targets[{index}].desired_digest does not match artifact"
                )
            if require_observed:
                _assert_profile(
                    target.get("observed"),
                    expected_profile,
                    f"{phase}.targets[{index}].observed",
                )
                if target.get("observed_digest") != artifact.sha256:
                    raise DeploymentVerificationError(
                        f"{phase}.targets[{index}].observed_digest does not match artifact"
                    )
                if target.get("status") != "succeeded":
                    raise DeploymentVerificationError(
                        f"{phase}.targets[{index}].status is not succeeded"
                    )
                if target.get("active_deployment_id") != spec.deployment_id:
                    raise DeploymentVerificationError(
                        f"{phase}.targets[{index}].active_deployment_id mismatch"
                    )
        if tuple(actual_target_ids) != expected_target_ids:
            raise DeploymentVerificationError(
                f"{phase}.targets do not contain the expected target order"
            )
        return status

    def deploy(
        self,
        spec: DeploymentSpec | Mapping[str, object],
        artifact: DeploymentArtifact | bytes | bytearray | str | Path,
    ) -> DeploymentResult:
        """Queue, apply, and independently verify one immutable artifact."""
        validated_spec = self._normalize_spec(spec)
        validated_artifact = self._normalize_artifact(artifact)
        self._validate_artifact_matches_spec(validated_spec, validated_artifact)
        events: list[DeploymentEvent] = []

        try:
            self._emit(
                events,
                validated_spec,
                validated_artifact,
                "deployment_started",
                "started",
            )
            queued = self.client.queue_deployment(validated_spec)
            queue_status = self._validate_snapshot(
                queued,
                validated_spec,
                validated_artifact,
                phase="queue",
                # A public status poll can reconcile the same in-flight
                # workflow to ``applying`` before this protected queue
                # response arrives.  That is a truthful intermediate state,
                # not a queue failure; the protected apply and fresh readback
                # below remain the authoritative success gates.
                allowed_statuses=frozenset({"queued", "applying", "succeeded"}),
                require_observed=False,
            )
            self._emit(
                events,
                validated_spec,
                validated_artifact,
                "deployment_queued",
                queue_status,
            )
            self._emit(
                events,
                validated_spec,
                validated_artifact,
                "deployment_applying",
                "applying",
            )
            applied = self.client.apply_deployment(validated_spec.deployment_id)
            apply_status = self._validate_snapshot(
                applied,
                validated_spec,
                validated_artifact,
                phase="apply",
                allowed_statuses=frozenset({"succeeded"}),
                require_observed=False,
            )
            fresh = self.client.read_deployment(validated_spec.deployment_id)
            self._validate_snapshot(
                fresh,
                validated_spec,
                validated_artifact,
                phase="readback",
                allowed_statuses=frozenset({"succeeded"}),
                require_observed=True,
            )
            if fresh.get("verification") != "verified" or fresh.get("verified") is not True:
                raise DeploymentVerificationError("readback did not report verified observed state")
            self._emit(
                events,
                validated_spec,
                validated_artifact,
                "deployment_verified",
                apply_status,
            )
            return DeploymentResult(
                spec=validated_spec,
                artifact=validated_artifact,
                response=fresh,
                events=tuple(events),
            )
        except Exception as exc:
            if not events or events[-1].event_type != "deployment_failed":
                detail = str(exc) if isinstance(exc, DeployerError) else type(exc).__name__
                self._emit(
                    events,
                    validated_spec,
                    validated_artifact,
                    "deployment_failed",
                    "failed",
                    detail=detail,
                )
            if isinstance(exc, DeployerError):
                raise
            raise DeployerError(f"deployment failed: {type(exc).__name__}") from exc


PythonDeployer = FleetDeployer


def deploy(
    spec: DeploymentSpec | Mapping[str, object],
    artifact: DeploymentArtifact | bytes | bytearray | str | Path,
    client: DeploymentApi,
    *,
    event_sink: EventSink | None = None,
) -> DeploymentResult:
    """Convenience function for workflow adapters and small scripts."""
    return FleetDeployer(client, event_sink=event_sink).deploy(spec, artifact)


def _read_spec(path: Path) -> Mapping[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError as exc:
        raise DeployerError(f"unable to read DeploymentSpec: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise DeployerError(f"DeploymentSpec is invalid JSON: {exc.msg}") from exc
    if not isinstance(value, Mapping):
        raise DeployerError("DeploymentSpec must be a JSON object")
    return value


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default=os.environ.get(API_BASE_URL_ENV))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-sha256")
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the deployer from a JSON spec and canonical profile artifact."""
    try:
        args = _parse_args(argv)
        if not args.api_base_url:
            raise DeployerError(f"set --api-base-url or {API_BASE_URL_ENV}")
        api_key = os.environ.get(API_KEY_ENV, "")
        client = FleetApiClient(args.api_base_url, api_key, timeout=args.timeout)
        result = FleetDeployer(client).deploy(
            _read_spec(args.spec),
            DeploymentArtifact.from_path(args.artifact, expected_sha256=args.artifact_sha256),
        )
    except (DeployerError, OSError, TypeError, ValueError) as exc:
        print(f"deployment failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=True, sort_keys=True))
    return 0


__all__ = [
    "API_BASE_URL_ENV",
    "API_KEY_ENV",
    "ArtifactIdentityError",
    "DeploymentApi",
    "DeploymentArtifact",
    "DeploymentEvent",
    "DeploymentEventType",
    "DeploymentResult",
    "DeploymentVerificationError",
    "DeployerError",
    "FleetApiClient",
    "FleetApiError",
    "FleetDeployer",
    "ProfileArtifact",
    "PythonDeployer",
    "deploy",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
