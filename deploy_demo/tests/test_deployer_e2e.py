"""End-to-end local tests for the Fleet Deploy Lab Python deployer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.request import Request

import fleet_api
import main
import pytest
from fastapi.testclient import TestClient

from deploy_demo import (
    DeploymentArtifact,
    DeploymentSpec,
    DeploymentVerificationError,
    FleetApiClient,
    FleetApiError,
    FleetDeployer,
    FleetProfile,
    FleetStateStore,
    UnknownDeploymentError,
    validate_deployment_spec,
)

FLEET_HEADERS = {"Authorization": "Bearer fleet-test-secret"}


def valid_spec(**overrides: object) -> DeploymentSpec:
    """Return a valid shared contract for an integration test."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": "demo-e2e-001",
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


def artifact_for(spec: DeploymentSpec) -> DeploymentArtifact:
    """Build the exact canonical artifact expected by the deployer."""
    profile = FleetProfile(spec.profile_color, spec.profile_shape)
    return DeploymentArtifact.from_bytes(profile.canonical_json().encode("utf-8"))


class InProcessResponse:
    """Adapt a FastAPI TestClient response to urllib's response protocol."""

    def __init__(self, response) -> None:
        self._response = response

    def __enter__(self) -> InProcessResponse:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def getcode(self) -> int:
        return self._response.status_code

    def read(self, _limit: int = -1) -> bytes:
        return self._response.content


@dataclass
class InProcessOpener:
    """Send the real HTTP client requests through an in-process FastAPI app."""

    client: TestClient
    requests: list[tuple[str, str]] = field(default_factory=list)
    responses: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, request: Request, *, timeout: float) -> InProcessResponse:
        del timeout
        parsed = urlsplit(request.full_url)
        response = self.client.request(
            request.method,
            parsed.path,
            content=request.data,
            headers=dict(request.header_items()),
        )
        self.requests.append((request.method, parsed.path))
        self.responses.append(response.json())
        return InProcessResponse(response)


@dataclass
class FleetHarness:
    """An isolated simulator plus the real API client used by the deployer."""

    store: FleetStateStore
    http: TestClient
    opener: InProcessOpener
    api: FleetApiClient


@pytest.fixture
def harness(tmp_path, monkeypatch) -> FleetHarness:
    """Provide a fresh SQLite state store and isolated FastAPI dependency."""
    store = FleetStateStore(tmp_path / "fleet.sqlite3")
    monkeypatch.setenv(fleet_api.FLEET_MANAGEMENT_KEY_ENV, "fleet-test-secret")
    main.app.dependency_overrides[fleet_api.get_fleet_state_store] = lambda: store
    try:
        with TestClient(main.app) as http:
            opener = InProcessOpener(http)
            api = FleetApiClient(
                "http://testserver/deploy/api",
                "fleet-test-secret",
                opener=opener,
            )
            yield FleetHarness(store=store, http=http, opener=opener, api=api)
    finally:
        main.app.dependency_overrides.pop(fleet_api.get_fleet_state_store, None)


@dataclass
class TamperedReadbackApi:
    """Delegate to the real API but corrupt only the fresh verification body."""

    delegate: FleetApiClient
    read_calls: int = 0

    def queue_deployment(self, spec: DeploymentSpec) -> Mapping[str, object]:
        return self.delegate.queue_deployment(spec)

    def apply_deployment(self, deployment_id: str) -> Mapping[str, object]:
        return self.delegate.apply_deployment(deployment_id)

    def read_deployment(self, deployment_id: str) -> Mapping[str, object]:
        self.read_calls += 1
        payload = dict(self.delegate.read_deployment(deployment_id))
        targets = [dict(target) for target in payload["targets"]]
        targets[0]["observed"] = {"color": "blue", "shape": "circle"}
        targets[0]["observed_digest"] = FleetProfile(color="blue", shape="circle").digest
        payload["targets"] = targets
        return payload


def test_real_deployer_applies_one_target_and_verifies_fresh_readback(harness) -> None:
    """The real client must queue, apply, and then read the simulator."""
    spec = valid_spec(target_ids=["test-vehicle-02"])
    artifact = artifact_for(spec)
    events = []

    result = FleetDeployer(harness.api, event_sink=events.append).deploy(spec, artifact)

    assert harness.opener.requests == [
        ("POST", "/deploy/api/deployments"),
        ("POST", "/deploy/api/deployments/demo-e2e-001/apply"),
        ("GET", "/deploy/api/deployments/demo-e2e-001"),
    ]
    assert result.response["verified"] is True
    assert result.target_ids == ("test-vehicle-02",)
    assert [event.event_type for event in events] == [
        "deployment_started",
        "deployment_queued",
        "deployment_applying",
        "deployment_verified",
    ]

    vehicle = harness.store.get_vehicle("test-vehicle-02")
    expected = FleetProfile(color="purple", shape="hexagon")
    assert vehicle.status == "succeeded"
    assert vehicle.desired_profile == expected
    assert vehicle.observed_profile == expected
    assert vehicle.observed_digest == artifact.sha256


def test_real_deployer_expands_environment_without_touching_other_targets(harness) -> None:
    """An environment selector must affect exactly its four simulator targets."""
    before = {vehicle.target_id: vehicle for vehicle in harness.store.list_vehicles()}
    spec = valid_spec(
        deployment_id="demo-e2e-stage",
        environment="stage",
        profile={"color": "orange", "shape": "triangle"},
    )

    result = FleetDeployer(harness.api).deploy(spec, artifact_for(spec))

    expected_targets = (
        "stage-vehicle-01",
        "stage-vehicle-02",
        "stage-vehicle-03",
        "stage-vehicle-04",
    )
    assert result.target_ids == expected_targets
    expected = FleetProfile(color="orange", shape="triangle")
    for target_id, previous in before.items():
        vehicle = harness.store.get_vehicle(target_id)
        if target_id in expected_targets:
            assert vehicle.status == "succeeded"
            assert vehicle.desired_profile == expected
            assert vehicle.observed_profile == expected
        else:
            assert vehicle == previous


def test_repeated_real_deployment_is_idempotent_and_preserves_state(harness) -> None:
    """Repeating the same deployment ID must not rewrite durable timestamps."""
    spec = valid_spec(target_ids=["prod-vehicle-03"])
    artifact = artifact_for(spec)

    first = FleetDeployer(harness.api).deploy(spec, artifact)
    first_deployment = harness.store.get_deployment(spec.deployment_id)
    first_vehicle = harness.store.get_vehicle("prod-vehicle-03")
    second = FleetDeployer(harness.api).deploy(spec, artifact)

    assert first.response["verified"] is True
    assert second.response["verified"] is True
    assert harness.opener.responses[3]["idempotent"] is True
    assert harness.opener.responses[4]["idempotent"] is True
    assert harness.store.get_deployment(spec.deployment_id) == first_deployment
    assert harness.store.get_vehicle("prod-vehicle-03") == first_vehicle


def test_protected_write_rejects_wrong_credential_without_mutating_simulator(harness) -> None:
    """The deployer cannot bypass the Fleet API's dedicated write boundary."""
    spec = valid_spec(target_ids=["test-vehicle-03"])
    artifact = artifact_for(spec)
    before = harness.store.list_vehicles()
    unauthorized = FleetApiClient(
        "http://testserver/deploy/api",
        "wrong-secret",
        opener=harness.opener,
    )

    with pytest.raises(FleetApiError, match="HTTP 401"):
        FleetDeployer(unauthorized).deploy(spec, artifact)

    with pytest.raises(UnknownDeploymentError):
        harness.store.get_deployment(spec.deployment_id)
    assert harness.store.list_vehicles() == before
    assert harness.opener.requests == [("POST", "/deploy/api/deployments")]


def test_queue_web_request_does_not_run_deployer_synchronously(harness) -> None:
    """The protected queue route leaves observed state pending for later apply."""
    spec = valid_spec(target_ids=["test-vehicle-04"])
    before = harness.store.get_vehicle("test-vehicle-04")

    response = harness.http.post(
        "/deploy/api/deployments",
        headers=FLEET_HEADERS,
        json=spec.to_dict(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["verification"] == "pending"
    assert body["verified"] is False
    assert body["targets"][0]["observed"] == before.observed_profile.to_dict()
    assert harness.store.get_deployment(spec.deployment_id).status == "queued"


def test_tampered_fresh_readback_fails_closed_after_real_apply(harness) -> None:
    """A successful apply is not enough when the independent readback disagrees."""
    spec = valid_spec(target_ids=["test-vehicle-01"])
    artifact = artifact_for(spec)
    events = []
    tampered_api = TamperedReadbackApi(harness.api)

    with pytest.raises(DeploymentVerificationError, match=r"readback\.targets\[0\]"):
        FleetDeployer(tampered_api, event_sink=events.append).deploy(spec, artifact)

    assert tampered_api.read_calls == 1
    assert harness.opener.requests == [
        ("POST", "/deploy/api/deployments"),
        ("POST", "/deploy/api/deployments/demo-e2e-001/apply"),
        ("GET", "/deploy/api/deployments/demo-e2e-001"),
    ]
    assert events[-1].event_type == "deployment_failed"
    assert "readback.targets[0]" in (events[-1].detail or "")
    assert harness.store.get_deployment(spec.deployment_id).status == "succeeded"
