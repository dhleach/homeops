"""Contract tests for the server-side GitHub manifest/dispatch boundary."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from deploy_demo.github_dispatch import (
    GitHubFleetClient,
    GitHubFleetError,
    ManifestConflictError,
)

COMMIT_SHA = "a" * 40


@dataclass
class FakeResponse:
    """Small urllib response double with explicit status and headers."""

    status: int
    body: bytes = b""
    headers: dict[str, str] | None = None

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def getcode(self) -> int:
        return self.status

    def read(self, _limit: int = -1) -> bytes:
        return self.body


class GitHubHarness:
    """Route requests to deterministic Contents and dispatch responses."""

    def __init__(self, *, dispatch_body: dict[str, object] | None = None) -> None:
        self.requests: list[Request] = []
        self.dispatch_body = dispatch_body
        self.manifest: bytes | None = None
        self.existing_commit: str | None = None
        self.workflow_run: dict[str, object] | None = None
        self.workflow_jobs: list[dict[str, object]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        del timeout
        self.requests.append(request)
        if request.method == "GET" and "/contents/manifests/" in request.full_url:
            if self.manifest is None:
                return FakeResponse(404, b'{"message":"Not Found"}', {})
            payload = {
                "content": base64.b64encode(self.manifest).decode("ascii"),
                "commit_sha": self.existing_commit,
            }
            return FakeResponse(200, json.dumps(payload).encode(), {})
        if request.method == "GET" and "/commits?" in request.full_url:
            return FakeResponse(200, json.dumps([{"sha": self.existing_commit}]).encode(), {})
        if request.method == "GET" and "/actions/workflows/" in request.full_url:
            runs = [] if self.workflow_run is None else [self.workflow_run]
            return FakeResponse(200, json.dumps({"workflow_runs": runs}).encode(), {})
        if request.method == "GET" and "/actions/runs/" in request.full_url:
            if request.full_url.endswith("/jobs?per_page=100"):
                return FakeResponse(200, json.dumps({"jobs": self.workflow_jobs}).encode(), {})
            if self.workflow_run is None:
                return FakeResponse(404, b'{"message":"Not Found"}', {})
            return FakeResponse(200, json.dumps(self.workflow_run).encode(), {})
        if request.method == "PUT":
            payload = json.loads(request.data.decode())
            self.manifest = base64.b64decode(payload["content"])
            self.existing_commit = COMMIT_SHA
            return FakeResponse(
                201,
                json.dumps({"commit": {"sha": COMMIT_SHA}}).encode(),
                {},
            )
        if request.method == "POST":
            if self.dispatch_body is None:
                return FakeResponse(204, b"", {})
            return FakeResponse(200, json.dumps(self.dispatch_body).encode(), {})
        raise AssertionError(f"unexpected request: {request.method} {request.full_url}")


def client(harness: GitHubHarness) -> GitHubFleetClient:
    """Build the adapter with a fake transport and a non-production API URL."""
    return GitHubFleetClient(
        "server-side-test-token",
        api_url="https://api.github.test",
        opener=harness,
    )


def test_manifest_commit_and_dispatch_bind_exact_identities() -> None:
    harness = GitHubHarness()
    adapter = client(harness)
    content = b'{"deployment_id":"demo-001"}'

    commit = adapter.commit_manifest("demo-001", content)
    dispatch = adapter.dispatch_workflow("demo-001", commit.commit_sha)

    assert commit.commit_sha == COMMIT_SHA
    assert dispatch.workflow_run_id is None
    assert dispatch.workflow_url is None
    assert [request.method for request in harness.requests] == ["GET", "PUT", "POST"]
    put_payload = json.loads(harness.requests[1].data.decode())
    assert put_payload["branch"] == "fleet-deployments"
    assert base64.b64decode(put_payload["content"]) == content
    post_payload = json.loads(harness.requests[2].data.decode())
    assert post_payload == {
        "inputs": {"deployment_id": "demo-001", "event_commit_sha": COMMIT_SHA},
        "ref": "master",
    }
    assert "server-side-test-token" not in harness.requests[1].data.decode()


def test_dispatch_preserves_run_metadata_only_when_provider_returns_it() -> None:
    harness = GitHubHarness(
        dispatch_body={
            "id": 1234,
            "html_url": "https://github.com/dhleach/homeops/actions/runs/1234",
        }
    )
    adapter = client(harness)

    receipt = adapter.dispatch_workflow("demo-002", COMMIT_SHA)

    assert receipt.workflow_run_id == 1234
    assert receipt.workflow_url == "https://github.com/dhleach/homeops/actions/runs/1234"


def test_identical_existing_manifest_is_recovered_without_a_second_commit() -> None:
    harness = GitHubHarness()
    harness.manifest = b'{"deployment_id":"demo-003"}'
    harness.existing_commit = COMMIT_SHA
    adapter = client(harness)

    receipt = adapter.commit_manifest("demo-003", harness.manifest)

    assert receipt.commit_sha == COMMIT_SHA
    assert [request.method for request in harness.requests] == ["GET"]


def test_different_existing_manifest_fails_closed_without_overwriting() -> None:
    harness = GitHubHarness()
    harness.manifest = b'{"deployment_id":"other"}'
    harness.existing_commit = COMMIT_SHA
    adapter = client(harness)

    with pytest.raises(ManifestConflictError):
        adapter.commit_manifest("demo-004", b'{"deployment_id":"demo-004"}')

    assert [request.method for request in harness.requests] == ["GET"]


def test_missing_branch_or_provider_error_does_not_leak_response_body() -> None:
    def failing_opener(request: Request, *, timeout: float) -> FakeResponse:
        del request, timeout
        raise HTTPError(
            "https://api.github.test",
            403,
            "forbidden",
            {},
            None,
        )

    adapter = GitHubFleetClient(
        "server-side-test-token",
        api_url="https://api.github.test",
        opener=failing_opener,
    )

    with pytest.raises(GitHubFleetError, match="HTTP 403") as caught:
        adapter.dispatch_workflow("demo-005", COMMIT_SHA)
    assert "forbidden" not in str(caught.value)
    assert "server-side-test-token" not in str(caught.value)


def test_workflow_run_lookup_requires_exact_run_name_and_reads_jobs() -> None:
    harness = GitHubHarness()
    harness.workflow_run = {
        "id": 9876,
        "display_title": "Fleet deployment demo-006",
        "html_url": "https://github.com/dhleach/homeops/actions/runs/9876",
        "status": "in_progress",
        "conclusion": None,
        "created_at": "2026-09-26T12:00:00Z",
        "updated_at": "2026-09-26T12:00:10Z",
    }
    harness.workflow_jobs = [
        {
            "id": 123,
            "name": "Deploy immutable artifact to simulator",
            "status": "in_progress",
            "conclusion": None,
            "started_at": "2026-09-26T12:00:05Z",
            "completed_at": None,
            "html_url": "https://github.com/dhleach/homeops/actions/runs/9876/job/123",
            "steps": [],
        }
    ]

    receipt = client(harness).find_workflow_run("demo-006")

    assert receipt is not None
    assert receipt.workflow_run_id == 9876
    assert receipt.status == "in_progress"
    assert receipt.jobs[0].name == "Deploy immutable artifact to simulator"
    assert receipt.jobs[0].status == "in_progress"
    assert any(
        "/actions/workflows/" in request.full_url and "event=workflow_dispatch" in request.full_url
        for request in harness.requests
    )
