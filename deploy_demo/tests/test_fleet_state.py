"""Focused durability and idempotency tests for simulated fleet state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deploy_demo import (
    DeploymentConflictError,
    FleetStateStore,
    UnknownDeploymentError,
    UnknownTargetError,
    validate_deployment_spec,
)


def valid_spec(**overrides: object):
    """Build a valid PR 02 contract for a state-store test."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": "demo-20260925-001",
        "environment": "test",
        "profile": {"color": "blue", "shape": "circle"},
        "implementation": "python",
        "strategy": "rolling",
        "failure_mode": "rollback",
    }
    payload.update(overrides)
    if "target_ids" in overrides:
        payload.pop("environment", None)
    return validate_deployment_spec(payload)


@pytest.fixture
def fixed_clock():
    return lambda: datetime(2026, 9, 25, 20, 0, tzinfo=UTC)


def test_fresh_store_seeds_twelve_readable_vehicles(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)

    vehicles = store.list_vehicles()

    assert len(vehicles) == 12
    assert [vehicle.label for vehicle in vehicles] == [
        "TEST-01",
        "TEST-02",
        "TEST-03",
        "TEST-04",
        "STAGE-01",
        "STAGE-02",
        "STAGE-03",
        "STAGE-04",
        "PROD-01",
        "PROD-02",
        "PROD-03",
        "PROD-04",
    ]
    assert all(vehicle.status == "ready" for vehicle in vehicles)
    assert all(vehicle.desired_profile == vehicle.observed_profile for vehicle in vehicles)
    assert all(vehicle.desired_digest == vehicle.observed_digest for vehicle in vehicles)


def test_queueing_one_vehicle_leaves_the_other_eleven_unchanged(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    before = {vehicle.target_id: vehicle for vehicle in store.list_vehicles()}

    result = store.queue_deployment(
        valid_spec(
            deployment_id="demo-single-target",
            target_ids=["test-vehicle-02"],
            profile={"color": "purple", "shape": "hexagon"},
        )
    )

    assert result.deployment.target_ids == ("test-vehicle-02",)
    changed = store.get_vehicle("test-vehicle-02")
    assert changed.status == "pending"
    assert changed.desired_profile.to_dict() == {"color": "purple", "shape": "hexagon"}
    assert changed.observed_profile == before["test-vehicle-02"].observed_profile
    for vehicle in store.list_vehicles():
        if vehicle.target_id != "test-vehicle-02":
            assert vehicle == before[vehicle.target_id]


def test_pending_and_failed_state_preserve_desired_observed_divergence(
    tmp_path, fixed_clock
) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    baseline = store.get_vehicle("prod-vehicle-04")
    spec = valid_spec(
        deployment_id="demo-failing-deployment",
        target_ids=["prod-vehicle-04"],
        profile={"color": "orange", "shape": "triangle"},
    )

    store.queue_deployment(spec)
    pending = store.get_vehicle("prod-vehicle-04")
    assert pending.status == "pending"
    assert pending.desired_profile != pending.observed_profile

    store.mark_deployment_status("demo-failing-deployment", "applying")
    failed = store.mark_deployment_status(
        "demo-failing-deployment", "failed", error="simulated vehicle timeout"
    )

    vehicle = store.get_vehicle("prod-vehicle-04")
    assert failed.status == "failed"
    assert vehicle.status == "failed"
    assert vehicle.desired_profile == pending.desired_profile
    assert vehicle.observed_profile == baseline.observed_profile
    assert vehicle.desired_profile != vehicle.observed_profile
    assert vehicle.last_error == "simulated vehicle timeout"


def test_reopening_store_preserves_queued_state_without_auto_success(tmp_path, fixed_clock) -> None:
    path = tmp_path / "fleet.sqlite3"
    first = FleetStateStore(path, clock=fixed_clock)
    first.queue_deployment(
        valid_spec(
            deployment_id="demo-survives-restart",
            target_ids=["stage-vehicle-03"],
            profile={"color": "green", "shape": "square"},
        )
    )

    reopened = FleetStateStore(path, clock=fixed_clock)

    assert reopened.get_deployment("demo-survives-restart").status == "queued"
    vehicle = reopened.get_vehicle("stage-vehicle-03")
    assert vehicle.status == "pending"
    assert vehicle.desired_profile != vehicle.observed_profile


def test_same_deployment_and_profile_digest_are_idempotent(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-idempotent",
        target_ids=["test-vehicle-01", "test-vehicle-03"],
        profile={"color": "green", "shape": "square"},
    )

    first = store.queue_deployment(spec)
    second = store.queue_deployment(spec)

    assert first.idempotent is False
    assert second.idempotent is True
    assert second.deployment == first.deployment
    assert len(store.list_vehicles()) == 12


def test_same_deployment_id_with_new_profile_fails_closed(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    store.queue_deployment(valid_spec(deployment_id="demo-collision"))

    with pytest.raises(DeploymentConflictError, match="different profile"):
        store.queue_deployment(
            valid_spec(
                deployment_id="demo-collision",
                profile={"color": "purple", "shape": "hexagon"},
            )
        )


def test_failed_deployment_can_be_replaced_by_a_new_id(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    store.queue_deployment(
        valid_spec(
            deployment_id="demo-first-attempt",
            target_ids=["test-vehicle-01"],
        )
    )
    store.mark_deployment_status("demo-first-attempt", "failed", error="timeout")

    retry = store.queue_deployment(
        valid_spec(
            deployment_id="demo-retry",
            target_ids=["test-vehicle-01"],
            profile={"color": "orange", "shape": "triangle"},
        )
    )

    assert retry.deployment.status == "queued"
    assert store.get_vehicle("test-vehicle-01").active_deployment_id == "demo-retry"


def test_conflicting_multi_target_queue_is_atomic(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    store.queue_deployment(
        valid_spec(
            deployment_id="demo-existing",
            target_ids=["test-vehicle-01"],
        )
    )
    untouched_before = store.get_vehicle("test-vehicle-02")

    with pytest.raises(DeploymentConflictError, match="already have active"):
        store.queue_deployment(
            valid_spec(
                deployment_id="demo-conflicting",
                target_ids=["test-vehicle-01", "test-vehicle-02"],
            )
        )

    assert store.get_vehicle("test-vehicle-02") == untouched_before
    with pytest.raises(UnknownDeploymentError):
        store.get_deployment("demo-conflicting")


def test_successful_completion_updates_observed_state_atomically(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    store.queue_deployment(
        valid_spec(
            deployment_id="demo-success",
            target_ids=["stage-vehicle-01", "stage-vehicle-02"],
            profile={"color": "purple", "shape": "hexagon"},
        )
    )

    store.mark_deployment_status("demo-success", "applying")
    store.mark_deployment_status("demo-success", "succeeded")

    for target_id in ("stage-vehicle-01", "stage-vehicle-02"):
        vehicle = store.get_vehicle(target_id)
        assert vehicle.status == "succeeded"
        assert vehicle.desired_profile == vehicle.observed_profile
        assert vehicle.desired_digest == vehicle.observed_digest


def test_unknown_target_is_rejected_before_state_changes(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    before = store.list_vehicles()

    with pytest.raises(UnknownTargetError):
        store.get_vehicle("test-vehicle-99")

    assert store.list_vehicles() == before
