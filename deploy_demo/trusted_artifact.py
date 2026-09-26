"""Validate Fleet Deploy manifests and build immutable profile artifacts.

This module is the trusted side of the Fleet Deploy Lab workflow.  It is
checked out from ``master`` and consumes only the JSON bytes selected by the
workflow from the separate manifest branch.  It never interprets a manifest
as Python, shell, or workflow code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .deployment_spec import DeploymentSpec, DeploymentSpecError, validate_deployment_spec
from .fleet_state import FleetProfile

MAX_MANIFEST_BYTES = 16_384
MAX_ARTIFACT_BYTES = 16_384
MAX_PROVENANCE_BYTES = 16_384
EVENT_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
PROFILE_ARTIFACT_SCHEMA = "homeops.fleet-deploy.profile-provenance.v1"
PROVENANCE_KEYS = frozenset(
    {
        "artifact_sha256",
        "artifact_size_bytes",
        "deployment_id",
        "event_commit_sha",
        "manifest_sha256",
        "schema",
    }
)


class TrustedArtifactError(ValueError):
    """Raised when a manifest or generated artifact violates the contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous JSON objects instead of silently keeping the last key."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TrustedArtifactError(f"manifest contains duplicate key: {key}")
        result[key] = value
    return result


def _validate_deployment_id(value: object) -> str:
    """Validate the bounded identifier used to select one manifest path."""
    if not isinstance(value, str) or DEPLOYMENT_ID_PATTERN.fullmatch(value) is None:
        raise TrustedArtifactError(
            "deployment_id must use lowercase letters, digits, and internal hyphens only"
        )
    return value


def validate_event_commit_sha(value: object) -> str:
    """Validate the full lower-case Git object ID supplied by the event."""
    if not isinstance(value, str) or EVENT_COMMIT_SHA_PATTERN.fullmatch(value) is None:
        raise TrustedArtifactError("event_commit_sha must be a 40-character lowercase SHA-1")
    return value


def parse_manifest_bytes(content: bytes) -> DeploymentSpec:
    """Parse and validate exact canonical manifest bytes from untrusted storage."""
    if not isinstance(content, bytes) or not content:
        raise TrustedArtifactError("manifest must be non-empty UTF-8 bytes")
    if len(content) > MAX_MANIFEST_BYTES:
        raise TrustedArtifactError("manifest exceeds the maximum allowed size")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrustedArtifactError("manifest must be UTF-8 JSON") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except TrustedArtifactError:
        raise
    except json.JSONDecodeError as exc:
        raise TrustedArtifactError(f"manifest must contain valid JSON: {exc.msg}") from exc
    try:
        spec = validate_deployment_spec(value)
    except DeploymentSpecError as exc:
        raise TrustedArtifactError(f"invalid DeploymentSpec manifest: {exc}") from exc

    canonical = spec.canonical_json().encode("utf-8")
    if content not in {canonical, canonical + b"\n"}:
        raise TrustedArtifactError(
            "manifest must use canonical DeploymentSpec JSON with at most one trailing newline"
        )
    return spec


@dataclass(frozen=True, slots=True)
class TrustedManifest:
    """A validated manifest bound to its source commit and exact bytes."""

    spec: DeploymentSpec
    raw_bytes: bytes
    event_commit_sha: str

    def __post_init__(self) -> None:
        validate_event_commit_sha(self.event_commit_sha)
        parsed = parse_manifest_bytes(self.raw_bytes)
        if parsed != self.spec:
            raise TrustedArtifactError("trusted manifest spec does not match its raw bytes")

    @property
    def deployment_id(self) -> str:
        """Return the validated deployment identifier."""
        return self.spec.deployment_id

    @property
    def manifest_sha256(self) -> str:
        """Return the digest of the exact manifest bytes selected by the event."""
        return hashlib.sha256(self.raw_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class ProfileArtifact:
    """The exact profile bytes and their content identity."""

    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content:
            raise TrustedArtifactError("profile artifact must be non-empty bytes")
        if not isinstance(self.sha256, str) or SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise TrustedArtifactError("profile artifact SHA-256 must be lowercase hexadecimal")
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise TrustedArtifactError("profile artifact digest does not match its exact bytes")


def load_manifest(
    path: str | Path,
    *,
    deployment_id: str,
    event_commit_sha: str,
) -> TrustedManifest:
    """Read one manifest file and bind it to the dispatch inputs."""
    deployment_id = _validate_deployment_id(deployment_id)
    event_commit_sha = validate_event_commit_sha(event_commit_sha)
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as exc:
        raise TrustedArtifactError(f"unable to read manifest: {type(exc).__name__}") from exc
    spec = parse_manifest_bytes(raw_bytes)
    if spec.deployment_id != deployment_id:
        raise TrustedArtifactError(
            "manifest deployment_id does not match the requested deployment_id"
        )
    return TrustedManifest(
        spec=spec,
        raw_bytes=raw_bytes,
        event_commit_sha=event_commit_sha,
    )


def profile_artifact(manifest: TrustedManifest) -> ProfileArtifact:
    """Derive canonical profile bytes from a validated manifest only."""
    profile = FleetProfile(
        color=manifest.spec.profile_color,
        shape=manifest.spec.profile_shape,
    )
    content = profile.canonical_json().encode("utf-8")
    return ProfileArtifact(content=content, sha256=hashlib.sha256(content).hexdigest())


def _read_bounded(path: str | Path, *, maximum: int, label: str) -> bytes:
    """Read one workflow artifact while keeping untrusted input bounded."""
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        raise TrustedArtifactError(f"unable to read {label}: {type(exc).__name__}") from exc
    if len(content) > maximum:
        raise TrustedArtifactError(f"{label} exceeds the maximum allowed size")
    return content


def _read_provenance(path: str | Path) -> dict[str, object]:
    """Read the exact provenance object emitted beside the profile artifact."""
    content = _read_bounded(path, maximum=MAX_PROVENANCE_BYTES, label="provenance")
    try:
        value = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except TrustedArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustedArtifactError("provenance must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TrustedArtifactError("provenance must be a JSON object")
    if set(value) != PROVENANCE_KEYS:
        raise TrustedArtifactError("provenance keys do not match the trusted schema")
    return value


def verify_artifacts(
    manifest_path: str | Path,
    artifact_path: str | Path,
    provenance_path: str | Path,
    *,
    deployment_id: str,
    event_commit_sha: str,
) -> ProfileArtifact:
    """Revalidate the downloaded spec, profile, and provenance before writes."""
    manifest = load_manifest(
        manifest_path,
        deployment_id=deployment_id,
        event_commit_sha=event_commit_sha,
    )
    expected = profile_artifact(manifest)
    artifact_bytes = _read_bounded(
        artifact_path,
        maximum=MAX_ARTIFACT_BYTES,
        label="profile artifact",
    )
    artifact = ProfileArtifact(
        content=artifact_bytes,
        sha256=hashlib.sha256(artifact_bytes).hexdigest(),
    )
    if artifact != expected:
        raise TrustedArtifactError("profile artifact does not match the validated manifest")

    provenance = _read_provenance(provenance_path)
    if provenance["schema"] != PROFILE_ARTIFACT_SCHEMA:
        raise TrustedArtifactError("provenance schema is not trusted")
    if provenance["deployment_id"] != manifest.deployment_id:
        raise TrustedArtifactError("provenance deployment_id does not match the manifest")
    if provenance["event_commit_sha"] != manifest.event_commit_sha:
        raise TrustedArtifactError("provenance event_commit_sha does not match the dispatch")
    if provenance["manifest_sha256"] != manifest.manifest_sha256:
        raise TrustedArtifactError("provenance manifest_sha256 does not match the manifest")
    if provenance["artifact_sha256"] != artifact.sha256:
        raise TrustedArtifactError("provenance artifact_sha256 does not match the artifact")
    artifact_size = provenance["artifact_size_bytes"]
    if isinstance(artifact_size, bool) or not isinstance(artifact_size, int):
        raise TrustedArtifactError("provenance artifact_size_bytes must be an integer")
    if artifact_size != len(artifact.content):
        raise TrustedArtifactError("provenance artifact_size_bytes does not match the artifact")
    return artifact


def _write_exact(path: str | Path, content: bytes) -> None:
    """Write an output once and refuse a conflicting replacement."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != content:
            raise TrustedArtifactError(f"refusing to replace conflicting output: {destination}")
        return
    destination.write_bytes(content)


def write_artifacts(
    manifest: TrustedManifest,
    *,
    artifact_path: str | Path,
    provenance_path: str | Path,
) -> ProfileArtifact:
    """Write ``profile.json`` and separate source/digest provenance."""
    artifact = profile_artifact(manifest)
    provenance: dict[str, Any] = {
        "artifact_sha256": artifact.sha256,
        "artifact_size_bytes": len(artifact.content),
        "deployment_id": manifest.deployment_id,
        "event_commit_sha": manifest.event_commit_sha,
        "manifest_sha256": manifest.manifest_sha256,
        "schema": PROFILE_ARTIFACT_SCHEMA,
    }
    provenance_bytes = (
        json.dumps(provenance, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_exact(artifact_path, artifact.content)
    _write_exact(provenance_path, provenance_bytes)
    return artifact


def _manifest_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared by the validation and build commands."""
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--event-commit-sha", required=True)


def _input_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments used before any manifest path is constructed."""
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--event-commit-sha", required=True)


def _load_from_args(args: argparse.Namespace) -> TrustedManifest:
    return load_manifest(
        args.manifest,
        deployment_id=args.deployment_id,
        event_commit_sha=args.event_commit_sha,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Validate, build, or verify Fleet Deploy workflow artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate manifest bytes")
    _manifest_arguments(validate_parser)

    inputs_parser = subparsers.add_parser(
        "validate-inputs", help="validate dispatch identity inputs"
    )
    _input_arguments(inputs_parser)

    build_parser = subparsers.add_parser("build", help="build profile and provenance artifacts")
    _manifest_arguments(build_parser)
    build_parser.add_argument("--artifact-out", type=Path, required=True)
    build_parser.add_argument("--provenance-out", type=Path, required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="verify a downloaded profile artifact and its provenance"
    )
    _manifest_arguments(verify_parser)
    verify_parser.add_argument("--artifact", type=Path, required=True)
    verify_parser.add_argument("--provenance", type=Path, required=True)

    try:
        args = parser.parse_args(argv)
        if args.command == "validate-inputs":
            deployment_id = _validate_deployment_id(args.deployment_id)
            event_commit_sha = validate_event_commit_sha(args.event_commit_sha)
            print(
                json.dumps(
                    {
                        "deployment_id": deployment_id,
                        "event_commit_sha": event_commit_sha,
                        "status": "valid",
                    },
                    sort_keys=True,
                )
            )
            return 0

        manifest = _load_from_args(args)
        if args.command == "validate":
            artifact = profile_artifact(manifest)
            print(
                json.dumps(
                    {
                        "deployment_id": manifest.deployment_id,
                        "event_commit_sha": manifest.event_commit_sha,
                        "manifest_sha256": manifest.manifest_sha256,
                        "profile_sha256": artifact.sha256,
                        "status": "valid",
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "verify":
            artifact = verify_artifacts(
                args.manifest,
                args.artifact,
                args.provenance,
                deployment_id=args.deployment_id,
                event_commit_sha=args.event_commit_sha,
            )
            print(
                json.dumps(
                    {
                        "artifact_sha256": artifact.sha256,
                        "deployment_id": manifest.deployment_id,
                        "event_commit_sha": manifest.event_commit_sha,
                        "status": "verified",
                    },
                    sort_keys=True,
                )
            )
        else:
            artifact = write_artifacts(
                manifest,
                artifact_path=args.artifact_out,
                provenance_path=args.provenance_out,
            )
            print(
                json.dumps(
                    {
                        "artifact": str(args.artifact_out),
                        "artifact_sha256": artifact.sha256,
                        "deployment_id": manifest.deployment_id,
                        "event_commit_sha": manifest.event_commit_sha,
                        "provenance": str(args.provenance_out),
                        "status": "built",
                    },
                    sort_keys=True,
                )
            )
    except (OSError, TrustedArtifactError, TypeError, ValueError) as exc:
        print(f"trusted Fleet Deploy artifact failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
