"""Durable state for the simulated Fleet Deploy Lab fleet.

The state boundary is intentionally independent of FastAPI and GitHub Actions.
The API and trusted deployer can both use this module without maintaining
separate copies of the fleet model.  SQLite is used for the small control-plane
store because one transaction can update a deployment record and all of its
target vehicles atomically.  The database path must point at a named volume or
an operator-managed host directory in production; an image-local file is not a
durability boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .deployment_spec import (
    COLORS,
    ENVIRONMENTS,
    KNOWN_TARGET_IDS,
    SHAPES,
    TARGETS_BY_ENVIRONMENT,
    DeploymentSpec,
)

STATE_PATH_ENV = "FLEET_DEPLOY_STATE_PATH"
DEFAULT_STATE_PATH = "/var/lib/homeops/deploy-demo/fleet-state.sqlite3"
STATE_SCHEMA_VERSION = 2

VehicleStatus = Literal["ready", "pending", "applying", "succeeded", "failed"]
DeploymentStatus = Literal["queued", "applying", "succeeded", "failed"]

VEHICLE_STATUSES = ("ready", "pending", "applying", "succeeded", "failed")
DEPLOYMENT_STATUSES = ("queued", "applying", "succeeded", "failed")
DISPATCH_STATUSES = (
    "not_started",
    "pending",
    "committing",
    "dispatching",
    "dispatched",
    "failed",
)
_BASELINE_PROFILES = (
    ("blue", "circle"),
    ("green", "square"),
    ("orange", "triangle"),
    ("purple", "hexagon"),
)
_TARGET_ORDER = tuple(
    target_id for environment in ENVIRONMENTS for target_id in TARGETS_BY_ENVIRONMENT[environment]
)
_TARGET_ORDER_INDEX = {target_id: index for index, target_id in enumerate(_TARGET_ORDER)}


class FleetStateError(RuntimeError):
    """Base class for durable simulated-fleet state failures."""


class DeploymentConflictError(FleetStateError):
    """Raised when a deployment ID or target has incompatible active state."""


class UnknownDeploymentError(FleetStateError):
    """Raised when an operation references a deployment not in the store."""


class UnknownTargetError(FleetStateError):
    """Raised when an operation references a target outside the fixed fleet."""


class InvalidStateTransitionError(FleetStateError):
    """Raised when a deployment status would move backward or after completion."""


class DeploymentCooldownError(FleetStateError):
    """Raised when one client IP submits before its cooldown expires."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("deployment submission cooldown is active")


class DeploymentAdmissionLimitError(FleetStateError):
    """Raised when the bounded global active-deployment admission limit is full."""


class DispatchClaimError(FleetStateError):
    """Raised when a dispatch recovery update loses its durable claim."""


@dataclass(frozen=True, slots=True)
class FleetProfile:
    """The finite profile rendered by a simulated vehicle."""

    color: str
    shape: str

    def __post_init__(self) -> None:
        if self.color not in COLORS:
            raise ValueError(f"profile.color must be one of: {', '.join(COLORS)}")
        if self.shape not in SHAPES:
            raise ValueError(f"profile.shape must be one of: {', '.join(SHAPES)}")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FleetProfile:
        """Create a profile from the same finite shape used by DeploymentSpec."""
        if set(value) != {"color", "shape"}:
            raise ValueError("profile must contain exactly color and shape")
        color = value["color"]
        shape = value["shape"]
        if not isinstance(color, str) or not isinstance(shape, str):
            raise ValueError("profile.color and profile.shape must be strings")
        return cls(color=color, shape=shape)

    def to_dict(self) -> dict[str, str]:
        """Return the JSON-safe profile representation."""
        return {"color": self.color, "shape": self.shape}

    def canonical_json(self) -> str:
        """Return the stable profile representation used for hashing."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @property
    def digest(self) -> str:
        """Return the SHA-256 digest of the canonical profile JSON."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VehicleState:
    """Desired and observed state for one logical vehicle."""

    target_id: str
    label: str
    environment: str
    desired_profile: FleetProfile
    observed_profile: FleetProfile
    status: VehicleStatus
    active_deployment_id: str | None
    last_error: str | None
    updated_at: str

    @property
    def desired_digest(self) -> str:
        """Return the digest of the desired profile."""
        return self.desired_profile.digest

    @property
    def observed_digest(self) -> str:
        """Return the digest of the observed profile."""
        return self.observed_profile.digest

    def to_dict(self) -> dict[str, object]:
        """Return the public-safe snapshot shape used by later API work."""
        return {
            "target_id": self.target_id,
            "label": self.label,
            "environment": self.environment,
            "desired": self.desired_profile.to_dict(),
            "desired_digest": self.desired_digest,
            "observed": self.observed_profile.to_dict(),
            "observed_digest": self.observed_digest,
            "status": self.status,
            "active_deployment_id": self.active_deployment_id,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class DeploymentState:
    """Durable status for one deployment request."""

    deployment_id: str
    target_ids: tuple[str, ...]
    profile: FleetProfile
    profile_digest: str
    status: DeploymentStatus
    error: str | None
    created_at: str
    updated_at: str
    manifest_path: str | None = None
    manifest_sha256: str | None = None
    manifest_commit_sha: str | None = None
    workflow_run_id: int | None = None
    workflow_url: str | None = None
    dispatch_status: str = "not_started"
    dispatch_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-safe deployment status representation."""
        return {
            "deployment_id": self.deployment_id,
            "target_ids": list(self.target_ids),
            "profile": self.profile.to_dict(),
            "profile_digest": self.profile_digest,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "manifest_commit_sha": self.manifest_commit_sha,
            "workflow_run_id": self.workflow_run_id,
            "workflow_url": self.workflow_url,
            "dispatch_status": self.dispatch_status,
            "dispatch_error": self.dispatch_error,
        }


@dataclass(frozen=True, slots=True)
class QueueResult:
    """Result of queueing a deployment, including duplicate detection."""

    deployment: DeploymentState
    idempotent: bool


def _baseline_profile(target_id: str) -> FleetProfile:
    """Return the deterministic readable baseline for a target."""
    suffix = int(target_id.rsplit("-", 1)[1])
    color, shape = _BASELINE_PROFILES[(suffix - 1) % len(_BASELINE_PROFILES)]
    return FleetProfile(color=color, shape=shape)


def _target_metadata(target_id: str) -> tuple[str, str]:
    """Return the environment and human-readable label for a known target."""
    if target_id not in KNOWN_TARGET_IDS:
        raise UnknownTargetError(f"unknown fleet target: {target_id}")
    environment, suffix = target_id.split("-vehicle-", 1)
    return environment, f"{environment.upper()}-{suffix}"


def _now_iso(clock: Callable[[], datetime]) -> str:
    """Return an RFC 3339 UTC timestamp from the injected clock."""
    value = clock()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_target_ids(target_ids: Iterable[str]) -> tuple[str, ...]:
    """Normalize and validate target IDs before entering a transaction."""
    normalized = tuple(target_ids)
    if not normalized:
        raise UnknownTargetError("a deployment must target at least one vehicle")
    unknown = tuple(target_id for target_id in normalized if target_id not in KNOWN_TARGET_IDS)
    if unknown:
        raise UnknownTargetError(f"unknown fleet target(s): {', '.join(unknown)}")
    if len(set(normalized)) != len(normalized):
        raise DeploymentConflictError("a deployment cannot contain duplicate target IDs")
    return tuple(sorted(normalized, key=_TARGET_ORDER_INDEX.__getitem__))


def _validate_error(error: str | None) -> str | None:
    """Bound persisted operator/provider error text."""
    if error is None:
        return None
    if not isinstance(error, str) or not error:
        raise ValueError("deployment error must be a non-empty string or None")
    return error[:2_000]


class FleetStateStore:
    """SQLite-backed state store for the fixed twelve-vehicle simulator."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_environment(
        cls,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> FleetStateStore:
        """Build a store using the deployment's explicit persistent path."""
        return cls(os.environ.get(STATE_PATH_ENV, DEFAULT_STATE_PATH), clock=clock)

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        """Create the schema and seed any missing baseline vehicle rows."""
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_state_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_vehicles (
                    target_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    desired_color TEXT NOT NULL,
                    desired_shape TEXT NOT NULL,
                    desired_digest TEXT NOT NULL,
                    observed_color TEXT NOT NULL,
                    observed_shape TEXT NOT NULL,
                    observed_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('ready', 'pending', 'applying', 'succeeded', 'failed')
                    ),
                    active_deployment_id TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_deployments (
                    deployment_id TEXT PRIMARY KEY,
                    target_ids_json TEXT NOT NULL,
                    profile_color TEXT NOT NULL,
                    profile_shape TEXT NOT NULL,
                    profile_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('queued', 'applying', 'succeeded', 'failed')
                    ),
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    manifest_path TEXT,
                    manifest_sha256 TEXT,
                    manifest_commit_sha TEXT,
                    workflow_run_id INTEGER,
                    workflow_url TEXT,
                    dispatch_status TEXT NOT NULL DEFAULT 'not_started',
                    dispatch_error TEXT,
                    dispatch_claim_token TEXT,
                    dispatch_claimed_at TEXT,
                    UNIQUE(deployment_id, profile_digest)
                )
                """
            )
            deployment_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(fleet_deployments)")
            }
            migration_columns = (
                ("manifest_path", "TEXT"),
                ("manifest_sha256", "TEXT"),
                ("manifest_commit_sha", "TEXT"),
                ("workflow_run_id", "INTEGER"),
                ("workflow_url", "TEXT"),
                ("dispatch_status", "TEXT NOT NULL DEFAULT 'not_started'"),
                ("dispatch_error", "TEXT"),
                ("dispatch_claim_token", "TEXT"),
                ("dispatch_claimed_at", "TEXT"),
            )
            for column, definition in migration_columns:
                if column not in deployment_columns:
                    connection.execute(
                        f"ALTER TABLE fleet_deployments ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_dispatch_admissions (
                    client_ip TEXT PRIMARY KEY,
                    last_admitted_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS fleet_deployments_profile_digest "
                "ON fleet_deployments(profile_digest)"
            )
            existing = connection.execute(
                "SELECT value FROM fleet_state_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO fleet_state_meta(key, value) VALUES ('schema_version', ?)",
                    (str(STATE_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(existing["value"])
                except (TypeError, ValueError) as exc:
                    raise FleetStateError("invalid fleet state schema version") from exc
                if version > STATE_SCHEMA_VERSION or version < 1:
                    raise FleetStateError(
                        "unsupported fleet state schema version: " + str(existing["value"])
                    )
                if version < STATE_SCHEMA_VERSION:
                    connection.execute(
                        "UPDATE fleet_state_meta SET value = ? WHERE key = 'schema_version'",
                        (str(STATE_SCHEMA_VERSION),),
                    )
            self._seed_missing_vehicles(connection)

    def _seed_missing_vehicles(self, connection: sqlite3.Connection) -> None:
        """Insert deterministic baseline rows without overwriting live state."""
        timestamp = _now_iso(self._clock)
        for environment in ENVIRONMENTS:
            for target_id in TARGETS_BY_ENVIRONMENT[environment]:
                environment_name, label = _target_metadata(target_id)
                profile = _baseline_profile(target_id)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO fleet_vehicles(
                        target_id, label, environment, desired_color, desired_shape,
                        desired_digest, observed_color, observed_shape, observed_digest,
                        status, active_deployment_id, last_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', NULL, NULL, ?)
                    """,
                    (
                        target_id,
                        label,
                        environment_name,
                        profile.color,
                        profile.shape,
                        profile.digest,
                        profile.color,
                        profile.shape,
                        profile.digest,
                        timestamp,
                    ),
                )

    @staticmethod
    def _vehicle_from_row(row: sqlite3.Row) -> VehicleState:
        """Convert a SQLite vehicle row into the immutable domain object."""
        status = row["status"]
        if status not in VEHICLE_STATUSES:
            raise FleetStateError(f"unsupported persisted vehicle status: {status}")
        return VehicleState(
            target_id=row["target_id"],
            label=row["label"],
            environment=row["environment"],
            desired_profile=FleetProfile(row["desired_color"], row["desired_shape"]),
            observed_profile=FleetProfile(row["observed_color"], row["observed_shape"]),
            status=status,
            active_deployment_id=row["active_deployment_id"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _deployment_from_row(row: sqlite3.Row) -> DeploymentState:
        """Convert a SQLite deployment row into the immutable domain object."""
        status = row["status"]
        if status not in DEPLOYMENT_STATUSES:
            raise FleetStateError(f"unsupported persisted deployment status: {status}")
        dispatch_status = row["dispatch_status"] or "not_started"
        if dispatch_status not in DISPATCH_STATUSES:
            raise FleetStateError(f"unsupported persisted dispatch status: {dispatch_status}")
        target_ids = tuple(json.loads(row["target_ids_json"]))
        return DeploymentState(
            deployment_id=row["deployment_id"],
            target_ids=target_ids,
            profile=FleetProfile(row["profile_color"], row["profile_shape"]),
            profile_digest=row["profile_digest"],
            status=status,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            manifest_path=row["manifest_path"],
            manifest_sha256=row["manifest_sha256"],
            manifest_commit_sha=row["manifest_commit_sha"],
            workflow_run_id=row["workflow_run_id"],
            workflow_url=row["workflow_url"],
            dispatch_status=dispatch_status,
            dispatch_error=row["dispatch_error"],
        )

    def list_vehicles(self) -> tuple[VehicleState, ...]:
        """Return all twelve vehicles in stable environment/number order."""
        connection = self._read_connection()
        try:
            rows = connection.execute("SELECT * FROM fleet_vehicles").fetchall()
            vehicles = [self._vehicle_from_row(row) for row in rows]
            return tuple(
                sorted(vehicles, key=lambda vehicle: _TARGET_ORDER_INDEX[vehicle.target_id])
            )
        finally:
            connection.close()

    def get_vehicle(self, target_id: str) -> VehicleState:
        """Return one vehicle or fail closed for an unknown target."""
        _target_metadata(target_id)
        connection = self._read_connection()
        try:
            row = connection.execute(
                "SELECT * FROM fleet_vehicles WHERE target_id = ?", (target_id,)
            ).fetchone()
            if row is None:
                raise FleetStateError(f"fleet target is missing from state: {target_id}")
            return self._vehicle_from_row(row)
        finally:
            connection.close()

    def get_deployment(self, deployment_id: str) -> DeploymentState:
        """Return one deployment or raise an explicit missing-record error."""
        connection = self._read_connection()
        try:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise UnknownDeploymentError(f"unknown deployment: {deployment_id}")
            return self._deployment_from_row(row)
        finally:
            connection.close()

    @staticmethod
    def _validate_manifest_metadata(manifest_path: str, manifest_sha256: str) -> None:
        """Keep persisted manifest identity bounded and path-safe."""
        if (
            not manifest_path.startswith("manifests/")
            or manifest_path.endswith("/")
            or ".." in manifest_path
            or "\\" in manifest_path
        ):
            raise ValueError("manifest path must be a relative manifests path")
        if len(manifest_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in manifest_sha256
        ):
            raise ValueError("manifest SHA-256 must be lowercase hexadecimal")

    def _insert_new_deployment(
        self,
        connection: sqlite3.Connection,
        spec: DeploymentSpec,
        *,
        profile: FleetProfile,
        target_ids: tuple[str, ...],
        timestamp: str,
        manifest_path: str | None = None,
        manifest_sha256: str | None = None,
        dispatch_status: str = "not_started",
    ) -> DeploymentState:
        """Insert one deployment and its target intent inside an open transaction."""
        if dispatch_status not in DISPATCH_STATUSES:
            raise ValueError("unsupported dispatch status")
        placeholders = ", ".join("?" for _ in target_ids)
        active_rows = connection.execute(
            f"SELECT target_id, active_deployment_id FROM fleet_vehicles "
            f"WHERE target_id IN ({placeholders}) AND status IN ('pending', 'applying')",
            target_ids,
        ).fetchall()
        conflicts = [f"{row['target_id']} ({row['active_deployment_id']})" for row in active_rows]
        if conflicts:
            raise DeploymentConflictError(
                "target(s) already have active deployment(s): " + ", ".join(conflicts)
            )

        connection.execute(
            """
            INSERT INTO fleet_deployments(
                deployment_id, target_ids_json, profile_color, profile_shape,
                profile_digest, status, error, created_at, updated_at,
                manifest_path, manifest_sha256, dispatch_status
            ) VALUES (?, ?, ?, ?, ?, 'queued', NULL, ?, ?, ?, ?, ?)
            """,
            (
                spec.deployment_id,
                json.dumps(target_ids, separators=(",", ":")),
                profile.color,
                profile.shape,
                profile.digest,
                timestamp,
                timestamp,
                manifest_path,
                manifest_sha256,
                dispatch_status,
            ),
        )
        updated = connection.execute(
            f"""
            UPDATE fleet_vehicles
            SET desired_color = ?, desired_shape = ?, desired_digest = ?,
                status = 'pending', active_deployment_id = ?, last_error = NULL,
                updated_at = ?
            WHERE target_id IN ({placeholders})
            """,
            (
                profile.color,
                profile.shape,
                profile.digest,
                spec.deployment_id,
                timestamp,
                *target_ids,
            ),
        )
        if updated.rowcount != len(target_ids):
            raise FleetStateError(
                f"deployment {spec.deployment_id} did not update every target atomically"
            )
        row = connection.execute(
            "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
            (spec.deployment_id,),
        ).fetchone()
        assert row is not None
        return self._deployment_from_row(row)

    def queue_deployment(self, spec: DeploymentSpec) -> QueueResult:
        """Atomically persist desired state for a validated internal deployment spec."""
        profile = FleetProfile(spec.profile_color, spec.profile_shape)
        target_ids = _validate_target_ids(spec.expanded_target_ids)
        timestamp = _now_iso(self._clock)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (spec.deployment_id,),
            ).fetchone()
            if row is not None:
                existing = self._deployment_from_row(row)
                if existing.profile_digest != profile.digest or existing.target_ids != target_ids:
                    raise DeploymentConflictError(
                        f"deployment ID already exists with a different profile or target set: "
                        f"{spec.deployment_id}"
                    )
                return QueueResult(deployment=existing, idempotent=True)
            deployment = self._insert_new_deployment(
                connection,
                spec,
                profile=profile,
                target_ids=target_ids,
                timestamp=timestamp,
            )
            return QueueResult(deployment=deployment, idempotent=False)

    def admit_public_deployment(
        self,
        spec: DeploymentSpec,
        *,
        client_ip: str,
        manifest_path: str,
        manifest_sha256: str,
        cooldown_seconds: int,
        max_active: int,
    ) -> QueueResult:
        """Admit one browser request under SQLite's cross-process write lock.

        The ``BEGIN IMMEDIATE`` transaction is the global admission lock. It
        serializes the IP cooldown, active-deployment count, deployment insert,
        target reservation, and admission timestamp so concurrent backend
        workers cannot each pass a check against the same old state.
        """
        if not client_ip.strip():
            raise ValueError("client IP must not be blank")
        if cooldown_seconds < 0 or max_active < 1:
            raise ValueError("public admission limits are invalid")
        self._validate_manifest_metadata(manifest_path, manifest_sha256)
        profile = FleetProfile(spec.profile_color, spec.profile_shape)
        target_ids = _validate_target_ids(spec.expanded_target_ids)
        now = self._clock()
        timestamp = _now_iso(lambda: now)

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (spec.deployment_id,),
            ).fetchone()
            if row is not None:
                existing = self._deployment_from_row(row)
                if (
                    existing.manifest_sha256 != manifest_sha256
                    or existing.profile_digest != profile.digest
                    or existing.target_ids != target_ids
                ):
                    raise DeploymentConflictError(
                        "deployment ID already exists with a different manifest: "
                        f"{spec.deployment_id}"
                    )
                return QueueResult(deployment=existing, idempotent=True)

            admission = connection.execute(
                "SELECT last_admitted_at FROM fleet_dispatch_admissions WHERE client_ip = ?",
                (client_ip,),
            ).fetchone()
            if admission is not None:
                try:
                    last_admitted = datetime.fromisoformat(
                        admission["last_admitted_at"].replace("Z", "+00:00")
                    )
                except (AttributeError, TypeError, ValueError) as exc:
                    raise FleetStateError("invalid fleet admission timestamp") from exc
                elapsed = (now.astimezone(UTC) - last_admitted.astimezone(UTC)).total_seconds()
                if elapsed < cooldown_seconds:
                    raise DeploymentCooldownError(int(cooldown_seconds - elapsed) + 1)

            active_count = connection.execute(
                "SELECT COUNT(*) AS count FROM fleet_deployments "
                "WHERE status IN ('queued', 'applying')"
            ).fetchone()["count"]
            if active_count >= max_active:
                raise DeploymentAdmissionLimitError("active deployment limit is reached")

            deployment = self._insert_new_deployment(
                connection,
                spec,
                profile=profile,
                target_ids=target_ids,
                timestamp=timestamp,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                dispatch_status="pending",
            )
            connection.execute(
                """
                INSERT INTO fleet_dispatch_admissions(client_ip, last_admitted_at)
                VALUES (?, ?)
                ON CONFLICT(client_ip) DO UPDATE SET last_admitted_at = excluded.last_admitted_at
                """,
                (client_ip, timestamp),
            )
            return QueueResult(deployment=deployment, idempotent=False)

    def claim_dispatch(
        self,
        deployment_id: str,
        claim_token: str,
        *,
        lease_seconds: int,
    ) -> DeploymentState | None:
        """Claim manifest commit/dispatch work, or return ``None`` when busy."""
        if not claim_token.strip() or lease_seconds < 1:
            raise ValueError("dispatch claim parameters are invalid")
        now = self._clock()
        timestamp = _now_iso(lambda: now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise UnknownDeploymentError(f"unknown deployment: {deployment_id}")
            existing = self._deployment_from_row(row)
            if existing.dispatch_status == "dispatched":
                return None
            claimed_at = row["dispatch_claimed_at"]
            if row["dispatch_claim_token"] and claimed_at:
                try:
                    claim_age = (
                        now.astimezone(UTC)
                        - datetime.fromisoformat(claimed_at.replace("Z", "+00:00")).astimezone(UTC)
                    ).total_seconds()
                except (AttributeError, TypeError, ValueError) as exc:
                    raise FleetStateError("invalid dispatch claim timestamp") from exc
                if claim_age < lease_seconds:
                    return None
            next_status = "committing" if existing.manifest_commit_sha is None else "dispatching"
            connection.execute(
                """
                UPDATE fleet_deployments
                SET dispatch_status = ?, dispatch_claim_token = ?, dispatch_claimed_at = ?,
                    dispatch_error = NULL, updated_at = ?
                WHERE deployment_id = ?
                """,
                (next_status, claim_token, timestamp, timestamp, deployment_id),
            )
            updated = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            assert updated is not None
            return self._deployment_from_row(updated)

    def record_manifest_commit(
        self,
        deployment_id: str,
        claim_token: str,
        commit_sha: str,
    ) -> DeploymentState:
        """Persist the exact manifest commit before any workflow dispatch."""
        if len(commit_sha) != 40 or any(c not in "0123456789abcdef" for c in commit_sha):
            raise ValueError("manifest commit SHA must be lowercase hexadecimal")
        timestamp = _now_iso(self._clock)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise UnknownDeploymentError(f"unknown deployment: {deployment_id}")
            existing = self._deployment_from_row(row)
            if row["dispatch_claim_token"] != claim_token:
                raise DispatchClaimError("manifest commit claim is no longer owned")
            if existing.manifest_commit_sha is not None:
                if existing.manifest_commit_sha != commit_sha:
                    raise DispatchClaimError("deployment already has a different manifest commit")
                return existing
            connection.execute(
                """
                UPDATE fleet_deployments
                SET manifest_commit_sha = ?, dispatch_status = 'dispatching',
                    dispatch_error = NULL, updated_at = ?
                WHERE deployment_id = ? AND dispatch_claim_token = ?
                """,
                (commit_sha, timestamp, deployment_id, claim_token),
            )
            updated = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            assert updated is not None
            return self._deployment_from_row(updated)

    def record_dispatch_success(
        self,
        deployment_id: str,
        claim_token: str,
        *,
        workflow_run_id: int | None = None,
        workflow_url: str | None = None,
    ) -> DeploymentState:
        """Record a successful dispatch without inventing absent run metadata."""
        if workflow_run_id is not None and workflow_run_id < 1:
            raise ValueError("workflow run ID must be positive")
        if workflow_url is not None and (
            len(workflow_url) > 512 or not workflow_url.startswith("https://")
        ):
            raise ValueError("workflow URL must be a bounded HTTPS URL")
        timestamp = _now_iso(self._clock)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise UnknownDeploymentError(f"unknown deployment: {deployment_id}")
            if row["dispatch_claim_token"] != claim_token:
                raise DispatchClaimError("workflow dispatch claim is no longer owned")
            connection.execute(
                """
                UPDATE fleet_deployments
                SET dispatch_status = 'dispatched', workflow_run_id = ?, workflow_url = ?,
                    dispatch_error = NULL, dispatch_claim_token = NULL,
                    dispatch_claimed_at = NULL, updated_at = ?
                WHERE deployment_id = ? AND dispatch_claim_token = ?
                """,
                (workflow_run_id, workflow_url, timestamp, deployment_id, claim_token),
            )
            updated = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            assert updated is not None
            return self._deployment_from_row(updated)

    def record_dispatch_failure(
        self,
        deployment_id: str,
        claim_token: str,
        *,
        error: str,
    ) -> DeploymentState:
        """Leave the queued deployment recoverable after an external failure."""
        error = _validate_error(error)
        if error is None:
            raise ValueError("dispatch error is required")
        timestamp = _now_iso(self._clock)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise UnknownDeploymentError(f"unknown deployment: {deployment_id}")
            if row["dispatch_claim_token"] != claim_token:
                raise DispatchClaimError("workflow dispatch claim is no longer owned")
            connection.execute(
                """
                UPDATE fleet_deployments
                SET dispatch_status = 'failed', dispatch_error = ?,
                    dispatch_claim_token = NULL, dispatch_claimed_at = NULL, updated_at = ?
                WHERE deployment_id = ? AND dispatch_claim_token = ?
                """,
                (error, timestamp, deployment_id, claim_token),
            )
            updated = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            assert updated is not None
            return self._deployment_from_row(updated)

    def mark_deployment_status(
        self,
        deployment_id: str,
        status: DeploymentStatus,
        *,
        error: str | None = None,
    ) -> DeploymentState:
        """Atomically record queued, applying, succeeded, or failed state.

        A successful transition copies desired profiles into observed profiles.
        A failed transition deliberately leaves observed profiles unchanged, so
        the UI can show the real desired/observed divergence.  Repeating the
        same terminal transition is idempotent.
        """
        if status not in DEPLOYMENT_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(DEPLOYMENT_STATUSES)}")
        error = _validate_error(error)
        timestamp = _now_iso(self._clock)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise UnknownDeploymentError(f"unknown deployment: {deployment_id}")
            existing = self._deployment_from_row(row)
            if existing.status in ("succeeded", "failed"):
                if existing.status == status and existing.error == error:
                    return existing
                raise InvalidStateTransitionError(
                    f"deployment {deployment_id} is already {existing.status}"
                )
            if existing.status == "applying" and status == "queued":
                raise InvalidStateTransitionError(
                    f"deployment {deployment_id} cannot return to queued"
                )

            connection.execute(
                """
                UPDATE fleet_deployments
                SET status = ?, error = ?, updated_at = ?
                WHERE deployment_id = ?
                """,
                (status, error, timestamp, deployment_id),
            )
            target_ids = existing.target_ids
            placeholders = ", ".join("?" for _ in target_ids)
            if status == "succeeded":
                updated_vehicles = connection.execute(
                    f"""
                    UPDATE fleet_vehicles
                    SET observed_color = desired_color, observed_shape = desired_shape,
                        observed_digest = desired_digest, status = 'succeeded',
                        last_error = NULL, updated_at = ?
                    WHERE target_id IN ({placeholders})
                      AND active_deployment_id = ?
                    """,
                    (timestamp, *target_ids, deployment_id),
                )
            elif status == "failed":
                updated_vehicles = connection.execute(
                    f"""
                    UPDATE fleet_vehicles
                    SET status = 'failed', last_error = ?, updated_at = ?
                    WHERE target_id IN ({placeholders})
                      AND active_deployment_id = ?
                    """,
                    (error, timestamp, *target_ids, deployment_id),
                )
            else:
                vehicle_status = "pending" if status == "queued" else "applying"
                updated_vehicles = connection.execute(
                    f"""
                    UPDATE fleet_vehicles
                    SET status = ?, last_error = NULL, updated_at = ?
                    WHERE target_id IN ({placeholders})
                      AND active_deployment_id = ?
                    """,
                    (vehicle_status, timestamp, *target_ids, deployment_id),
                )
            if updated_vehicles.rowcount != len(target_ids):
                raise FleetStateError(
                    f"deployment {deployment_id} did not update every target atomically"
                )
            updated = connection.execute(
                "SELECT * FROM fleet_deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            assert updated is not None
            return self._deployment_from_row(updated)


__all__ = [
    "DEFAULT_STATE_PATH",
    "DEPLOYMENT_STATUSES",
    "DISPATCH_STATUSES",
    "STATE_PATH_ENV",
    "STATE_SCHEMA_VERSION",
    "VEHICLE_STATUSES",
    "DeploymentConflictError",
    "DeploymentCooldownError",
    "DeploymentAdmissionLimitError",
    "DeploymentState",
    "DispatchClaimError",
    "FleetProfile",
    "FleetStateError",
    "FleetStateStore",
    "InvalidStateTransitionError",
    "QueueResult",
    "UnknownDeploymentError",
    "UnknownTargetError",
    "VehicleState",
]
