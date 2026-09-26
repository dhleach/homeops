"""Server-side GitHub Contents and Actions boundary for Fleet Deploy.

The browser never calls this module and never receives its credential.  The
backend uses it to write one canonical manifest to the dedicated data branch,
then dispatches the read-only trusted-master workflow from PR09.  The adapter
does not create the manifest branch and does not infer a workflow-run URL when
GitHub's dispatch endpoint returns no run metadata.  Later reads resolve the
exact run by its deterministic workflow run name and retrieve bounded job
state.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

GITHUB_API_URL = "https://api.github.com"
GITHUB_REPOSITORY_ENV = "FLEET_DEPLOY_GITHUB_REPOSITORY"
GITHUB_TOKEN_ENV = "FLEET_DEPLOY_GITHUB_TOKEN"
MANIFEST_BRANCH_ENV = "FLEET_DEPLOY_MANIFEST_BRANCH"
WORKFLOW_FILE_ENV = "FLEET_DEPLOY_WORKFLOW_FILE"
DEFAULT_REPOSITORY = "dhleach/homeops"
DEFAULT_MANIFEST_BRANCH = "fleet-deployments"
DEFAULT_WORKFLOW_FILE = "fleet-deploy.yml"
MAX_RESPONSE_BYTES = 1_048_576
MAX_MANIFEST_BYTES = 16_384
_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?[.]json$")
_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_WORKFLOW_VALUE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MAX_WORKFLOW_JOBS = 100


class GitHubFleetError(RuntimeError):
    """A safe, non-secret GitHub integration failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubFleetConfigurationError(GitHubFleetError):
    """Raised when the backend credential/configuration is absent or invalid."""


class ManifestConflictError(GitHubFleetError):
    """Raised when a supposedly unique manifest path contains different bytes."""


@dataclass(frozen=True, slots=True)
class ManifestCommitReceipt:
    """The commit identity returned by the Contents API."""

    commit_sha: str


@dataclass(frozen=True, slots=True)
class WorkflowDispatchReceipt:
    """Optional run metadata returned by a dispatch-capable GitHub endpoint."""

    workflow_run_id: int | None = None
    workflow_url: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowJobReceipt:
    """Bounded state for one GitHub Actions job."""

    job_id: int
    name: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    workflow_url: str | None
    failed_step: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRunReceipt:
    """Run and job state read from GitHub Actions."""

    workflow_run_id: int
    workflow_url: str | None
    status: str
    conclusion: str | None
    created_at: str
    updated_at: str
    jobs: tuple[WorkflowJobReceipt, ...]


def _validate_deployment_id(value: str) -> str:
    """Validate an ID before placing it in a repository path."""
    candidate = f"{value}.json"
    if not isinstance(value, str) or _DEPLOYMENT_ID_PATTERN.fullmatch(candidate) is None:
        raise GitHubFleetConfigurationError("deployment ID is not a safe manifest identifier")
    return value


def _validate_sha(value: object, *, name: str) -> str:
    """Require the exact lower-case Git object identity used by PR09."""
    if not isinstance(value, str) or _SHA1_PATTERN.fullmatch(value) is None:
        raise GitHubFleetError(f"{name} is not a full lower-case commit SHA")
    return value


def _safe_workflow_url(value: object) -> str | None:
    """Accept only bounded HTTPS metadata returned by the provider."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512 or not value.startswith("https://"):
        return None
    return value


def _safe_workflow_value(value: object, *, name: str, allow_none: bool = False) -> str | None:
    """Accept a small provider enum without reflecting arbitrary API text."""
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or _WORKFLOW_VALUE_PATTERN.fullmatch(value) is None:
        raise GitHubFleetError(f"GitHub workflow {name} was invalid")
    return value


def _safe_workflow_timestamp(value: object, *, name: str) -> str:
    """Keep provider timestamps bounded while preserving their exact value."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise GitHubFleetError(f"GitHub workflow {name} was invalid")
    return value


def _safe_optional_workflow_timestamp(value: object, *, name: str) -> str | None:
    """Accept nullable job timestamps from the Actions API."""
    if value is None:
        return None
    return _safe_workflow_timestamp(value, name=name)


def _safe_workflow_text(value: object, *, name: str, max_length: int = 256) -> str:
    """Accept bounded human-readable provider metadata."""
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise GitHubFleetError(f"GitHub workflow {name} was invalid")
    return value


def _workflow_job_from_payload(payload: object) -> WorkflowJobReceipt:
    """Parse one bounded Actions job object."""
    if not isinstance(payload, Mapping):
        raise GitHubFleetError("GitHub workflow job was not an object")
    job_id = payload.get("id")
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1:
        raise GitHubFleetError("GitHub workflow job ID was invalid")
    steps = payload.get("steps", [])
    failed_step: str | None = None
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping) or step.get("conclusion") not in {
                "failure",
                "timed_out",
                "cancelled",
            }:
                continue
            raw_name = step.get("name")
            if isinstance(raw_name, str) and raw_name and len(raw_name) <= 256:
                failed_step = raw_name
                break
    return WorkflowJobReceipt(
        job_id=job_id,
        name=_safe_workflow_text(payload.get("name"), name="job name"),
        status=_safe_workflow_value(payload.get("status"), name="job status") or "unknown",
        conclusion=_safe_workflow_value(
            payload.get("conclusion"), name="job conclusion", allow_none=True
        ),
        started_at=_safe_optional_workflow_timestamp(
            payload.get("started_at"), name="job start timestamp"
        ),
        completed_at=_safe_optional_workflow_timestamp(
            payload.get("completed_at"), name="job completion timestamp"
        ),
        workflow_url=_safe_workflow_url(payload.get("html_url")),
        failed_step=failed_step,
    )


def _workflow_run_from_payload(payload: object) -> WorkflowRunReceipt:
    """Parse run metadata without making any assumptions about completion."""
    if not isinstance(payload, Mapping):
        raise GitHubFleetError("GitHub workflow run was not an object")
    run_id = payload.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise GitHubFleetError("GitHub workflow run ID was invalid")
    return WorkflowRunReceipt(
        workflow_run_id=run_id,
        workflow_url=_safe_workflow_url(payload.get("html_url")),
        status=_safe_workflow_value(payload.get("status"), name="run status") or "unknown",
        conclusion=_safe_workflow_value(
            payload.get("conclusion"), name="run conclusion", allow_none=True
        ),
        created_at=_safe_workflow_timestamp(payload.get("created_at"), name="creation timestamp"),
        updated_at=_safe_workflow_timestamp(payload.get("updated_at"), name="update timestamp"),
        jobs=(),
    )


class GitHubFleetClient:
    """Minimal Contents/Actions client with injectable HTTP transport."""

    def __init__(
        self,
        token: str,
        *,
        repository: str = DEFAULT_REPOSITORY,
        manifest_branch: str = DEFAULT_MANIFEST_BRANCH,
        workflow_file: str = DEFAULT_WORKFLOW_FILE,
        api_url: str = GITHUB_API_URL,
        timeout: float = 10.0,
        opener: Callable[..., object] | None = None,
    ) -> None:
        if not token.strip():
            raise GitHubFleetConfigurationError("Fleet GitHub credential is not configured")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise GitHubFleetConfigurationError("Fleet GitHub repository is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", manifest_branch) or ".." in manifest_branch:
            raise GitHubFleetConfigurationError("Fleet manifest branch is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", workflow_file) or ".." in workflow_file:
            raise GitHubFleetConfigurationError("Fleet workflow file is invalid")
        if timeout <= 0:
            raise GitHubFleetConfigurationError("Fleet GitHub timeout must be positive")
        self._token = token.strip()
        self.repository = repository
        self.manifest_branch = manifest_branch
        self.workflow_file = workflow_file
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or build_opener().open

    @classmethod
    def from_environment(cls) -> GitHubFleetClient:
        """Build the server-side adapter without exposing the token to callers."""
        token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
        if not token:
            raise GitHubFleetConfigurationError("Fleet GitHub credential is not configured")
        return cls(
            token,
            repository=os.environ.get(GITHUB_REPOSITORY_ENV, DEFAULT_REPOSITORY).strip(),
            manifest_branch=os.environ.get(MANIFEST_BRANCH_ENV, DEFAULT_MANIFEST_BRANCH).strip(),
            workflow_file=os.environ.get(WORKFLOW_FILE_ENV, DEFAULT_WORKFLOW_FILE).strip(),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> tuple[int, Mapping[str, object] | list[object] | None, Mapping[str, str]]:
        """Perform one bounded request and redact all provider error content."""
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "homeops-fleet-deploy",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.api_url}{path}", data=body, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status_code = int(response.getcode())
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
        except HTTPError as exc:
            raise GitHubFleetError(
                f"GitHub {method} request returned HTTP {exc.code}", status_code=exc.code
            ) from None
        except (OSError, URLError, ValueError) as exc:
            raise GitHubFleetError(
                f"GitHub {method} request failed: {type(exc).__name__}"
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GitHubFleetError("GitHub response exceeded the bounded response size")
        if status_code not in expected_statuses:
            raise GitHubFleetError(
                f"GitHub {method} request returned HTTP {status_code}",
                status_code=status_code,
            )
        if not raw:
            return status_code, None, response_headers
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubFleetError("GitHub returned invalid JSON") from exc
        if not isinstance(decoded, (Mapping, list)):
            raise GitHubFleetError("GitHub returned an unexpected JSON shape")
        return status_code, decoded, response_headers

    def _manifest_path(self, deployment_id: str) -> str:
        """Return the one deterministic path owned by a deployment ID."""
        deployment_id = _validate_deployment_id(deployment_id)
        return f"manifests/{deployment_id}.json"

    def _get_existing_manifest(self, path: str) -> Mapping[str, object] | None:
        """Read one path without treating a missing manifest branch as success."""
        encoded_path = quote(path, safe="/")
        branch = quote(self.manifest_branch, safe="")
        try:
            _, payload, _ = self._request(
                "GET",
                f"/repos/{self.repository}/contents/{encoded_path}?ref={branch}",
            )
        except GitHubFleetError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(payload, Mapping):
            raise GitHubFleetError("GitHub manifest response was not an object")
        return payload

    def _latest_manifest_commit(self, path: str) -> str:
        """Recover an existing file's commit identity without inventing a run."""
        encoded_path = quote(path, safe="/")
        branch = quote(self.manifest_branch, safe="")
        _, payload, _ = self._request(
            "GET",
            f"/repos/{self.repository}/commits?path={encoded_path}&sha={branch}&per_page=1",
        )
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], Mapping):
            raise GitHubFleetError("GitHub did not return a manifest commit identity")
        return _validate_sha(payload[0].get("sha"), name="manifest commit SHA")

    def commit_manifest(self, deployment_id: str, content: bytes) -> ManifestCommitReceipt:
        """Create one canonical manifest commit, or recover an identical prior commit."""
        if not isinstance(content, bytes) or not content or len(content) > MAX_MANIFEST_BYTES:
            raise GitHubFleetError("manifest content is outside the bounded size")
        path = self._manifest_path(deployment_id)
        existing = self._get_existing_manifest(path)
        if existing is not None:
            encoded = existing.get("content")
            try:
                normalized_encoded = (
                    "".join(encoded.split()) if isinstance(encoded, str) else encoded
                )
                existing_content = base64.b64decode(normalized_encoded, validate=True)
            except (TypeError, ValueError):
                raise GitHubFleetError("existing manifest content is invalid") from None
            if existing_content != content:
                raise ManifestConflictError("manifest path already contains different bytes")
            commit_sha = existing.get("commit_sha")
            if not _SHA1_PATTERN.fullmatch(commit_sha or ""):
                commit_sha = self._latest_manifest_commit(path)
            return ManifestCommitReceipt(
                commit_sha=_validate_sha(commit_sha, name="manifest commit SHA")
            )

        _, payload, _ = self._request(
            "PUT",
            f"/repos/{self.repository}/contents/{quote(path, safe='/')}",
            payload={
                "branch": self.manifest_branch,
                "content": base64.b64encode(content).decode("ascii"),
                "message": f"fleet: record manifest {deployment_id}",
            },
            expected_statuses=(200, 201),
        )
        if not isinstance(payload, Mapping):
            raise GitHubFleetError("GitHub manifest commit response was empty")
        commit = payload.get("commit")
        if not isinstance(commit, Mapping):
            raise GitHubFleetError("GitHub manifest commit identity was missing")
        return ManifestCommitReceipt(
            commit_sha=_validate_sha(commit.get("sha"), name="manifest commit SHA")
        )

    def dispatch_workflow(
        self,
        deployment_id: str,
        event_commit_sha: str,
    ) -> WorkflowDispatchReceipt:
        """Dispatch PR09 from trusted master with matching validated inputs."""
        path = self._manifest_path(deployment_id)
        del path  # Validate the deployment ID without duplicating path logic.
        event_commit_sha = _validate_sha(event_commit_sha, name="event commit SHA")
        workflow = quote(self.workflow_file, safe="/")
        _, payload, headers = self._request(
            "POST",
            f"/repos/{self.repository}/actions/workflows/{workflow}/dispatches",
            payload={
                "ref": "master",
                "inputs": {
                    "deployment_id": deployment_id,
                    "event_commit_sha": event_commit_sha,
                },
            },
            expected_statuses=(200, 201, 204),
        )
        run_id: int | None = None
        workflow_url: str | None = None
        if isinstance(payload, Mapping):
            raw_run_id = payload.get("workflow_run_id", payload.get("run_id", payload.get("id")))
            if isinstance(raw_run_id, int) and not isinstance(raw_run_id, bool) and raw_run_id > 0:
                run_id = raw_run_id
            workflow_url = _safe_workflow_url(
                payload.get("workflow_url")
                or payload.get("run_url")
                or payload.get("html_url")
                or payload.get("url")
            )
        if workflow_url is None:
            workflow_url = _safe_workflow_url(headers.get("location"))
        return WorkflowDispatchReceipt(workflow_run_id=run_id, workflow_url=workflow_url)

    def manifest_commit_url(self, commit_sha: str) -> str:
        """Return the public URL for a validated manifest commit."""
        validated_sha = _validate_sha(commit_sha, name="manifest commit SHA")
        return f"https://github.com/{self.repository}/commit/{validated_sha}"

    def _workflow_runs_path(self) -> str:
        """Return the Actions run-list path for this trusted workflow."""
        workflow = quote(self.workflow_file, safe="/")
        query = urlencode({"event": "workflow_dispatch", "branch": "master", "per_page": "100"})
        return f"/repos/{self.repository}/actions/workflows/{workflow}/runs?{query}"

    def _workflow_run_jobs_path(self, workflow_run_id: int) -> str:
        """Return the bounded job-list path for one run."""
        if (
            not isinstance(workflow_run_id, int)
            or isinstance(workflow_run_id, bool)
            or workflow_run_id < 1
        ):
            raise GitHubFleetError("workflow run ID must be positive")
        return (
            f"/repos/{self.repository}/actions/runs/{workflow_run_id}/jobs"
            f"?per_page={MAX_WORKFLOW_JOBS}"
        )

    def get_workflow_run(self, workflow_run_id: int) -> WorkflowRunReceipt:
        """Read one Actions run and its jobs without exposing provider payloads."""
        _, payload, _ = self._request(
            "GET",
            f"/repos/{self.repository}/actions/runs/{workflow_run_id}",
        )
        run = _workflow_run_from_payload(payload)
        if run.workflow_run_id != workflow_run_id:
            raise GitHubFleetError("GitHub returned a different workflow run ID")

        _, jobs_payload, _ = self._request("GET", self._workflow_run_jobs_path(workflow_run_id))
        if not isinstance(jobs_payload, Mapping):
            raise GitHubFleetError("GitHub workflow jobs response was not an object")
        raw_jobs = jobs_payload.get("jobs", [])
        if not isinstance(raw_jobs, list) or len(raw_jobs) > MAX_WORKFLOW_JOBS:
            raise GitHubFleetError("GitHub workflow jobs response was invalid")
        jobs = tuple(_workflow_job_from_payload(job) for job in raw_jobs)
        return WorkflowRunReceipt(
            workflow_run_id=run.workflow_run_id,
            workflow_url=run.workflow_url,
            status=run.status,
            conclusion=run.conclusion,
            created_at=run.created_at,
            updated_at=run.updated_at,
            jobs=jobs,
        )

    # The descriptive alias keeps the call site clear when a route refreshes
    # an existing deployment instead of discovering a new one.
    read_workflow_run = get_workflow_run

    def find_workflow_run(self, deployment_id: str) -> WorkflowRunReceipt | None:
        """Find the exact dispatched run by its deterministic run name.

        A 204 dispatch response contains no run identity.  PR12 adds a
        ``run-name`` based on the deployment ID, so this lookup never chooses
        an unrelated recent workflow merely because it happened to be newest.
        """
        _validate_deployment_id(deployment_id)
        _, payload, _ = self._request("GET", self._workflow_runs_path())
        if not isinstance(payload, Mapping):
            raise GitHubFleetError("GitHub workflow runs response was not an object")
        raw_runs = payload.get("workflow_runs", [])
        if not isinstance(raw_runs, list) or len(raw_runs) > MAX_WORKFLOW_JOBS:
            raise GitHubFleetError("GitHub workflow runs response was invalid")
        expected_name = f"Fleet deployment {deployment_id}"
        candidates: list[tuple[str, int]] = []
        for raw_run in raw_runs:
            if not isinstance(raw_run, Mapping):
                raise GitHubFleetError("GitHub workflow run list contained an invalid item")
            display_title = raw_run.get("display_title") or raw_run.get("run_name")
            if display_title != expected_name:
                continue
            candidate = _workflow_run_from_payload(raw_run)
            candidates.append((candidate.created_at, candidate.workflow_run_id))
        if not candidates:
            return None
        _, workflow_run_id = max(candidates)
        return self.get_workflow_run(workflow_run_id)


__all__ = [
    "DEFAULT_MANIFEST_BRANCH",
    "DEFAULT_REPOSITORY",
    "DEFAULT_WORKFLOW_FILE",
    "GITHUB_REPOSITORY_ENV",
    "GITHUB_TOKEN_ENV",
    "GitHubFleetClient",
    "GitHubFleetConfigurationError",
    "GitHubFleetError",
    "ManifestCommitReceipt",
    "ManifestConflictError",
    "MANIFEST_BRANCH_ENV",
    "WORKFLOW_FILE_ENV",
    "WorkflowDispatchReceipt",
    "WorkflowJobReceipt",
    "WorkflowRunReceipt",
]
