"""Contract tests for the trusted Fleet Deploy artifact builder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deploy_demo.trusted_artifact import (
    PROFILE_ARTIFACT_SCHEMA,
    TrustedArtifactError,
    load_manifest,
    main,
    parse_manifest_bytes,
    profile_artifact,
    write_artifacts,
)

EVENT_SHA = "a" * 40


def manifest_bytes(**overrides: object) -> bytes:
    """Return one canonical manifest with optional field replacements."""
    value: dict[str, object] = {
        "deployment_id": "demo-trusted-001",
        "environment": "test",
        "failure_mode": "rollback",
        "implementation": "python",
        "profile": {"color": "purple", "shape": "hexagon"},
        "schema_version": 1,
        "strategy": "rolling",
    }
    value.update(overrides)
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def write_manifest(tmp_path: Path, content: bytes | None = None) -> Path:
    """Write a test manifest to a temporary path."""
    path = tmp_path / "manifest.json"
    path.write_bytes(content or manifest_bytes())
    return path


def test_parse_manifest_reuses_canonical_deployment_spec_validation() -> None:
    """A valid manifest returns the shared immutable DeploymentSpec."""
    spec = parse_manifest_bytes(manifest_bytes())

    assert spec.deployment_id == "demo-trusted-001"
    assert spec.expanded_target_ids == (
        "test-vehicle-01",
        "test-vehicle-02",
        "test-vehicle-03",
        "test-vehicle-04",
    )


def test_duplicate_json_keys_fail_closed() -> None:
    """The trusted reader must not let JSON last-key-wins ambiguity through."""
    content = b'{"deployment_id":"demo-trusted-001","deployment_id":"other"}'

    with pytest.raises(TrustedArtifactError, match="duplicate key"):
        parse_manifest_bytes(content)


def test_noncanonical_manifest_bytes_fail_closed() -> None:
    """Whitespace changes do not become a second accepted manifest identity."""
    content = manifest_bytes().replace(b'{"deployment_id"', b'{ "deployment_id"')

    with pytest.raises(TrustedArtifactError, match="canonical"):
        parse_manifest_bytes(content)


def test_manifest_binds_deployment_and_event_identity(tmp_path: Path) -> None:
    """The selected path and event commit cannot be substituted independently."""
    path = write_manifest(tmp_path)

    trusted = load_manifest(path, deployment_id="demo-trusted-001", event_commit_sha=EVENT_SHA)

    assert trusted.deployment_id == "demo-trusted-001"
    assert trusted.event_commit_sha == EVENT_SHA
    assert trusted.manifest_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(TrustedArtifactError, match="does not match"):
        load_manifest(path, deployment_id="demo-other", event_commit_sha=EVENT_SHA)
    with pytest.raises(TrustedArtifactError, match="40-character"):
        load_manifest(path, deployment_id="demo-trusted-001", event_commit_sha="deadbeef")


def test_profile_artifact_is_exact_canonical_profile_bytes(tmp_path: Path) -> None:
    """The artifact contains only the finite profile, with no event metadata."""
    trusted = load_manifest(
        write_manifest(tmp_path),
        deployment_id="demo-trusted-001",
        event_commit_sha=EVENT_SHA,
    )

    artifact = profile_artifact(trusted)

    assert artifact.content == b'{"color":"purple","shape":"hexagon"}'
    assert artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()
    assert EVENT_SHA.encode() not in artifact.content


def test_write_artifacts_keeps_event_commit_separate_from_profile_digest(tmp_path: Path) -> None:
    """Provenance records both identities without changing profile.json bytes."""
    trusted = load_manifest(
        write_manifest(tmp_path),
        deployment_id="demo-trusted-001",
        event_commit_sha=EVENT_SHA,
    )
    artifact_path = tmp_path / "out" / "profile.json"
    provenance_path = tmp_path / "out" / "profile-provenance.json"

    artifact = write_artifacts(
        trusted,
        artifact_path=artifact_path,
        provenance_path=provenance_path,
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

    assert artifact_path.read_bytes() == artifact.content
    assert provenance["schema"] == PROFILE_ARTIFACT_SCHEMA
    assert provenance["event_commit_sha"] == EVENT_SHA
    assert provenance["artifact_sha256"] == artifact.sha256
    assert provenance["event_commit_sha"] != provenance["artifact_sha256"]
    assert provenance["artifact_size_bytes"] == len(artifact.content)


def test_conflicting_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    """A rerun cannot silently replace an artifact with different bytes."""
    trusted = load_manifest(
        write_manifest(tmp_path),
        deployment_id="demo-trusted-001",
        event_commit_sha=EVENT_SHA,
    )
    artifact_path = tmp_path / "profile.json"
    provenance_path = tmp_path / "provenance.json"
    write_artifacts(trusted, artifact_path=artifact_path, provenance_path=provenance_path)
    artifact_path.write_bytes(b"tampered")

    with pytest.raises(TrustedArtifactError, match="conflicting output"):
        write_artifacts(trusted, artifact_path=artifact_path, provenance_path=provenance_path)


def test_cli_build_emits_identity_and_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow-facing CLI reports the same digest it writes."""
    manifest = write_manifest(tmp_path)
    artifact = tmp_path / "profile.json"
    provenance = tmp_path / "provenance.json"

    assert (
        main(
            [
                "build",
                "--manifest",
                str(manifest),
                "--deployment-id",
                "demo-trusted-001",
                "--event-commit-sha",
                EVENT_SHA,
                "--artifact-out",
                str(artifact),
                "--provenance-out",
                str(provenance),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "built"
    assert output["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert output["event_commit_sha"] == EVENT_SHA
