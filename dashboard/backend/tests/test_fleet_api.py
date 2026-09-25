"""Contract tests for the Fleet Deploy Lab API boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import fleet_api
import main
import pytest
from fastapi.testclient import TestClient

from deploy_demo import FleetStateStore

client = TestClient(main.app)
FLEET_HEADERS = {"Authorization": "Bearer fleet-test-secret"}


def valid_payload(**overrides: object) -> dict[str, object]:
    """Return a valid DeploymentSpec request for API tests."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": "demo-api-001",
        "target_ids": ["test-vehicle-01"],
        "profile": {"color": "purple", "shape": "hexagon"},
        "implementation": "python",
        "strategy": "rolling",
        "failure_mode": "rollback",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def fleet_store(tmp_path, monkeypatch):
    """Use an isolated SQLite store and a dedicated test-only credential."""
    store = FleetStateStore(
        tmp_path / "fleet.sqlite3",
        clock=lambda: datetime(2026, 9, 25, 22, 0, tzinfo=UTC),
    )
    monkeypatch.setenv(fleet_api.FLEET_MANAGEMENT_KEY_ENV, "fleet-test-secret")
    main.app.dependency_overrides[fleet_api.get_fleet_state_store] = lambda: store
    try:
        yield store
    finally:
        main.app.dependency_overrides.pop(fleet_api.get_fleet_state_store, None)


def test_public_fleet_reads_identify_every_target_as_simulated(fleet_store) -> None:
    health = client.get("/deploy/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "simulated": True, "target_count": 12}

    response = client.get("/deploy/api/fleet")

    assert response.status_code == 200
    body = response.json()
    assert body["simulated"] is True
    assert body["target_kind"] == "simulated"
    assert len(body["targets"]) == 12
    assert body["targets"][0]["label"] == "TEST-01"
    assert all(target["simulated"] is True for target in body["targets"])
    assert all(target["desired_digest"] == target["observed_digest"] for target in body["targets"])


def test_public_read_rejects_unknown_target(fleet_store) -> None:
    response = client.get("/deploy/api/fleet/test-vehicle-99")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_target"


def test_anonymous_write_is_rejected_without_mutating_state(fleet_store) -> None:
    before_all = fleet_store.list_vehicles()
    before = fleet_store.get_vehicle("test-vehicle-01")

    response = client.post("/deploy/api/deployments", json=valid_payload())

    assert response.status_code == 401
    assert response.json()["detail"] == fleet_api.FLEET_MANAGEMENT_REQUIRED_ERROR
    assert fleet_store.list_vehicles() == before_all
    assert fleet_store.get_vehicle("test-vehicle-01") == before


def test_homeops_diagnostic_token_is_not_a_fleet_management_credential(fleet_store) -> None:
    response = client.post(
        "/deploy/api/deployments",
        headers={"Authorization": "Bearer test-token"},
        json=valid_payload(),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == fleet_api.FLEET_MANAGEMENT_REQUIRED_ERROR


def test_management_fails_closed_when_dedicated_credential_is_missing(
    fleet_store, monkeypatch
) -> None:
    monkeypatch.delenv(fleet_api.FLEET_MANAGEMENT_KEY_ENV)

    response = client.post(
        "/deploy/api/deployments",
        headers=FLEET_HEADERS,
        json=valid_payload(),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == fleet_api.FLEET_MANAGEMENT_UNAVAILABLE_ERROR


def test_authorized_queue_apply_and_replay_exposes_verifiable_state(fleet_store) -> None:
    queue = client.post(
        "/deploy/api/deployments",
        headers=FLEET_HEADERS,
        json=valid_payload(),
    )

    assert queue.status_code == 200
    queued = queue.json()
    assert queued["deployment_id"] == "demo-api-001"
    assert queued["status"] == "queued"
    assert queued["verification"] == "pending"
    assert queued["verified"] is False
    assert queued["idempotent"] is False
    target = queued["targets"][0]
    assert target["desired"] == {"color": "purple", "shape": "hexagon"}
    assert target["observed"] == {"color": "blue", "shape": "circle"}
    assert queued["desired_digest"] == target["desired_digest"]

    public_target = client.get("/deploy/api/fleet/test-vehicle-01")
    assert public_target.status_code == 200
    assert public_target.json()["observed"] == {"color": "blue", "shape": "circle"}

    applied = client.post(
        "/deploy/api/deployments/demo-api-001/apply",
        headers=FLEET_HEADERS,
    )

    assert applied.status_code == 200
    applied_body = applied.json()
    assert applied_body["status"] == "succeeded"
    assert applied_body["verification"] == "verified"
    assert applied_body["verified"] is True
    assert applied_body["idempotent"] is False
    assert applied_body["targets"][0]["observed"] == {
        "color": "purple",
        "shape": "hexagon",
    }
    assert (
        applied_body["targets"][0]["desired_digest"]
        == applied_body["targets"][0]["observed_digest"]
    )

    replay = client.post(
        "/deploy/api/deployments/demo-api-001/apply",
        headers=FLEET_HEADERS,
    )

    assert replay.status_code == 200
    assert replay.json()["status"] == "succeeded"
    assert replay.json()["verified"] is True
    assert replay.json()["idempotent"] is True
    assert replay.json()["updated_at"] == applied_body["updated_at"]

    readback = client.get("/deploy/api/deployments/demo-api-001")
    assert readback.status_code == 200
    assert readback.json()["verified"] is True


def test_invalid_target_id_fails_before_state_mutation(fleet_store) -> None:
    before = fleet_store.list_vehicles()

    response = client.post(
        "/deploy/api/deployments",
        headers=FLEET_HEADERS,
        json=valid_payload(
            deployment_id="demo-invalid-target",
            target_ids=["test-vehicle-99"],
        ),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_deployment_spec"
    assert fleet_store.list_vehicles() == before


def test_unknown_deployment_apply_fails_closed(fleet_store) -> None:
    response = client.post(
        "/deploy/api/deployments/missing/apply",
        headers=FLEET_HEADERS,
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_deployment"
