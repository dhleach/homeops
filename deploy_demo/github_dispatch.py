"""Server-side GitHub Contents and Actions boundary for Fleet Deploy.

The browser never calls this module and never receives its credential.  The
backend uses it to write one canonical manifest to the dedicated data branch,
then dispatches the read-only trusted-master workflow from PR09.  The adapter
does not create the manifest branch and does not infer a workflow-run URL when
GitHub's dispatch endpoint returns no run metadata.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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
]
