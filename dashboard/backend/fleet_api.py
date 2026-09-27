"""Public Fleet Deploy Lab reads and protected simulator management routes.

The Fleet Deploy Lab is deliberately a separate control-plane boundary from
the live HomeOps HVAC APIs.  Reads are safe to expose to the demo frontend and
identify every target as simulated.  Management writes use a dedicated static
bearer credential supplied only to the backend; they do not reuse OIDC,
Home Assistant, Ask HomeOps, or normal Pi/EC2 deployment credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import logging
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from security import extract_client_ip, load_proxy_config


def _ensure_deploy_demo_importable(module_file: Path) -> None:
    """Make the source-tree package importable without assuming a layout.

    CI imports this module as a top-level file while the production image
    copies it to ``/app/fleet_api.py`` beside the ``deploy_demo`` package.
    Search ancestors only when the package is not already importable; this
    keeps the flattened image path from indexing beyond the filesystem root.
    """
    if importlib.util.find_spec("deploy_demo") is not None:
        return

    for candidate in module_file.resolve().parents:
        repository_root = candidate / "deploy_demo"
        if (repository_root / "__init__.py").is_file():
            root_string = str(candidate)
            if root_string not in sys.path:
                sys.path.insert(0, root_string)
            return


_ensure_deploy_demo_importable(Path(__file__))

from deploy_demo import (  # noqa: E402
    DEFAULT_REPOSITORY,
    GITHUB_REPOSITORY_ENV,
    MAX_DEPLOYMENT_HISTORY,
    SIMULATED_VERIFICATION_FAILURE_PREFIX,
    DeploymentAdmissionLimitError,
    DeploymentConflictError,
    DeploymentCooldownError,
    DeploymentDailyLimitError,
    DeploymentEventRecord,
    DeploymentSpecError,
    DeploymentState,
    FleetProfile,
    FleetStateError,
    FleetStateStore,
    GitHubFleetClient,
    GitHubFleetConfigurationError,
    GitHubFleetError,
    InvalidStateTransitionError,
    UnknownDeploymentError,
    UnknownTargetError,
    VehicleState,
    WorkflowJobReceipt,
    WorkflowRunReceipt,
    WorkflowStepReceipt,
    baseline_profile,
    supported_capabilities,
    validate_deployment_spec,
)

logger = logging.getLogger(__name__)

FLEET_API_PREFIX = "/deploy/api"
FLEET_MANAGEMENT_KEY_ENV = "FLEET_DEPLOY_API_KEY"
FLEET_MANAGEMENT_REQUIRED_ERROR = "Fleet management credential required"
FLEET_MANAGEMENT_UNAVAILABLE_ERROR = "Fleet management temporarily unavailable"
FLEET_STATE_UNAVAILABLE_ERROR = "Fleet simulator temporarily unavailable"
FLEET_GITHUB_UNAVAILABLE_ERROR = "Fleet deployment temporarily unavailable"
FLEET_SUBMISSION_COOLDOWN_ENV = "FLEET_DEPLOY_IP_COOLDOWN_SECONDS"
FLEET_MAX_ACTIVE_ENV = "FLEET_DEPLOY_MAX_ACTIVE"
FLEET_DISPATCH_LEASE_ENV = "FLEET_DEPLOY_DISPATCH_LEASE_SECONDS"
FLEET_RESET_DAILY_CAP_ENV = "FLEET_DEPLOY_RESET_DAILY_CAP"
DEFAULT_FLEET_SUBMISSION_COOLDOWN_SECONDS = 60
DEFAULT_FLEET_MAX_ACTIVE = 1
DEFAULT_FLEET_DISPATCH_LEASE_SECONDS = 300
DEFAULT_FLEET_RESET_DAILY_CAP = 1

VehicleStatusResponse = Literal["ready", "pending", "applying", "succeeded", "failed"]
DeploymentStatusResponse = Literal["queued", "applying", "succeeded", "failed"]
VerificationStatus = Literal["pending", "verified", "failed"]
DispatchStatusResponse = Literal[
    "not_started",
    "pending",
    "committing",
    "dispatching",
    "dispatched",
    "failed",
]
RollbackStatusResponse = Literal["not_started", "in_progress", "succeeded", "failed"]

router = APIRouter(prefix=FLEET_API_PREFIX, tags=["Fleet Deploy Lab"])
_fleet_bearer_scheme = HTTPBearer(auto_error=False)
_fleet_state_store: FleetStateStore | None = None


class FleetProfileResponse(BaseModel):
    """A finite color/shape profile rendered by a simulated target."""

    model_config = ConfigDict(extra="forbid")

    color: str
    shape: str


class FleetTargetResponse(BaseModel):
    """A public-safe target snapshot with desired/observed state separated."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    label: str
    environment: str
    simulated: Literal[True] = True
    desired: FleetProfileResponse
    desired_digest: str
    observed: FleetProfileResponse
    observed_digest: str
    status: VehicleStatusResponse
    active_deployment_id: str | None
    last_error: str | None
    updated_at: str


class FleetCapabilityResponse(BaseModel):
    """One implementation/strategy/failure-mode combination proven by CI."""

    model_config = ConfigDict(extra="forbid")

    implementation: str
    strategy: str
    failure_mode: str


class FleetReadResponse(BaseModel):
    """The anonymous fleet snapshot consumed by the future demo frontend."""

    model_config = ConfigDict(extra="forbid")

    simulated: Literal[True] = True
    target_kind: Literal["simulated"] = "simulated"
    targets: list[FleetTargetResponse]
    capabilities: list[FleetCapabilityResponse]


class FleetHealthResponse(BaseModel):
    """Readiness for the Fleet Deploy Lab state boundary."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    simulated: Literal[True] = True
    target_count: int = Field(..., ge=0)


class DeploymentSpecRequest(BaseModel):
    """JSON input accepted by the shared dependency-free DeploymentSpec validator."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    deployment_id: str
    environment: str | None = None
    target_ids: list[str] | None = None
    profile: FleetProfileResponse
    implementation: str
    strategy: str
    failure_mode: str
    failure_target_id: str | None = None
    operation: Literal["deploy", "reset"] = "deploy"


class DeploymentApplyRequest(BaseModel):
    """Optional protected target subset for a verified canary phase."""

    model_config = ConfigDict(extra="forbid")

    target_ids: list[str] | None = Field(default=None, min_length=1, max_length=12)


class DeploymentRestoreTargetRequest(BaseModel):
    """One trusted pre-deployment profile used for protected rollback."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    profile: FleetProfileResponse


class DeploymentRestoreRequest(BaseModel):
    """Bounded target snapshots accepted only by the management credential."""

    model_config = ConfigDict(extra="forbid")

    targets: list[DeploymentRestoreTargetRequest] = Field(..., min_length=1, max_length=12)


class FleetWorkflowStepResponse(BaseModel):
    """Public-safe state for one bounded GitHub Actions step."""

    model_config = ConfigDict(extra="forbid")

    number: int
    name: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None


class FleetWorkflowJobResponse(BaseModel):
    """Public-safe state for one GitHub Actions job."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    url: str | None
    failed_step: str | None
    steps: list[FleetWorkflowStepResponse] = Field(default_factory=list)


class FleetWorkflowResponse(BaseModel):
    """Public-safe run and job state for the trusted deployment workflow."""

    model_config = ConfigDict(extra="forbid")

    id: int
    url: str | None
    status: str
    conclusion: str | None
    created_at: str
    updated_at: str
    jobs: list[FleetWorkflowJobResponse]


class FleetDeploymentEventResponse(BaseModel):
    """Public-safe persisted deployment evidence."""

    model_config = ConfigDict(extra="forbid")

    sequence: int
    event_type: str
    schema_version: int
    artifact_sha256: str
    target_ids: list[str]
    status: str
    detail: str | None
    recorded_at: str


class FleetDeploymentResponse(BaseModel):
    """Deployment plus fresh target state used for apply and verification."""

    model_config = ConfigDict(extra="forbid")

    simulated: Literal[True] = True
    target_kind: Literal["simulated"] = "simulated"
    deployment_id: str
    operation: Literal["deploy", "reset"] = "deploy"
    target_ids: list[str]
    desired: FleetProfileResponse
    desired_digest: str
    status: DeploymentStatusResponse
    error: str | None
    created_at: str
    updated_at: str
    verification: VerificationStatus
    verified: bool
    targets: list[FleetTargetResponse]
    events: list[FleetDeploymentEventResponse] = Field(default_factory=list)
    request_summary: dict[str, object] | None = None
    idempotent: bool = False
    manifest_path: str | None = None
    manifest_sha256: str | None = None
    manifest_commit_sha: str | None = None
    manifest_commit_url: str | None = None
    workflow_run_id: int | None = None
    workflow_url: str | None = None
    workflow_status: str | None = None
    workflow_conclusion: str | None = None
    workflow_created_at: str | None = None
    workflow_updated_at: str | None = None
    workflow_error: str | None = None
    workflow: FleetWorkflowResponse | None = None
    dispatch_status: DispatchStatusResponse = "not_started"
    dispatch_error: str | None = None
    rollback_status: RollbackStatusResponse = "not_started"
    rollback_target_ids: list[str] = Field(default_factory=list)
    rollback_error: str | None = None
    error_code: str | None = None
    error_recovery: str | None = None
    retry_after_seconds: int | None = None


class FleetDeploymentHistoryEntryResponse(BaseModel):
    """Public-safe summary for one durable recent deployment record."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    operation: Literal["deploy", "reset"]
    selector: dict[str, object]
    implementation: str
    artifact_sha256: str
    outcome: str
    status: DeploymentStatusResponse
    verification: VerificationStatus
    created_at: str
    updated_at: str
    manifest_commit_url: str | None = None
    workflow_run_id: int | None = None
    workflow_url: str | None = None


class FleetDeploymentHistoryResponse(BaseModel):
    """Bounded durable history returned to the anonymous demo frontend."""

    model_config = ConfigDict(extra="forbid")

    simulated: Literal[True] = True
    target_kind: Literal["simulated"] = "simulated"
    deployments: list[FleetDeploymentHistoryEntryResponse]


class FleetDeploymentEventRequest(BaseModel):
    """One bounded lifecycle event emitted by a trusted deployer."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal[
        "deployment_started",
        "deployment_queued",
        "deployment_applying",
        "deployment_canary_started",
        "deployment_canary_verified",
        "deployment_rollout_started",
        "deployment_rollout_verified",
        "deployment_rollback_started",
        "deployment_rollback_verified",
        "deployment_rollback_failed",
        "deployment_verified",
        "deployment_failed",
    ]
    schema_version: int = Field(..., ge=1, le=10)
    artifact_sha256: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    target_ids: list[str] = Field(..., min_length=1, max_length=12)
    status: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    detail: str | None = Field(default=None, max_length=2_000)


def _profile_response(color: str, shape: str) -> FleetProfileResponse:
    return FleetProfileResponse(color=color, shape=shape)


def _target_response(vehicle: VehicleState) -> FleetTargetResponse:
    """Translate a domain vehicle into the explicit simulated API shape."""
    return FleetTargetResponse(
        target_id=vehicle.target_id,
        label=vehicle.label,
        environment=vehicle.environment,
        desired=_profile_response(
            vehicle.desired_profile.color,
            vehicle.desired_profile.shape,
        ),
        desired_digest=vehicle.desired_digest,
        observed=_profile_response(
            vehicle.observed_profile.color,
            vehicle.observed_profile.shape,
        ),
        observed_digest=vehicle.observed_digest,
        status=vehicle.status,
        active_deployment_id=vehicle.active_deployment_id,
        last_error=vehicle.last_error,
        updated_at=vehicle.updated_at,
    )


def _manifest_commit_url(commit_sha: str | None) -> str | None:
    """Build a public commit URL only from validated repository metadata."""
    if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        return None
    repository = os.environ.get(GITHUB_REPOSITORY_ENV, DEFAULT_REPOSITORY).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return None
    return f"https://github.com/{repository}/commit/{commit_sha}"


def _workflow_step_response(step: WorkflowStepReceipt) -> FleetWorkflowStepResponse:
    """Translate a bounded Actions step receipt into the public contract."""
    return FleetWorkflowStepResponse(
        number=step.number,
        name=step.name,
        status=step.status,
        conclusion=step.conclusion,
        started_at=step.started_at,
        completed_at=step.completed_at,
    )


def _workflow_job_response(job: WorkflowJobReceipt) -> FleetWorkflowJobResponse:
    """Translate a bounded provider job receipt into the public contract."""
    return FleetWorkflowJobResponse(
        id=job.job_id,
        name=job.name,
        status=job.status,
        conclusion=job.conclusion,
        started_at=job.started_at,
        completed_at=job.completed_at,
        url=job.workflow_url,
        failed_step=job.failed_step,
        steps=[_workflow_step_response(step) for step in job.steps],
    )


def _workflow_response(run: WorkflowRunReceipt | None) -> FleetWorkflowResponse | None:
    """Translate an optional Actions receipt for the browser."""
    if run is None:
        return None
    return FleetWorkflowResponse(
        id=run.workflow_run_id,
        url=run.workflow_url,
        status=run.status,
        conclusion=run.conclusion,
        created_at=run.created_at,
        updated_at=run.updated_at,
        jobs=[_workflow_job_response(job) for job in run.jobs],
    )


def _deployment_event_response(event: DeploymentEventRecord) -> FleetDeploymentEventResponse:
    """Translate durable deployer evidence into the public timeline contract."""
    return FleetDeploymentEventResponse(
        sequence=event.sequence,
        event_type=event.event_type,
        schema_version=event.schema_version,
        artifact_sha256=event.artifact_sha256,
        target_ids=list(event.target_ids),
        status=event.status,
        detail=event.detail,
        recorded_at=event.recorded_at,
    )


def _deployment_operation(deployment: DeploymentState) -> Literal["deploy", "reset"]:
    """Return the validated operation recorded with the deployment request."""
    operation = (deployment.request_summary or {}).get("operation", "deploy")
    return operation if operation in {"deploy", "reset"} else "deploy"


def _deployment_is_verified(store: FleetStateStore, deployment: DeploymentState) -> bool:
    """Check the durable desired/observed convergence without provider calls."""
    if deployment.status != "succeeded":
        return False
    return all(
        (vehicle := store.get_vehicle(target_id)).desired_digest == vehicle.observed_digest
        for target_id in deployment.target_ids
    )


def _deployment_history_entry(
    store: FleetStateStore,
    deployment: DeploymentState,
) -> FleetDeploymentHistoryEntryResponse:
    """Translate one durable deployment into the bounded recent-run contract."""
    summary = deployment.request_summary or {}
    operation = _deployment_operation(deployment)
    environment = summary.get("environment")
    target_ids = summary.get("target_ids") or list(deployment.target_ids)
    selector: dict[str, object]
    if isinstance(environment, str):
        selector = {"environment": environment}
    else:
        selector = {"target_ids": list(target_ids) if isinstance(target_ids, list) else []}
    verified = _deployment_is_verified(store, deployment)
    if verified:
        outcome = "verified"
    elif deployment.dispatch_status == "failed":
        outcome = deployment.dispatch_error or "dispatch_failed"
    elif deployment.status == "failed":
        outcome = "failed"
    else:
        outcome = deployment.status
    return FleetDeploymentHistoryEntryResponse(
        deployment_id=deployment.deployment_id,
        operation=operation,
        selector=selector,
        implementation=str(summary.get("implementation", "unknown")),
        artifact_sha256=deployment.profile_digest,
        outcome=outcome,
        status=deployment.status,
        verification=(
            "verified" if verified else ("failed" if deployment.status == "failed" else "pending")
        ),
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
        manifest_commit_url=_manifest_commit_url(deployment.manifest_commit_sha),
        workflow_run_id=deployment.workflow_run_id,
        workflow_url=deployment.workflow_url,
    )


def _workflow_failure_message(run: WorkflowRunReceipt) -> str:
    """Return an actionable, bounded failure summary for a completed run."""
    failed_jobs = [
        job for job in run.jobs if job.conclusion not in {None, "success", "neutral", "skipped"}
    ]
    if failed_jobs:
        details = []
        for job in failed_jobs[:3]:
            detail = f"{job.name} ({job.conclusion or 'failed'})"
            if job.failed_step:
                detail += f" at {job.failed_step}"
            details.append(detail)
        return "GitHub Actions failed: " + "; ".join(details)
    return f"GitHub Actions completed with conclusion: {run.conclusion or 'failed'}"


_DISPATCH_ERROR_METADATA = {
    "manifest_commit_failed": (
        "manifest_commit_failed",
        "The deployment manifest could not be recorded.",
        "Retry this same attempt; any already-recorded manifest will be reused.",
    ),
    "workflow_dispatch_failed": (
        "workflow_dispatch_failed",
        "The deployment manifest was recorded, but the trusted workflow was not dispatched.",
        "Retry this same attempt; the committed manifest will be reused without "
        "creating a duplicate.",
    ),
}


def _deployment_error_metadata(
    deployment: DeploymentState,
    *,
    workflow_run: WorkflowRunReceipt | None,
    workflow_error: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return a public-safe error code, message, recovery, and retry hint."""
    if workflow_error:
        return (
            "workflow_unavailable",
            workflow_error,
            "Keep this attempt open and refresh; do not start a duplicate while "
            "workflow state is unavailable.",
            None,
        )

    if deployment.dispatch_error:
        metadata = _DISPATCH_ERROR_METADATA.get(
            deployment.dispatch_error,
            (
                deployment.dispatch_error,
                "The deployment could not be dispatched.",
                "Retry this same attempt; the server will preserve the request identity.",
            ),
        )
        return (*metadata, None)

    workflow_failed = (
        workflow_run is not None
        and workflow_run.status == "completed"
        and workflow_run.conclusion not in {None, "success", "neutral", "skipped"}
    ) or (deployment.error or "").startswith("GitHub Actions failed:")
    if workflow_failed:
        return (
            "workflow_failed",
            deployment.error or "GitHub Actions reported a failed deployment.",
            "Review the workflow run and observed target state before deciding "
            "whether to start a new attempt.",
            None,
        )

    if deployment.error and deployment.error.startswith("GitHub Actions reported success"):
        return (
            "verification_failed",
            deployment.error,
            "Compare desired and observed target state before retrying this request.",
            None,
        )

    if deployment.error and deployment.error.startswith(SIMULATED_VERIFICATION_FAILURE_PREFIX):
        recovery = "Compare desired and observed target state before retrying this request."
        if deployment.rollback_status == "succeeded":
            recovery = (
                "Deployment remains failed; rollback verified the changed simulated targets "
                "against their pre-deployment profiles."
            )
        elif deployment.rollback_status == "failed":
            recovery = (
                "Rollback was not verified. Inspect the desired/observed target state; "
                "the fleet may be partially changed."
            )
        elif (deployment.request_summary or {}).get("failure_mode") == "rollback":
            recovery = (
                "Rollback was requested but is not verified. Inspect the desired/observed "
                "target state before retrying."
            )
        return (
            "verification_failed",
            deployment.error,
            recovery,
            None,
        )

    return (None, deployment.error, None, None)


def _deployment_observed_matches(store: FleetStateStore, deployment: DeploymentState) -> bool:
    """Verify every requested target has the requested observed profile."""
    if _deployment_operation(deployment) == "reset":
        return all(
            store.get_vehicle(target_id).observed_digest == baseline_profile(target_id).digest
            for target_id in deployment.target_ids
        )
    return all(
        store.get_vehicle(target_id).observed_digest == deployment.profile_digest
        for target_id in deployment.target_ids
    )


def _deployment_response(
    store: FleetStateStore,
    deployment: DeploymentState,
    *,
    idempotent: bool = False,
    workflow_run: WorkflowRunReceipt | None = None,
    workflow_error: str | None = None,
) -> FleetDeploymentResponse:
    """Return a deployment with fresh desired/observed target snapshots."""
    targets = [
        _target_response(store.get_vehicle(target_id)) for target_id in deployment.target_ids
    ]
    all_observed = all(target.desired_digest == target.observed_digest for target in targets)
    if deployment.status == "succeeded" and all_observed:
        verification: VerificationStatus = "verified"
    elif deployment.status == "failed":
        verification = "failed"
    elif deployment.status == "succeeded":
        verification = "failed"
    else:
        verification = "pending"

    error_code, error_message, error_recovery, retry_after_seconds = _deployment_error_metadata(
        deployment,
        workflow_run=workflow_run,
        workflow_error=workflow_error,
    )
    return FleetDeploymentResponse(
        deployment_id=deployment.deployment_id,
        operation=_deployment_operation(deployment),
        target_ids=list(deployment.target_ids),
        desired=_profile_response(deployment.profile.color, deployment.profile.shape),
        desired_digest=deployment.profile_digest,
        status=deployment.status,
        error=error_message,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
        verification=verification,
        verified=verification == "verified",
        targets=targets,
        events=[_deployment_event_response(event) for event in deployment.events],
        request_summary=deployment.request_summary,
        idempotent=idempotent,
        manifest_path=deployment.manifest_path,
        manifest_sha256=deployment.manifest_sha256,
        manifest_commit_sha=deployment.manifest_commit_sha,
        manifest_commit_url=_manifest_commit_url(deployment.manifest_commit_sha),
        workflow_run_id=(
            workflow_run.workflow_run_id if workflow_run is not None else deployment.workflow_run_id
        ),
        workflow_url=(
            workflow_run.workflow_url if workflow_run is not None else deployment.workflow_url
        ),
        workflow_status=workflow_run.status if workflow_run is not None else None,
        workflow_conclusion=workflow_run.conclusion if workflow_run is not None else None,
        workflow_created_at=workflow_run.created_at if workflow_run is not None else None,
        workflow_updated_at=workflow_run.updated_at if workflow_run is not None else None,
        workflow_error=workflow_error,
        workflow=_workflow_response(workflow_run),
        dispatch_status=deployment.dispatch_status,
        dispatch_error=deployment.dispatch_error,
        rollback_status=deployment.rollback_status,
        rollback_target_ids=list(deployment.rollback_target_ids),
        rollback_error=deployment.rollback_error,
        error_code=error_code,
        error_recovery=error_recovery,
        retry_after_seconds=retry_after_seconds,
    )


def _state_unavailable(exc: Exception) -> None:
    """Log only the exception type and fail closed without leaking paths."""
    logger.error("Fleet simulator state unavailable: %s", type(exc).__name__)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=FLEET_STATE_UNAVAILABLE_ERROR,
    ) from None


def get_fleet_state_store() -> FleetStateStore:
    """Lazily open the configured durable state store."""
    global _fleet_state_store
    if _fleet_state_store is None:
        try:
            _fleet_state_store = FleetStateStore.from_environment()
        except (OSError, sqlite3.Error, FleetStateError) as exc:
            _state_unavailable(exc)
    assert _fleet_state_store is not None
    return _fleet_state_store


FleetStoreDependency = Annotated[FleetStateStore, Depends(get_fleet_state_store)]


def get_fleet_github_client() -> GitHubFleetClient:
    """Load the server-only GitHub Contents/Actions credential boundary."""
    try:
        return GitHubFleetClient.from_environment()
    except GitHubFleetConfigurationError:
        logger.error("Fleet GitHub integration is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=FLEET_GITHUB_UNAVAILABLE_ERROR,
        ) from None


FleetGitHubDependency = Annotated[
    GitHubFleetClient,
    Depends(get_fleet_github_client),
]


def get_optional_fleet_github_client() -> GitHubFleetClient | None:
    """Load GitHub polling when configured without breaking anonymous reads."""
    try:
        return GitHubFleetClient.from_environment()
    except GitHubFleetConfigurationError:
        return None


OptionalFleetGitHubDependency = Annotated[
    GitHubFleetClient | None,
    Depends(get_optional_fleet_github_client),
]


def _refresh_workflow_state(
    store: FleetStateStore,
    deployment: DeploymentState,
    github: GitHubFleetClient | None,
) -> tuple[DeploymentState, WorkflowRunReceipt | None, str | None]:
    """Read Actions plus observed state and reconcile only proven transitions."""
    if github is None or deployment.dispatch_status != "dispatched":
        return deployment, None, None

    try:
        if deployment.workflow_run_id is None:
            workflow_run = github.find_workflow_run(deployment.deployment_id)
            if workflow_run is None:
                return deployment, None, None
        else:
            workflow_run = github.read_workflow_run(deployment.workflow_run_id)

        if deployment.workflow_run_id != workflow_run.workflow_run_id or (
            workflow_run.workflow_url and deployment.workflow_url != workflow_run.workflow_url
        ):
            deployment = store.record_workflow_run(
                deployment.deployment_id,
                workflow_run.workflow_run_id,
                workflow_run.workflow_url,
            )

        current = store.get_deployment(deployment.deployment_id)
        if workflow_run.status == "in_progress" and current.status == "queued":
            current = store.mark_deployment_status(deployment.deployment_id, "applying")
        elif workflow_run.status == "completed" and workflow_run.conclusion == "success":
            if current.status not in {"succeeded", "failed"}:
                if _deployment_observed_matches(store, current):
                    current = store.mark_deployment_status(deployment.deployment_id, "succeeded")
                else:
                    current = store.mark_deployment_status(
                        deployment.deployment_id,
                        "failed",
                        error="GitHub Actions reported success without verified fleet readback",
                    )
        elif workflow_run.status == "completed" and current.status not in {
            "succeeded",
            "failed",
        }:
            current = store.mark_deployment_status(
                deployment.deployment_id,
                "failed",
                error=_workflow_failure_message(workflow_run),
            )
        return current, workflow_run, None
    except GitHubFleetError as exc:
        logger.warning("Fleet workflow refresh failed: %s", type(exc).__name__)
        return deployment, None, "Workflow state temporarily unavailable"


def _reconcile_active_deployments(
    store: FleetStateStore,
    github: GitHubFleetClient,
) -> None:
    """Reconcile tracked workflows before a new request checks capacity.

    The browser may stop polling an older attempt when the user edits a new
    request or reloads.  Admission therefore performs a bounded server-side
    reconciliation of every queued/applying dispatched deployment before
    counting active capacity.  Provider failures remain fail-closed: the
    deployment stays active until a later read or retry proves its terminal
    workflow state.
    """
    for deployment in store.list_active_deployments():
        _refresh_workflow_state(store, deployment, github)


def _positive_fleet_int(name: str, default: int, *, allow_zero: bool = False) -> int:
    """Read bounded admission settings without allowing unsafe negatives."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    if value > 0 or (allow_zero and value == 0):
        return value
    return default


def require_fleet_management_credential(
    credentials: HTTPAuthorizationCredentials | None = Depends(_fleet_bearer_scheme),
) -> None:
    """Require the dedicated Fleet Deploy bearer credential.

    This intentionally does not call the shared HomeOps OIDC verifier.  The
    simulator's write credential is a separate backend-only secret until the
    later supporting-prerequisite task provisions and rotates it.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=FLEET_MANAGEMENT_REQUIRED_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected = os.environ.get(FLEET_MANAGEMENT_KEY_ENV, "").strip()
    provided = credentials.credentials.strip()
    if not expected:
        logger.error("Fleet management credential is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=FLEET_MANAGEMENT_UNAVAILABLE_ERROR,
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=FLEET_MANAGEMENT_REQUIRED_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )


def _invalid_spec(exc: DeploymentSpecError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "invalid_deployment_spec",
            "message": str(exc),
            "recovery": "Correct the rejected request fields and submit the revised attempt.",
        },
    )


def _submission_limit_error(
    *,
    code: str,
    message: str,
    recovery: str,
    retry_after_seconds: int,
) -> HTTPException:
    """Return a bounded, credential-free admission error for the browser."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": code,
            "message": message,
            "recovery": recovery,
            "retry_after_seconds": retry_after_seconds,
        },
        headers={"Retry-After": str(retry_after_seconds)},
    )


def _deployment_not_found(exc: UnknownDeploymentError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "unknown_deployment", "message": str(exc)},
    )


def _target_not_found(exc: UnknownTargetError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "unknown_target", "message": str(exc)},
    )


def _deployment_conflict(
    exc: DeploymentConflictError | InvalidStateTransitionError,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "deployment_conflict", "message": str(exc)},
    )


@router.get("/health", response_model=FleetHealthResponse)
def fleet_health(store: FleetStoreDependency) -> FleetHealthResponse:
    """Return read-only simulator readiness and the fixed target count."""
    try:
        target_count = len(store.list_vehicles())
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)
    return FleetHealthResponse(target_count=target_count)


@router.get("/fleet", response_model=FleetReadResponse)
def read_fleet(store: FleetStoreDependency) -> FleetReadResponse:
    """Return the current desired/observed state of all simulated targets."""
    try:
        vehicles = store.list_vehicles()
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)
    return FleetReadResponse(
        targets=[_target_response(vehicle) for vehicle in vehicles],
        capabilities=[FleetCapabilityResponse(**item) for item in supported_capabilities()],
    )


@router.get("/fleet/{target_id}", response_model=FleetTargetResponse)
def read_target(target_id: str, store: FleetStoreDependency) -> FleetTargetResponse:
    """Return one simulated target, rejecting unknown IDs explicitly."""
    try:
        vehicle = store.get_vehicle(target_id)
    except UnknownTargetError as exc:
        raise _target_not_found(exc) from None
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)
    return _target_response(vehicle)


@router.get("/deployments/history", response_model=FleetDeploymentHistoryResponse)
def read_deployment_history(
    store: FleetStoreDependency,
    limit: int = Query(default=20, ge=1, le=MAX_DEPLOYMENT_HISTORY),
) -> FleetDeploymentHistoryResponse:
    """Return recent durable attempts without requiring a browser session."""
    try:
        deployments = store.list_deployments(limit)
        return FleetDeploymentHistoryResponse(
            deployments=[_deployment_history_entry(store, deployment) for deployment in deployments]
        )
    except (OSError, sqlite3.Error, FleetStateError, ValueError) as exc:
        _state_unavailable(exc)


@router.get("/deployments/{deployment_id}", response_model=FleetDeploymentResponse)
def read_deployment(
    deployment_id: str,
    store: FleetStoreDependency,
    github: OptionalFleetGitHubDependency,
) -> FleetDeploymentResponse:
    """Return a fresh Actions-plus-simulator snapshot for client polling."""
    try:
        deployment = store.get_deployment(deployment_id)
        deployment, workflow_run, workflow_error = _refresh_workflow_state(
            store, deployment, github
        )
        return _deployment_response(
            store,
            deployment,
            workflow_run=workflow_run,
            workflow_error=workflow_error,
        )
    except UnknownDeploymentError as exc:
        raise _deployment_not_found(exc) from None
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)


@router.post(
    "/deployments/submit",
    response_model=FleetDeploymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_public_deployment(
    request: Request,
    payload: DeploymentSpecRequest,
    store: FleetStoreDependency,
    github: FleetGitHubDependency,
) -> FleetDeploymentResponse:
    """Admit one browser request, commit its manifest, and dispatch the trusted workflow.

    The durable row is created before the external calls.  A dispatch failure
    therefore leaves the exact manifest commit and deployment ID available for
    a safe retry without writing a second manifest path or guessing a run URL.
    """
    try:
        spec = validate_deployment_spec(payload.model_dump(exclude_none=True))
    except DeploymentSpecError as exc:
        raise _invalid_spec(exc) from None

    manifest_content = spec.canonical_json().encode("utf-8")
    manifest_path = f"manifests/{spec.deployment_id}.json"
    manifest_sha256 = hashlib.sha256(manifest_content).hexdigest()
    client_ip = extract_client_ip(
        request.client.host if request.client is not None else None,
        request.headers,
        proxy_config=load_proxy_config(),
    )

    try:
        _reconcile_active_deployments(store, github)
        admission = store.admit_public_deployment(
            spec,
            client_ip=client_ip,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            cooldown_seconds=_positive_fleet_int(
                FLEET_SUBMISSION_COOLDOWN_ENV,
                DEFAULT_FLEET_SUBMISSION_COOLDOWN_SECONDS,
                allow_zero=True,
            ),
            max_active=_positive_fleet_int(FLEET_MAX_ACTIVE_ENV, DEFAULT_FLEET_MAX_ACTIVE),
            reset_daily_cap=_positive_fleet_int(
                FLEET_RESET_DAILY_CAP_ENV,
                DEFAULT_FLEET_RESET_DAILY_CAP,
            ),
        )
    except DeploymentCooldownError as exc:
        raise _submission_limit_error(
            code="submission_cooldown",
            message="Fleet deployment submission cooldown is active.",
            recovery=f"Wait {exc.retry_after_seconds} seconds, then retry the same attempt.",
            retry_after_seconds=exc.retry_after_seconds,
        ) from None
    except DeploymentAdmissionLimitError:
        raise _submission_limit_error(
            code="capacity_full",
            message="Fleet deployment capacity is temporarily full.",
            recovery="Wait for the active run to finish, then retry the same attempt.",
            retry_after_seconds=30,
        ) from None
    except DeploymentDailyLimitError as exc:
        raise _submission_limit_error(
            code="reset_daily_cap",
            message="The conservative daily Fleet reset limit has been reached.",
            recovery="Wait until the next UTC day before requesting another reset.",
            retry_after_seconds=exc.retry_after_seconds,
        ) from None
    except DeploymentConflictError as exc:
        raise _deployment_conflict(exc) from None
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)

    claim_token = uuid.uuid4().hex
    try:
        claimed = store.claim_dispatch(
            spec.deployment_id,
            claim_token,
            lease_seconds=_positive_fleet_int(
                FLEET_DISPATCH_LEASE_ENV,
                DEFAULT_FLEET_DISPATCH_LEASE_SECONDS,
            ),
        )
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)
    if claimed is None:
        try:
            current = store.get_deployment(spec.deployment_id)
        except (OSError, sqlite3.Error, FleetStateError) as exc:
            _state_unavailable(exc)
        return _deployment_response(store, current, idempotent=True)

    operation = "manifest_commit_failed"
    try:
        current = claimed
        if current.manifest_commit_sha is None:
            commit = github.commit_manifest(spec.deployment_id, manifest_content)
            current = store.record_manifest_commit(
                spec.deployment_id,
                claim_token,
                commit.commit_sha,
            )
        assert current.manifest_commit_sha is not None
        operation = "workflow_dispatch_failed"
        dispatch = github.dispatch_workflow(spec.deployment_id, current.manifest_commit_sha)
        current = store.record_dispatch_success(
            spec.deployment_id,
            claim_token,
            workflow_run_id=dispatch.workflow_run_id,
            workflow_url=dispatch.workflow_url,
        )
        return _deployment_response(store, current, idempotent=admission.idempotent)
    except GitHubFleetError:
        # Store only a stable operation code. Provider responses may contain
        # repository, path, or credential-bearing details and never belong in
        # the browser response or durable state.
        logger.warning("Fleet public submission failed during %s", operation)
        try:
            current = store.record_dispatch_failure(
                spec.deployment_id,
                claim_token,
                error=operation,
            )
        except (OSError, sqlite3.Error, FleetStateError) as exc:
            _state_unavailable(exc)
        return _deployment_response(store, current, idempotent=admission.idempotent)
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)


@router.post("/deployments", response_model=FleetDeploymentResponse)
def queue_deployment(
    payload: DeploymentSpecRequest,
    store: FleetStoreDependency,
    _: None = Depends(require_fleet_management_credential),
) -> FleetDeploymentResponse:
    """Persist desired state for a validated deployment request."""
    try:
        spec = validate_deployment_spec(payload.model_dump(exclude_none=True))
    except DeploymentSpecError as exc:
        raise _invalid_spec(exc) from None

    try:
        result = store.queue_deployment(spec)
        return _deployment_response(store, result.deployment, idempotent=result.idempotent)
    except DeploymentConflictError as exc:
        raise _deployment_conflict(exc) from None
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)


@router.post(
    "/deployments/{deployment_id}/events",
    response_model=FleetDeploymentResponse,
)
def append_deployment_event(
    deployment_id: str,
    payload: FleetDeploymentEventRequest,
    store: FleetStoreDependency,
    _: None = Depends(require_fleet_management_credential),
) -> FleetDeploymentResponse:
    """Persist trusted deployer evidence for public readback and reloads."""
    try:
        deployment = store.append_deployment_event(
            deployment_id,
            event_type=payload.event_type,
            schema_version=payload.schema_version,
            artifact_sha256=payload.artifact_sha256,
            target_ids=payload.target_ids,
            status=payload.status,
            detail=payload.detail,
        )
        return _deployment_response(store, deployment)
    except UnknownDeploymentError as exc:
        raise _deployment_not_found(exc) from None
    except (DeploymentConflictError, InvalidStateTransitionError) as exc:
        raise _deployment_conflict(exc) from None
    except UnknownTargetError as exc:
        raise _target_not_found(exc) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_deployment_event", "message": str(exc)},
        ) from None
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)


@router.post(
    "/deployments/{deployment_id}/apply",
    response_model=FleetDeploymentResponse,
)
def apply_deployment(
    deployment_id: str,
    store: FleetStoreDependency,
    payload: DeploymentApplyRequest | None = None,
    _: None = Depends(require_fleet_management_credential),
) -> FleetDeploymentResponse:
    """Apply desired state to the simulator and make observed state converge.

    The simulator models the applying transition and then a successful
    observation in one protected request.  Repeating an already successful
    request is a read-only idempotent replay.
    """
    try:
        deployment = store.get_deployment(deployment_id)
        if _deployment_operation(deployment) == "reset":
            if payload is not None and payload.target_ids is not None:
                raise DeploymentConflictError("reset applies the complete simulated fleet")
            deployment = store.reset_deployment_targets(deployment_id)
            return _deployment_response(store, deployment)
        if payload is not None and payload.target_ids is not None:
            deployment = store.apply_deployment_targets(deployment_id, payload.target_ids)
            return _deployment_response(store, deployment)
        if deployment.status == "succeeded":
            return _deployment_response(store, deployment, idempotent=True)
        if deployment.status == "failed":
            raise _deployment_conflict(
                InvalidStateTransitionError(f"deployment {deployment_id} is already failed")
            )
        if deployment.status == "queued":
            deployment = store.mark_deployment_status(deployment_id, "applying")
        deployment = store.mark_deployment_status(deployment_id, "succeeded")
        return _deployment_response(store, deployment)
    except UnknownDeploymentError as exc:
        raise _deployment_not_found(exc) from None
    except UnknownTargetError as exc:
        raise _target_not_found(exc) from None
    except DeploymentConflictError as exc:
        raise _deployment_conflict(exc) from None
    except InvalidStateTransitionError as exc:
        # Another authorized request may have completed the same deployment
        # between the initial read and transition.  Treat that terminal replay
        # as idempotent while preserving all other conflicts.
        try:
            current = store.get_deployment(deployment_id)
        except UnknownDeploymentError:
            raise _deployment_not_found(exc) from None
        if current.status == "succeeded":
            return _deployment_response(store, current, idempotent=True)
        raise _deployment_conflict(exc) from None
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)


@router.post(
    "/deployments/{deployment_id}/restore",
    response_model=FleetDeploymentResponse,
)
def restore_deployment(
    deployment_id: str,
    payload: DeploymentRestoreRequest,
    store: FleetStoreDependency,
    _: None = Depends(require_fleet_management_credential),
) -> FleetDeploymentResponse:
    """Restore trusted pre-deployment profiles and keep the run failed."""
    try:
        profiles: dict[str, FleetProfile] = {}
        for target in payload.targets:
            if target.target_id in profiles:
                raise DeploymentConflictError(
                    f"rollback contains duplicate target: {target.target_id}"
                )
            try:
                profiles[target.target_id] = FleetProfile(
                    color=target.profile.color,
                    shape=target.profile.shape,
                )
            except ValueError as exc:
                raise DeploymentSpecError(f"rollback profile: {exc}") from None
        deployment = store.restore_deployment_targets(deployment_id, profiles)
        return _deployment_response(store, deployment)
    except DeploymentSpecError as exc:
        raise _invalid_spec(exc) from None
    except UnknownDeploymentError as exc:
        raise _deployment_not_found(exc) from None
    except UnknownTargetError as exc:
        raise _target_not_found(exc) from None
    except (DeploymentConflictError, InvalidStateTransitionError) as exc:
        try:
            store.record_rollback_failure(deployment_id, error=str(exc))
        except (OSError, sqlite3.Error, FleetStateError):
            logger.warning("Fleet rollback failure could not be persisted")
        raise _deployment_conflict(exc) from None
    except (OSError, sqlite3.Error, FleetStateError) as exc:
        _state_unavailable(exc)


__all__ = [
    "FLEET_API_PREFIX",
    "FLEET_MANAGEMENT_KEY_ENV",
    "FLEET_RESET_DAILY_CAP_ENV",
    "FleetCapabilityResponse",
    "FleetDeploymentHistoryEntryResponse",
    "FleetDeploymentHistoryResponse",
    "FleetDeploymentEventRequest",
    "FleetDeploymentEventResponse",
    "FleetDeploymentResponse",
    "FleetHealthResponse",
    "FleetReadResponse",
    "FleetTargetResponse",
    "FleetWorkflowStepResponse",
    "get_fleet_state_store",
    "require_fleet_management_credential",
    "router",
]
