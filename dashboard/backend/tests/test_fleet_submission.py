"""Public PR10 manifest admission and workflow-dispatch contracts."""

from __future__ import annotations

from collections.abc import Mapping

import fleet_api
import main
import pytest
from fastapi.testclient import TestClient

from deploy_demo import (
    FleetStateStore,
    GitHubFleetError,
    ManifestCommitReceipt,
    WorkflowDispatchReceipt,
    WorkflowJobReceipt,
    WorkflowRunReceipt,
    WorkflowStepReceipt,
)

SUBMISSION = {
    "schema_version": 1,
    "deployment_id": "public-submit-001",
    "target_ids": ["test-vehicle-01"],
    "profile": {"color": "purple", "shape": "hexagon"},
    "implementation": "python",
    "strategy": "all_at_once",
    "failure_mode": "abort",
}

CANARY_FAILURE_SUBMISSION = {
    **SUBMISSION,
    "deployment_id": "public-canary-failure-001",
    "target_ids": [
        "test-vehicle-01",
        "test-vehicle-02",
        "test-vehicle-03",
        "test-vehicle-04",
    ],
    "profile": {"color": "green", "shape": "hexagon"},
    "strategy": "canary",
    "failure_target_id": "test-vehicle-02",
}


class FakeGitHub:
    """Observable Contents/Actions adapter with a dispatch failure switch."""

    def __init__(self) -> None:
        self.commit_calls: list[tuple[str, bytes]] = []
        self.dispatch_calls: list[tuple[str, str]] = []
        self.fail_commit = False
        self.fail_dispatch = False
        self.workflow_run: WorkflowRunReceipt | None = None

    def commit_manifest(self, deployment_id: str, content: bytes) -> ManifestCommitReceipt:
        self.commit_calls.append((deployment_id, content))
        if self.fail_commit:
            raise GitHubFleetError("provider response must not reach the client")
        return ManifestCommitReceipt("e" * 40)

    def dispatch_workflow(
        self,
        deployment_id: str,
        event_commit_sha: str,
    ) -> WorkflowDispatchReceipt:
        self.dispatch_calls.append((deployment_id, event_commit_sha))
        if self.fail_dispatch:
            raise GitHubFleetError("provider response must not reach the client")
        return WorkflowDispatchReceipt(
            workflow_run_id=456,
            workflow_url="https://github.com/dhleach/homeops/actions/runs/456",
        )

    def read_workflow_run(self, workflow_run_id: int) -> WorkflowRunReceipt:
        if self.workflow_run is not None:
            assert self.workflow_run.workflow_run_id == workflow_run_id
            return self.workflow_run
        return WorkflowRunReceipt(
            workflow_run_id=workflow_run_id,
            workflow_url=f"https://github.com/dhleach/homeops/actions/runs/{workflow_run_id}",
            status="in_progress",
            conclusion=None,
            created_at="2026-09-26T12:00:00Z",
            updated_at="2026-09-26T12:00:30Z",
            jobs=(),
        )

    def find_workflow_run(self, _deployment_id: str) -> WorkflowRunReceipt | None:
        return self.workflow_run


@pytest.fixture
def submission_harness(tmp_path, monkeypatch):
    """Install isolated durable state and a fake server-side GitHub client."""
    store = FleetStateStore(tmp_path / "fleet.sqlite3")
    github = FakeGitHub()
    monkeypatch.setenv(fleet_api.FLEET_SUBMISSION_COOLDOWN_ENV, "0")
    monkeypatch.setenv(fleet_api.FLEET_MAX_ACTIVE_ENV, "2")
    main.app.dependency_overrides[fleet_api.get_fleet_state_store] = lambda: store
    main.app.dependency_overrides[fleet_api.get_fleet_github_client] = lambda: github
    main.app.dependency_overrides[fleet_api.get_optional_fleet_github_client] = lambda: github
    try:
        with TestClient(main.app) as client:
            yield client, store, github
    finally:
        main.app.dependency_overrides.pop(fleet_api.get_fleet_state_store, None)
        main.app.dependency_overrides.pop(fleet_api.get_fleet_github_client, None)
        main.app.dependency_overrides.pop(fleet_api.get_optional_fleet_github_client, None)


def test_public_submission_commits_once_and_dispatches_matching_identity(
    submission_harness,
) -> None:
    client, store, github = submission_harness

    response = client.post("/deploy/api/deployments/submit", json=SUBMISSION)

    assert response.status_code == 202
    body = response.json()
    assert body["deployment_id"] == SUBMISSION["deployment_id"]
    assert body["status"] == "queued"
    assert body["dispatch_status"] == "dispatched"
    assert body["manifest_path"] == "manifests/public-submit-001.json"
    assert body["manifest_commit_sha"] == "e" * 40
    assert body["workflow_run_id"] == 456
    assert body["workflow_url"].endswith("/456")
    assert body["request_summary"] == {
        "deployment_id": "public-submit-001",
        "failure_mode": "abort",
        "implementation": "python",
        "profile": {"color": "purple", "shape": "hexagon"},
        "schema_version": 1,
        "strategy": "all_at_once",
        "target_ids": ["test-vehicle-01"],
    }
    assert len(github.commit_calls) == 1
    assert len(github.dispatch_calls) == 1
    deployment = store.get_deployment("public-submit-001")
    assert deployment.manifest_commit_sha == "e" * 40
    assert deployment.request_summary == body["request_summary"]
    assert github.dispatch_calls[0] == ("public-submit-001", "e" * 40)
    assert "FLEET_DEPLOY_API_KEY" not in response.text


def test_replaying_same_public_submission_is_idempotent(submission_harness) -> None:
    client, _store, github = submission_harness

    first = client.post("/deploy/api/deployments/submit", json=SUBMISSION)
    second = client.post("/deploy/api/deployments/submit", json=SUBMISSION)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["idempotent"] is True
    assert len(github.commit_calls) == 1
    assert len(github.dispatch_calls) == 1


def test_dispatch_failure_is_recoverable_without_a_second_manifest_commit(
    submission_harness,
) -> None:
    client, store, github = submission_harness
    github.fail_dispatch = True

    failed = client.post("/deploy/api/deployments/submit", json=SUBMISSION)

    assert failed.status_code == 202
    assert failed.json()["dispatch_status"] == "failed"
    assert failed.json()["dispatch_error"] == "workflow_dispatch_failed"
    assert failed.json()["manifest_commit_sha"] == "e" * 40
    assert store.get_deployment("public-submit-001").manifest_commit_sha == "e" * 40

    github.fail_dispatch = False
    recovered = client.post("/deploy/api/deployments/submit", json=SUBMISSION)

    assert recovered.status_code == 202
    assert recovered.json()["dispatch_status"] == "dispatched"
    assert len(github.commit_calls) == 1
    assert len(github.dispatch_calls) == 2


def test_manifest_commit_failure_is_classified_and_retryable(submission_harness) -> None:
    client, _store, github = submission_harness
    github.fail_commit = True

    failed = client.post("/deploy/api/deployments/submit", json=SUBMISSION)

    assert failed.status_code == 202
    body = failed.json()
    assert body["dispatch_error"] == "manifest_commit_failed"
    assert body["error_code"] == "manifest_commit_failed"
    assert "manifest" in body["error"].lower()
    assert "same attempt" in body["error_recovery"]


def test_cooldown_error_exposes_bounded_recovery_metadata(submission_harness, monkeypatch) -> None:
    client, _store, _github = submission_harness
    monkeypatch.setenv(fleet_api.FLEET_SUBMISSION_COOLDOWN_ENV, "60")

    first = client.post("/deploy/api/deployments/submit", json=SUBMISSION)
    second = client.post(
        "/deploy/api/deployments/submit",
        json={**SUBMISSION, "deployment_id": "public-submit-002"},
    )

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["Retry-After"]
    assert second.json()["detail"]["code"] == "submission_cooldown"
    assert second.json()["detail"]["retry_after_seconds"] >= 1


def test_capacity_error_exposes_busy_fleet_recovery_metadata(
    submission_harness, monkeypatch
) -> None:
    client, _store, _github = submission_harness
    monkeypatch.setenv(fleet_api.FLEET_MAX_ACTIVE_ENV, "1")

    first = client.post("/deploy/api/deployments/submit", json=SUBMISSION)
    second = client.post(
        "/deploy/api/deployments/submit",
        json={**SUBMISSION, "deployment_id": "public-submit-003"},
    )

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "capacity_full"
    assert second.json()["detail"]["retry_after_seconds"] == 30


def test_submission_rejects_extra_browser_fields_before_external_calls(submission_harness) -> None:
    client, _store, github = submission_harness
    payload: Mapping[str, object] = {**SUBMISSION, "repository": "attacker-controlled"}

    response = client.post("/deploy/api/deployments/submit", json=payload)

    assert response.status_code == 422
    assert github.commit_calls == []
    assert github.dispatch_calls == []


def test_submission_rejects_unproven_capability_before_external_calls(submission_harness) -> None:
    client, _store, github = submission_harness
    payload = {**SUBMISSION, "strategy": "rolling", "failure_mode": "rollback"}

    response = client.post("/deploy/api/deployments/submit", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_deployment_spec"
    assert github.commit_calls == []
    assert github.dispatch_calls == []


def _workflow_run(*, status: str, conclusion: str | None, jobs: tuple[WorkflowJobReceipt, ...]):
    """Build a deterministic Actions receipt for reconciliation tests."""
    return WorkflowRunReceipt(
        workflow_run_id=456,
        workflow_url="https://github.com/dhleach/homeops/actions/runs/456",
        status=status,
        conclusion=conclusion,
        created_at="2026-09-26T12:00:00Z",
        updated_at="2026-09-26T12:00:30Z",
        jobs=jobs,
    )


def test_public_read_reconciles_running_workflow_and_exposes_job_state(submission_harness) -> None:
    client, _store, github = submission_harness
    client.post("/deploy/api/deployments/submit", json=SUBMISSION)
    github.workflow_run = _workflow_run(
        status="in_progress",
        conclusion=None,
        jobs=(
            WorkflowJobReceipt(
                job_id=10,
                name="Validate manifest and build profile artifact",
                status="completed",
                conclusion="success",
                started_at="2026-09-26T12:00:01Z",
                completed_at="2026-09-26T12:00:10Z",
                workflow_url="https://github.com/dhleach/homeops/actions/runs/456/job/10",
                steps=(
                    WorkflowStepReceipt(
                        number=1,
                        name="Validate exact manifest before build gates",
                        status="completed",
                        conclusion="success",
                        started_at="2026-09-26T12:00:02Z",
                        completed_at="2026-09-26T12:00:09Z",
                    ),
                ),
            ),
        ),
    )

    response = client.get("/deploy/api/deployments/public-submit-001")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applying"
    assert body["workflow_status"] == "in_progress"
    assert body["workflow"]["jobs"][0]["name"] == "Validate manifest and build profile artifact"
    assert body["workflow"]["jobs"][0]["steps"][0]["name"] == (
        "Validate exact manifest before build gates"
    )
    assert body["manifest_commit_url"].endswith("/commit/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
    assert body["targets"][0]["observed"] == {"color": "blue", "shape": "circle"}


def test_protected_deployer_event_is_durable_and_visible_on_public_read(
    submission_harness, monkeypatch
) -> None:
    client, store, _github = submission_harness
    monkeypatch.setenv(fleet_api.FLEET_MANAGEMENT_KEY_ENV, "management-secret")
    client.post("/deploy/api/deployments/submit", json=SUBMISSION)
    digest = store.get_deployment("public-submit-001").profile_digest
    event = {
        "event_type": "deployment_started",
        "schema_version": 1,
        "artifact_sha256": digest,
        "target_ids": ["test-vehicle-01"],
        "status": "started",
        "detail": "trusted deployer started",
    }

    first = client.post(
        "/deploy/api/deployments/public-submit-001/events",
        json=event,
        headers={"Authorization": "Bearer management-secret"},
    )
    repeated = client.post(
        "/deploy/api/deployments/public-submit-001/events",
        json=event,
        headers={"Authorization": "Bearer management-secret"},
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["events"] == repeated.json()["events"]
    assert first.json()["events"][0]["event_type"] == "deployment_started"
    assert (
        client.get("/deploy/api/deployments/public-submit-001").json()["events"]
        == first.json()["events"]
    )


def test_failed_workflow_marks_deployment_failed_without_changing_observed_state(
    submission_harness,
) -> None:
    client, store, github = submission_harness
    client.post("/deploy/api/deployments/submit", json=SUBMISSION)
    baseline = store.get_vehicle("test-vehicle-01").observed_profile.to_dict()
    github.workflow_run = _workflow_run(
        status="completed",
        conclusion="failure",
        jobs=(
            WorkflowJobReceipt(
                job_id=11,
                name="Deploy immutable artifact to simulator",
                status="completed",
                conclusion="failure",
                started_at="2026-09-26T12:00:11Z",
                completed_at="2026-09-26T12:00:20Z",
                workflow_url="https://github.com/dhleach/homeops/actions/runs/456/job/11",
                failed_step="Deploy artifact and verify fresh API readback",
            ),
        ),
    )

    response = client.get("/deploy/api/deployments/public-submit-001")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["workflow_conclusion"] == "failure"
    assert body["error_code"] == "workflow_failed"
    assert "workflow run" in body["error_recovery"]
    assert "Deploy immutable artifact to simulator" in body["error"]
    assert "Deploy artifact and verify fresh API readback" in body["error"]
    assert body["targets"][0]["observed"] == baseline
    assert body["targets"][0]["desired"] != body["targets"][0]["observed"]


def test_new_submission_reconciles_terminal_canary_before_capacity_check(
    submission_harness, monkeypatch
) -> None:
    client, store, github = submission_harness
    monkeypatch.setenv(fleet_api.FLEET_MAX_ACTIVE_ENV, "1")

    first = client.post("/deploy/api/deployments/submit", json=CANARY_FAILURE_SUBMISSION)
    assert first.status_code == 202
    store.apply_deployment_targets(CANARY_FAILURE_SUBMISSION["deployment_id"], ["test-vehicle-01"])
    store.apply_deployment_targets(
        CANARY_FAILURE_SUBMISSION["deployment_id"],
        ["test-vehicle-02", "test-vehicle-03", "test-vehicle-04"],
    )
    github.workflow_run = _workflow_run(
        status="completed",
        conclusion="failure",
        jobs=(
            WorkflowJobReceipt(
                job_id=12,
                name="Deploy immutable artifact to simulator",
                status="completed",
                conclusion="failure",
                started_at="2026-09-26T12:00:11Z",
                completed_at="2026-09-26T12:00:20Z",
                workflow_url="https://github.com/dhleach/homeops/actions/runs/456/job/12",
                failed_step="Deploy artifact and verify fresh API readback",
            ),
        ),
    )

    second = client.post(
        "/deploy/api/deployments/submit",
        json={
            **SUBMISSION,
            "deployment_id": "public-after-canary-failure",
            "target_ids": ["stage-vehicle-01"],
            "profile": {"color": "orange", "shape": "triangle"},
        },
    )

    assert second.status_code == 202
    assert second.json()["deployment_id"] == "public-after-canary-failure"
    assert store.get_deployment(CANARY_FAILURE_SUBMISSION["deployment_id"]).status == "failed"
    assert all(
        store.get_vehicle(target_id).active_deployment_id is None
        for target_id in CANARY_FAILURE_SUBMISSION["target_ids"]
    )
    assert store.get_vehicle("stage-vehicle-01").active_deployment_id == (
        "public-after-canary-failure"
    )


def test_successful_workflow_requires_verified_observed_state(submission_harness) -> None:
    client, store, github = submission_harness
    client.post("/deploy/api/deployments/submit", json=SUBMISSION)
    store.mark_deployment_status("public-submit-001", "succeeded")
    github.workflow_run = _workflow_run(status="completed", conclusion="success", jobs=())

    response = client.get("/deploy/api/deployments/public-submit-001")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["verified"] is True
    assert body["workflow_conclusion"] == "success"
