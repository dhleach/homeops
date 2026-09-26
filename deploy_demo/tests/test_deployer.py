"""Focused tests for the local Fleet Deploy Lab Python deployer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.error import HTTPError

import pytest

import deploy_demo.deployer as deployer
from deploy_demo import (
    ArtifactIdentityError,
    DeploymentArtifact,
    DeploymentSpecError,
    DeploymentVerificationError,
    FleetApiClient,
    FleetApiError,
    FleetDeployer,
    FleetProfile,
    validate_deployment_spec,
)


def valid_spec(**overrides: object):
    """Return a valid shared contract for deployer tests."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": "demo-deployer-001",
        "environment": "test",
        "profile": {"color": "purple", "shape": "hexagon"},
        "implementation": "python",
        "strategy": "rolling",
        "failure_mode": "rollback",
    }
    payload.update(overrides)
    if "target_ids" in overrides:
        payload.pop("environment", None)
    return validate_deployment_spec(payload)


def artifact_for(spec) -> DeploymentArtifact:
    profile = FleetProfile(spec.profile_color, spec.profile_shape)
    return DeploymentArtifact.from_bytes(profile.canonical_json().encode("utf-8"))


def snapshot(
    spec,
    artifact,
    *,
    status: str,
    verified: bool,
    observed: Mapping[str, str] | None = None,
    target_status: str = "succeeded",
) -> dict[str, object]:
    desired = artifact.profile.to_dict()
    observed_profile = dict(observed or desired)
    targets = []
    for target_id in spec.expanded_target_ids:
        targets.append(
            {
                "target_id": target_id,
                "label": target_id.upper(),
                "environment": target_id.split("-vehicle-", 1)[0],
                "simulated": True,
                "desired": desired,
                "desired_digest": artifact.sha256,
                "observed": observed_profile,
                "observed_digest": hashlib.sha256(
                    json.dumps(observed_profile, separators=(",", ":"), sort_keys=True).encode()
                ).hexdigest(),
                "status": target_status,
                "active_deployment_id": spec.deployment_id,
                "last_error": None,
                "updated_at": "2026-09-26T00:00:00Z",
            }
        )
    return {
        "simulated": True,
        "target_kind": "simulated",
        "deployment_id": spec.deployment_id,
        "target_ids": list(spec.expanded_target_ids),
        "desired": desired,
        "desired_digest": artifact.sha256,
        "status": status,
        "error": None,
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:00Z",
        "verification": "verified" if verified else "pending",
        "verified": verified,
        "targets": targets,
        "idempotent": False,
    }


@dataclass
class FakeClient:
    queue_response: Mapping[str, object]
    apply_response: Mapping[str, object]
    read_response: Mapping[str, object]
    calls: list[tuple[str, object]] = field(default_factory=list)

    def queue_deployment(self, spec) -> Mapping[str, object]:
        self.calls.append(("queue", spec))
        return self.queue_response

    def apply_deployment(self, deployment_id: str) -> Mapping[str, object]:
        self.calls.append(("apply", deployment_id))
        return self.apply_response

    def read_deployment(self, deployment_id: str) -> Mapping[str, object]:
        self.calls.append(("read", deployment_id))
        return self.read_response


def test_artifact_requires_exact_canonical_bytes_and_expected_digest() -> None:
    spec = valid_spec()
    artifact = artifact_for(spec)

    assert artifact.sha256 == artifact.profile.digest
    assert artifact.artifact_sha256 == artifact.sha256
    assert artifact.content == b'{"color":"purple","shape":"hexagon"}'

    with pytest.raises(ArtifactIdentityError, match="canonical"):
        DeploymentArtifact.from_bytes(b'{"shape": "hexagon", "color": "purple"}')
    with pytest.raises(ArtifactIdentityError, match="does not match expectation"):
        DeploymentArtifact.from_bytes(artifact.content, expected_sha256="0" * 64)


def test_deployer_queues_applies_and_verifies_fresh_observed_state() -> None:
    spec = valid_spec(environment=None, target_ids=["stage-vehicle-02", "test-vehicle-01"])
    artifact = artifact_for(spec)
    queued = snapshot(
        spec,
        artifact,
        status="queued",
        verified=False,
        observed={"color": "blue", "shape": "circle"},
        target_status="pending",
    )
    applied = snapshot(spec, artifact, status="succeeded", verified=True)
    client = FakeClient(queued, applied, applied)
    events = []

    result = FleetDeployer(client, event_sink=events.append).deploy(spec, artifact)

    assert [name for name, _ in client.calls] == ["queue", "apply", "read"]
    assert client.calls[0][1].expanded_target_ids == (
        "test-vehicle-01",
        "stage-vehicle-02",
    )
    assert result.deployment_id == spec.deployment_id
    assert result.target_ids == spec.expanded_target_ids
    assert result.artifact_sha256 == artifact.sha256
    assert result.response["verified"] is True
    assert [event.event_type for event in events] == [
        "deployment_started",
        "deployment_queued",
        "deployment_applying",
        "deployment_verified",
    ]
    assert all(event.deployment_id == spec.deployment_id for event in events)
    assert all(event.artifact_sha256 == artifact.sha256 for event in events)


def test_deployer_accepts_concurrent_applying_queue_state() -> None:
    """Public polling may advance the durable row before queue returns."""
    spec = valid_spec(target_ids=["test-vehicle-01"])
    artifact = artifact_for(spec)
    queued = snapshot(
        spec,
        artifact,
        status="applying",
        verified=False,
        observed={"color": "blue", "shape": "circle"},
        target_status="pending",
    )
    completed = snapshot(spec, artifact, status="succeeded", verified=True)
    client = FakeClient(queued, completed, completed)
    events = []

    result = FleetDeployer(client, event_sink=events.append).deploy(spec, artifact)

    assert result.response["verified"] is True
    assert [name for name, _ in client.calls] == ["queue", "apply", "read"]
    assert events[1].event_type == "deployment_queued"
    assert events[1].status == "applying"


def test_repeated_runs_are_safe_when_api_returns_idempotent_success() -> None:
    spec = valid_spec(target_ids=["test-vehicle-01"])
    artifact = artifact_for(spec)
    queued = snapshot(spec, artifact, status="succeeded", verified=True)
    queued["idempotent"] = True
    client = FakeClient(queued, queued, queued)

    first = FleetDeployer(client).deploy(spec, artifact)
    second = FleetDeployer(client).deploy(spec, artifact)

    assert first.response == second.response
    assert [name for name, _ in client.calls] == [
        "queue",
        "apply",
        "read",
        "queue",
        "apply",
        "read",
    ]


def test_artifact_profile_mismatch_fails_before_any_fleet_write() -> None:
    spec = valid_spec(target_ids=["prod-vehicle-04"])
    wrong_artifact = DeploymentArtifact.from_bytes(b'{"color":"green","shape":"square"}')
    client = FakeClient({}, {}, {})

    with pytest.raises(ArtifactIdentityError, match="does not match"):
        FleetDeployer(client).deploy(spec, wrong_artifact)

    assert client.calls == []


def test_http_200_with_observed_mismatch_fails_and_emits_failure_event() -> None:
    spec = valid_spec(target_ids=["test-vehicle-01"])
    artifact = artifact_for(spec)
    queued = snapshot(
        spec,
        artifact,
        status="queued",
        verified=False,
        observed={"color": "blue", "shape": "circle"},
        target_status="pending",
    )
    mismatch = snapshot(
        spec,
        artifact,
        status="succeeded",
        verified=True,
        observed={"color": "blue", "shape": "circle"},
    )
    client = FakeClient(queued, mismatch, mismatch)
    events = []

    with pytest.raises(DeploymentVerificationError, match="observed"):
        FleetDeployer(client, event_sink=events.append).deploy(spec, artifact)

    assert events[-1].event_type == "deployment_failed"
    assert events[-1].deployment_id == spec.deployment_id


def test_http_200_failed_apply_status_is_not_success() -> None:
    spec = valid_spec(target_ids=["test-vehicle-02"])
    artifact = artifact_for(spec)
    queued = snapshot(
        spec,
        artifact,
        status="queued",
        verified=False,
        observed={"color": "blue", "shape": "circle"},
        target_status="pending",
    )
    failed = snapshot(
        spec,
        artifact,
        status="failed",
        verified=False,
        observed={"color": "blue", "shape": "circle"},
        target_status="failed",
    )
    client = FakeClient(queued, failed, failed)

    with pytest.raises(DeploymentVerificationError, match="apply.status"):
        FleetDeployer(client).deploy(spec, artifact)

    assert [name for name, _ in client.calls] == ["queue", "apply"]


def test_invalid_target_is_rejected_by_shared_contract_before_network() -> None:
    payload = {
        "schema_version": 1,
        "deployment_id": "demo-invalid",
        "target_ids": ["test-vehicle-99"],
        "profile": {"color": "blue", "shape": "circle"},
        "implementation": "python",
        "strategy": "rolling",
        "failure_mode": "rollback",
    }
    artifact = DeploymentArtifact.from_bytes(b'{"color":"blue","shape":"circle"}')
    client = FakeClient({}, {}, {})

    with pytest.raises(DeploymentSpecError, match="unknown target"):
        FleetDeployer(client).deploy(payload, artifact)
    assert client.calls == []


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, _limit=-1):
        return self.body


def test_http_client_uses_bearer_auth_and_exposes_testable_requests() -> None:
    spec = valid_spec(target_ids=["test-vehicle-01"])
    requests = []
    response = {"deployment_id": "demo-deployer-001"}

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse(200, json.dumps(response).encode("utf-8"))

    client = FleetApiClient(
        "https://api.example.test/deploy/api/",
        "secret-value",
        timeout=4.5,
        opener=opener,
    )
    result = client._request_json("GET", "deployments/demo-deployer-001")

    assert result == response
    request, timeout = requests[0]
    assert request.full_url == "https://api.example.test/deploy/api/deployments/demo-deployer-001"
    assert request.headers["Authorization"] == "Bearer secret-value"
    assert timeout == 4.5
    assert spec.deployment_id == "demo-deployer-001"


def test_http_client_rejects_http_errors_without_leaking_response_body() -> None:
    def opener(request, *, timeout):
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    client = FleetApiClient("https://api.example.test/deploy/api", "secret", opener=opener)

    with pytest.raises(FleetApiError, match="HTTP 503") as error:
        client.read_deployment("demo-deployer-001")

    assert error.value.status_code == 503
    assert "secret" not in str(error.value)


def test_cli_emits_verified_result_without_putting_key_in_output(
    tmp_path, monkeypatch, capsys
) -> None:
    spec = valid_spec(target_ids=["test-vehicle-01"])
    artifact = artifact_for(spec)
    spec_path = tmp_path / "deployment.json"
    artifact_path = tmp_path / "profile.json"
    spec_path.write_text(spec.canonical_json(), encoding="utf-8")
    artifact_path.write_bytes(artifact.content)
    queued = snapshot(
        spec,
        artifact,
        status="queued",
        verified=False,
        observed={"color": "blue", "shape": "circle"},
        target_status="pending",
    )
    completed = snapshot(spec, artifact, status="succeeded", verified=True)
    client = FakeClient(queued, completed, completed)

    monkeypatch.setenv(deployer.API_KEY_ENV, "cli-secret")
    monkeypatch.setattr(deployer, "FleetApiClient", lambda *args, **kwargs: client)

    exit_code = deployer.main(
        [
            "--api-base-url",
            "https://api.example.test/deploy/api",
            "--spec",
            str(spec_path),
            "--artifact",
            str(artifact_path),
            "--artifact-sha256",
            artifact.sha256,
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert json.loads(output)["verified"] is True
    assert "cli-secret" not in output
