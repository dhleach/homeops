"""Focused durability and idempotency tests for simulated fleet state."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from deploy_demo import (
    DeploymentAdmissionLimitError,
    DeploymentConflictError,
    DeploymentCooldownError,
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
        "strategy": "all_at_once",
        "failure_mode": "abort",
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


def test_deterministic_verification_failure_preserves_partial_target_state(
    tmp_path, fixed_clock
) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-deterministic-failure",
        profile={"color": "orange", "shape": "triangle"},
        failure_target_id="test-vehicle-02",
    )

    store.queue_deployment(spec)
    store.mark_deployment_status(spec.deployment_id, "applying")
    applying = store.mark_deployment_status(spec.deployment_id, "succeeded")

    assert applying.status == "applying"
    assert (
        applying.error == "deterministic verification failure injected for target test-vehicle-02"
    )
    assert store.get_vehicle("test-vehicle-01").status == "succeeded"
    failed = store.get_vehicle("test-vehicle-02")
    assert failed.status == "failed"
    assert failed.last_error == applying.error
    assert failed.desired_profile != failed.observed_profile
    assert store.get_vehicle("test-vehicle-03").status == "pending"

    completed = store.mark_deployment_status(
        spec.deployment_id,
        "failed",
        error="GitHub Actions failed: Deploy immutable artifact to simulator (failure)",
    )
    assert completed.status == "failed"
    assert store.get_vehicle("test-vehicle-01").status == "succeeded"
    assert store.get_vehicle("test-vehicle-02").status == "failed"
    assert store.get_vehicle("test-vehicle-03").status == "pending"


def test_terminal_canary_failure_releases_reservations_across_restart_and_retry(
    tmp_path, fixed_clock
) -> None:
    path = tmp_path / "fleet.sqlite3"
    store = FleetStateStore(path, clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-terminal-canary-failure",
        strategy="canary",
        profile={"color": "orange", "shape": "triangle"},
        failure_target_id="test-vehicle-02",
    )

    store.queue_deployment(spec)
    store.apply_deployment_targets(spec.deployment_id, ["test-vehicle-01"])
    store.apply_deployment_targets(
        spec.deployment_id,
        ["test-vehicle-02", "test-vehicle-03", "test-vehicle-04"],
    )
    failed = store.mark_deployment_status(
        spec.deployment_id,
        "failed",
        error="GitHub Actions failed: deploy simulator",
    )

    assert failed.status == "failed"
    assert [
        store.get_vehicle(target_id).active_deployment_id for target_id in spec.expanded_target_ids
    ] == [None, None, None, None]
    assert [store.get_vehicle(target_id).status for target_id in spec.expanded_target_ids] == [
        "succeeded",
        "failed",
        "pending",
        "pending",
    ]

    # Repeated terminal reconciliation must be harmless and preserve the
    # target-level failure/pending evidence.
    repeated = store.mark_deployment_status(
        spec.deployment_id,
        "failed",
        error="GitHub Actions failed: deploy simulator",
    )
    assert repeated.status == "failed"
    assert all(
        store.get_vehicle(target_id).active_deployment_id is None
        for target_id in spec.expanded_target_ids
    )

    reopened = FleetStateStore(path, clock=fixed_clock)
    retry = reopened.queue_deployment(
        valid_spec(
            deployment_id="demo-terminal-canary-retry",
            target_ids=list(spec.expanded_target_ids),
            profile={"color": "purple", "shape": "hexagon"},
        )
    )
    assert retry.deployment.status == "queued"
    assert all(
        reopened.get_vehicle(target_id).active_deployment_id == retry.deployment.deployment_id
        for target_id in spec.expanded_target_ids
    )


def test_concurrent_terminal_failure_reconciliation_is_idempotent(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-concurrent-terminal-failure",
        target_ids=["test-vehicle-01"],
    )
    store.queue_deployment(spec)
    store.mark_deployment_status(spec.deployment_id, "applying")

    def reconcile() -> str:
        return store.mark_deployment_status(
            spec.deployment_id,
            "failed",
            error="workflow failed",
        ).status

    with ThreadPoolExecutor(max_workers=4) as executor:
        statuses = list(executor.map(lambda _: reconcile(), range(4)))

    assert statuses == ["failed"] * 4
    vehicle = store.get_vehicle("test-vehicle-01")
    assert vehicle.status == "failed"
    assert vehicle.active_deployment_id is None


def test_canary_failure_leaves_later_targets_pending(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-canary-deterministic-failure",
        strategy="canary",
        profile={"color": "orange", "shape": "triangle"},
        failure_target_id="test-vehicle-01",
    )

    store.queue_deployment(spec)
    applying = store.apply_deployment_targets(spec.deployment_id, ["test-vehicle-01"])

    assert applying.status == "applying"
    assert store.get_vehicle("test-vehicle-01").status == "failed"
    assert store.get_vehicle("test-vehicle-01").last_error == applying.error
    assert [store.get_vehicle(target_id).status for target_id in spec.expanded_target_ids[1:]] == [
        "pending",
        "pending",
        "pending",
    ]


def test_successful_canary_completion_releases_reservations(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-canary-success-reservations",
        strategy="canary",
        profile={"color": "green", "shape": "hexagon"},
    )

    store.queue_deployment(spec)
    store.apply_deployment_targets(spec.deployment_id, ["test-vehicle-01"])
    completed = store.apply_deployment_targets(
        spec.deployment_id,
        ["test-vehicle-02", "test-vehicle-03", "test-vehicle-04"],
    )

    assert completed.status == "succeeded"
    assert all(
        store.get_vehicle(target_id).active_deployment_id is None
        for target_id in spec.expanded_target_ids
    )


def test_rollback_restores_changed_targets_and_keeps_partial_failure_state(
    tmp_path, fixed_clock
) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    baseline = store.get_vehicle("test-vehicle-01").observed_profile
    spec = valid_spec(
        deployment_id="demo-canary-rollback",
        strategy="canary",
        failure_mode="rollback",
        profile={"color": "orange", "shape": "triangle"},
        failure_target_id="test-vehicle-02",
    )

    store.queue_deployment(spec)
    store.apply_deployment_targets(spec.deployment_id, ["test-vehicle-01"])
    store.apply_deployment_targets(
        spec.deployment_id,
        ["test-vehicle-02", "test-vehicle-03", "test-vehicle-04"],
    )

    rolled_back = store.restore_deployment_targets(
        spec.deployment_id,
        {"test-vehicle-01": baseline},
    )

    assert rolled_back.status == "failed"
    assert rolled_back.rollback_status == "succeeded"
    assert rolled_back.rollback_target_ids == ("test-vehicle-01",)
    restored = store.get_vehicle("test-vehicle-01")
    assert restored.status == "ready"
    assert restored.active_deployment_id is None
    assert restored.desired_profile == baseline
    assert restored.observed_profile == baseline
    assert store.get_vehicle("test-vehicle-02").status == "failed"
    assert store.get_vehicle("test-vehicle-03").status == "pending"


def test_rollback_failure_records_partial_state_without_false_success(
    tmp_path, fixed_clock
) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-canary-rollback-failure",
        strategy="canary",
        failure_mode="rollback",
        profile={"color": "orange", "shape": "triangle"},
        failure_target_id="test-vehicle-02",
    )

    store.queue_deployment(spec)
    store.apply_deployment_targets(spec.deployment_id, ["test-vehicle-01"])
    store.apply_deployment_targets(
        spec.deployment_id,
        ["test-vehicle-02", "test-vehicle-03", "test-vehicle-04"],
    )
    failed = store.record_rollback_failure(
        spec.deployment_id,
        error="restore endpoint unavailable",
    )

    assert failed.status == "failed"
    assert failed.rollback_status == "failed"
    assert failed.rollback_error == "restore endpoint unavailable"
    assert store.get_vehicle("test-vehicle-01").status == "succeeded"
    assert store.get_vehicle("test-vehicle-02").status == "failed"
    assert store.get_vehicle("test-vehicle-03").status == "pending"


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


def test_deployment_events_are_durable_and_idempotent(tmp_path, fixed_clock) -> None:
    path = tmp_path / "fleet.sqlite3"
    store = FleetStateStore(path, clock=fixed_clock)
    spec = valid_spec(
        deployment_id="demo-event-history",
        target_ids=["test-vehicle-01", "test-vehicle-02"],
        profile={"color": "green", "shape": "square"},
    )
    queued = store.queue_deployment(spec).deployment

    first = store.append_deployment_event(
        spec.deployment_id,
        event_type="deployment_canary_started",
        schema_version=1,
        artifact_sha256=queued.profile_digest,
        target_ids=["test-vehicle-01"],
        status="applying",
        detail="verifying canary target test-vehicle-01",
    )
    repeated = store.append_deployment_event(
        spec.deployment_id,
        event_type="deployment_canary_started",
        schema_version=1,
        artifact_sha256=queued.profile_digest,
        target_ids=["test-vehicle-01"],
        status="applying",
        detail="verifying canary target test-vehicle-01",
    )

    assert len(first.events) == 1
    assert repeated.events == first.events
    assert first.events[0].sequence == 1
    assert first.events[0].target_ids == ("test-vehicle-01",)

    reopened = FleetStateStore(path, clock=fixed_clock)
    persisted = reopened.get_deployment(spec.deployment_id)
    assert persisted.events == first.events
    assert persisted.to_dict()["events"][0]["event_type"] == "deployment_canary_started"


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
        assert vehicle.active_deployment_id is None

    repeated = store.mark_deployment_status("demo-success", "succeeded")
    assert repeated.status == "succeeded"
    assert all(
        store.get_vehicle(target_id).active_deployment_id is None
        for target_id in ("stage-vehicle-01", "stage-vehicle-02")
    )


def test_unknown_target_is_rejected_before_state_changes(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    before = store.list_vehicles()

    with pytest.raises(UnknownTargetError):
        store.get_vehicle("test-vehicle-99")

    assert store.list_vehicles() == before


def test_public_admission_serializes_cooldown_and_active_limit(tmp_path) -> None:
    now = [datetime(2026, 9, 25, 20, 0, tzinfo=UTC)]
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=lambda: now[0])
    first_spec = valid_spec(
        deployment_id="public-first",
        target_ids=["test-vehicle-01"],
    )
    second_spec = valid_spec(
        deployment_id="public-second",
        target_ids=["test-vehicle-02"],
    )

    first = store.admit_public_deployment(
        first_spec,
        client_ip="203.0.113.10",
        manifest_path="manifests/public-first.json",
        manifest_sha256="a" * 64,
        cooldown_seconds=60,
        max_active=1,
    )
    assert first.idempotent is False
    assert first.deployment.dispatch_status == "pending"

    with pytest.raises(DeploymentCooldownError):
        store.admit_public_deployment(
            second_spec,
            client_ip="203.0.113.10",
            manifest_path="manifests/public-second.json",
            manifest_sha256="b" * 64,
            cooldown_seconds=60,
            max_active=1,
        )

    replay = store.admit_public_deployment(
        first_spec,
        client_ip="203.0.113.10",
        manifest_path="manifests/public-first.json",
        manifest_sha256="a" * 64,
        cooldown_seconds=60,
        max_active=1,
    )
    assert replay.idempotent is True

    now[0] = now[0].replace(minute=1, second=1)
    with pytest.raises(DeploymentAdmissionLimitError):
        store.admit_public_deployment(
            second_spec,
            client_ip="203.0.113.10",
            manifest_path="manifests/public-second.json",
            manifest_sha256="b" * 64,
            cooldown_seconds=60,
            max_active=1,
        )

    store.mark_deployment_status("public-first", "failed", error="dispatch_failed")
    accepted = store.admit_public_deployment(
        second_spec,
        client_ip="203.0.113.10",
        manifest_path="manifests/public-second.json",
        manifest_sha256="b" * 64,
        cooldown_seconds=60,
        max_active=1,
    )
    assert accepted.idempotent is False


def test_dispatch_claim_recovery_preserves_manifest_commit(tmp_path, fixed_clock) -> None:
    store = FleetStateStore(tmp_path / "fleet.sqlite3", clock=fixed_clock)
    spec = valid_spec(deployment_id="public-recovery", target_ids=["test-vehicle-03"])
    store.admit_public_deployment(
        spec,
        client_ip="203.0.113.11",
        manifest_path="manifests/public-recovery.json",
        manifest_sha256="c" * 64,
        cooldown_seconds=0,
        max_active=1,
    )

    claimed = store.claim_dispatch("public-recovery", "claim-one", lease_seconds=300)
    assert claimed is not None
    assert store.claim_dispatch("public-recovery", "claim-two", lease_seconds=300) is None

    committed = store.record_manifest_commit("public-recovery", "claim-one", "d" * 40)
    assert committed.manifest_commit_sha == "d" * 40
    failed = store.record_dispatch_failure(
        "public-recovery",
        "claim-one",
        error="workflow_dispatch_failed",
    )
    assert failed.dispatch_status == "failed"
    assert failed.manifest_commit_sha == "d" * 40

    retry = store.claim_dispatch("public-recovery", "claim-two", lease_seconds=300)
    assert retry is not None
    assert retry.manifest_commit_sha == "d" * 40
    dispatched = store.record_dispatch_success(
        "public-recovery",
        "claim-two",
        workflow_run_id=123,
        workflow_url="https://github.com/dhleach/homeops/actions/runs/123",
    )
    assert dispatched.dispatch_status == "dispatched"
    assert dispatched.workflow_run_id == 123
    assert dispatched.workflow_url.endswith("/123")
