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
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    DeploymentAdmissionLimitError,
    DeploymentConflictError,
    DeploymentCooldownError,
    DeploymentSpecError,
    DeploymentState,
    FleetStateError,
    FleetStateStore,
    GitHubFleetClient,
    GitHubFleetConfigurationError,
    GitHubFleetError,
    InvalidStateTransitionError,
    UnknownDeploymentError,
    UnknownTargetError,
    VehicleState,
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
DEFAULT_FLEET_SUBMISSION_COOLDOWN_SECONDS = 60
DEFAULT_FLEET_MAX_ACTIVE = 1
DEFAULT_FLEET_DISPATCH_LEASE_SECONDS = 300

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


class FleetReadResponse(BaseModel):
    """The anonymous fleet snapshot consumed by the future demo frontend."""

    model_config = ConfigDict(extra="forbid")

    simulated: Literal[True] = True
    target_kind: Literal["simulated"] = "simulated"
    targets: list[FleetTargetResponse]


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


class FleetDeploymentResponse(BaseModel):
    """Deployment plus fresh target state used for apply and verification."""

    model_config = ConfigDict(extra="forbid")

    simulated: Literal[True] = True
    target_kind: Literal["simulated"] = "simulated"
    deployment_id: str
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
    idempotent: bool = False
    manifest_path: str | None = None
    manifest_sha256: str | None = None
    manifest_commit_sha: str | None = None
    workflow_run_id: int | None = None
    workflow_url: str | None = None
    dispatch_status: DispatchStatusResponse = "not_started"
    dispatch_error: str | None = None


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


def _deployment_response(
    store: FleetStateStore,
    deployment: DeploymentState,
    *,
    idempotent: bool = False,
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

    return FleetDeploymentResponse(
        deployment_id=deployment.deployment_id,
        target_ids=list(deployment.target_ids),
        desired=_profile_response(deployment.profile.color, deployment.profile.shape),
        desired_digest=deployment.profile_digest,
        status=deployment.status,
        error=deployment.error,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
        verification=verification,
        verified=verification == "verified",
        targets=targets,
        idempotent=idempotent,
        manifest_path=deployment.manifest_path,
        manifest_sha256=deployment.manifest_sha256,
        manifest_commit_sha=deployment.manifest_commit_sha,
        workflow_run_id=deployment.workflow_run_id,
        workflow_url=deployment.workflow_url,
        dispatch_status=deployment.dispatch_status,
        dispatch_error=deployment.dispatch_error,
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
        detail={"code": "invalid_deployment_spec", "message": str(exc)},
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
    return FleetReadResponse(targets=[_target_response(vehicle) for vehicle in vehicles])


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


@router.get("/deployments/{deployment_id}", response_model=FleetDeploymentResponse)
def read_deployment(
    deployment_id: str,
    store: FleetStoreDependency,
) -> FleetDeploymentResponse:
    """Return a fresh deployment snapshot suitable for client verification."""
    try:
        deployment = store.get_deployment(deployment_id)
        return _deployment_response(store, deployment)
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
    """Admit one browser request, commit its manifest, and dispatch PR09.

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
        )
    except DeploymentCooldownError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Fleet deployment submission cooldown is active",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from None
    except DeploymentAdmissionLimitError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Fleet deployment capacity is temporarily full",
            headers={"Retry-After": "30"},
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
    "/deployments/{deployment_id}/apply",
    response_model=FleetDeploymentResponse,
)
def apply_deployment(
    deployment_id: str,
    store: FleetStoreDependency,
    _: None = Depends(require_fleet_management_credential),
) -> FleetDeploymentResponse:
    """Apply desired state to the simulator and make observed state converge.

    The simulator models the applying transition and then a successful
    observation in one protected request.  Repeating an already successful
    request is a read-only idempotent replay.
    """
    try:
        deployment = store.get_deployment(deployment_id)
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


__all__ = [
    "FLEET_API_PREFIX",
    "FLEET_MANAGEMENT_KEY_ENV",
    "FleetDeploymentResponse",
    "FleetHealthResponse",
    "FleetReadResponse",
    "FleetTargetResponse",
    "get_fleet_state_store",
    "require_fleet_management_credential",
    "router",
]
