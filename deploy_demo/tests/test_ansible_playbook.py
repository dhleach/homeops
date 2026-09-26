"""Integration tests for the Ansible Fleet Deploy playbook."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from deploy_demo.fleet_state import FleetProfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = REPOSITORY_ROOT / "ansible" / "deploy.yml"
INVENTORY = REPOSITORY_ROOT / "ansible" / "inventory.yml"


def _response_body(
    spec: dict[str, Any],
    *,
    status: str,
    verified: bool,
    observed_profile: dict[str, str] | None = None,
) -> dict[str, Any]:
    profile = spec["profile"]
    artifact = FleetProfile(**profile)
    observed = observed_profile or profile
    observed_digest = FleetProfile(**observed).digest
    targets = [
        {
            "target_id": target_id,
            "label": target_id.upper(),
            "environment": target_id.split("-vehicle-", 1)[0],
            "simulated": True,
            "desired": profile,
            "desired_digest": artifact.digest,
            "observed": observed,
            "observed_digest": observed_digest,
            "status": "succeeded" if verified else "ready",
            "active_deployment_id": spec["deployment_id"] if verified else None,
            "last_error": None,
            "updated_at": "2026-01-01T00:00:00Z",
        }
        for target_id in [
            "test-vehicle-01",
            "test-vehicle-02",
            "test-vehicle-03",
            "test-vehicle-04",
        ]
    ]
    return {
        "simulated": True,
        "target_kind": "simulated",
        "deployment_id": spec["deployment_id"],
        "target_ids": [target["target_id"] for target in targets],
        "desired": profile,
        "desired_digest": artifact.digest,
        "status": status,
        "error": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "verification": "verified" if verified else "pending",
        "verified": verified,
        "targets": targets,
    }


class FleetMockHandler(BaseHTTPRequestHandler):
    """Small deterministic API double for the Ansible URI tasks."""

    server: FleetMockServer

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_json()
        self.server.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.server.failure_mode == "queue" and self.path.endswith("/deployments"):
            self._send(503, {"detail": "synthetic queue failure"})
            return
        if self.path.endswith("/deployments"):
            queue_status = "applying" if self.server.failure_mode == "applying" else "queued"
            self._send(200, _response_body(body, status=queue_status, verified=False))
            return
        if self.path.endswith("/apply"):
            self._send(200, _response_body(self.server.spec, status="succeeded", verified=True))
            return
        self._send(404, {"detail": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        self.server.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.server.failure_mode == "mismatch":
            self._send(
                200,
                _response_body(
                    self.server.spec,
                    status="succeeded",
                    verified=True,
                    observed_profile={"color": "green", "shape": "circle"},
                ),
            )
            return
        self._send(200, _response_body(self.server.spec, status="succeeded", verified=True))

    def log_message(self, *_args: object) -> None:
        return


class FleetMockServer(ThreadingHTTPServer):
    """HTTP server state shared with the request handler."""

    def __init__(self, spec: dict[str, Any], failure_mode: str | None = None) -> None:
        super().__init__(("127.0.0.1", 0), FleetMockHandler)
        self.spec = spec
        self.failure_mode = failure_mode
        self.requests: list[tuple[str, str, str | None, dict[str, Any] | None]] = []


@pytest.fixture
def ansible_playbook() -> list[str]:
    command = shutil.which("ansible-playbook")
    if command is None:
        if os.environ.get("PYTHONPATH", "").find("ansible") < 0:
            pytest.skip("ansible-core is required for Ansible playbook integration tests")
        return [sys.executable, "-m", "ansible.cli.playbook"]
    return [command]


def _run_playbook(
    ansible_playbook: list[str],
    tmp_path: Path,
    server: FleetMockServer,
    spec: dict[str, Any],
    inventory_path: Path = INVENTORY,
) -> subprocess.CompletedProcess[str]:
    spec_path = tmp_path / "deployment-spec.json"
    artifact_path = tmp_path / "profile.json"
    spec_path.write_text(
        json.dumps(spec, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    artifact_path.write_text(
        json.dumps(spec["profile"], ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(REPOSITORY_ROOT / "ansible" / "ansible.cfg")
    env["FLEET_DEPLOY_API_KEY"] = "synthetic-secret"
    command = [
        *ansible_playbook,
        "-i",
        str(inventory_path),
        str(PLAYBOOK),
        "-e",
        f"api_base_url=http://127.0.0.1:{server.server_port}/deploy/api",
        "-e",
        f"spec_path={spec_path}",
        "-e",
        f"artifact_path={artifact_path}",
    ]
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _start_server(spec: dict[str, Any], failure_mode: str | None = None):
    server = FleetMockServer(spec, failure_mode=failure_mode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server: FleetMockServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def valid_spec() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "deployment_id": "ansible-playbook-001",
        "environment": "test",
        "profile": {"color": "purple", "shape": "hexagon"},
        "implementation": "ansible",
        "strategy": "rolling",
        "failure_mode": "rollback",
    }


def test_playbook_sends_shared_spec_and_verifies_fresh_readback(
    ansible_playbook: list[str], tmp_path: Path
) -> None:
    spec = valid_spec()
    server, thread = _start_server(spec)
    try:
        result = _run_playbook(ansible_playbook, tmp_path, server, spec)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, result.stdout + result.stderr
    assert [request[0:2] for request in server.requests] == [
        ("POST", "/deploy/api/deployments"),
        ("POST", "/deploy/api/deployments/ansible-playbook-001/apply"),
        ("GET", "/deploy/api/deployments/ansible-playbook-001"),
    ]
    assert server.requests[0][2] == "Bearer synthetic-secret"
    assert server.requests[1][2] == "Bearer synthetic-secret"
    assert server.requests[2][2] is None
    assert server.requests[0][3] == spec


def test_playbook_accepts_concurrent_applying_queue_state(
    ansible_playbook: list[str], tmp_path: Path
) -> None:
    spec = valid_spec()
    server, thread = _start_server(spec, failure_mode="applying")
    try:
        result = _run_playbook(ansible_playbook, tmp_path, server, spec)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, result.stdout + result.stderr
    assert [request[0:2] for request in server.requests] == [
        ("POST", "/deploy/api/deployments"),
        ("POST", "/deploy/api/deployments/ansible-playbook-001/apply"),
        ("GET", "/deploy/api/deployments/ansible-playbook-001"),
    ]


def test_playbook_fails_nonzero_on_protected_api_failure(
    ansible_playbook: list[str], tmp_path: Path
) -> None:
    spec = valid_spec()
    server, thread = _start_server(spec, failure_mode="queue")
    try:
        result = _run_playbook(ansible_playbook, tmp_path, server, spec)
    finally:
        _stop_server(server, thread)

    assert result.returncode != 0
    assert [request[0:2] for request in server.requests] == [("POST", "/deploy/api/deployments")]


def test_playbook_fails_nonzero_on_fresh_readback_mismatch(
    ansible_playbook: list[str], tmp_path: Path
) -> None:
    spec = valid_spec()
    server, thread = _start_server(spec, failure_mode="mismatch")
    try:
        result = _run_playbook(ansible_playbook, tmp_path, server, spec)
    finally:
        _stop_server(server, thread)

    assert result.returncode != 0
    assert server.requests[-1][0:2] == (
        "GET",
        "/deploy/api/deployments/ansible-playbook-001",
    )


def test_playbook_fails_nonzero_on_inventory_target_mismatch(
    ansible_playbook: list[str], tmp_path: Path
) -> None:
    spec = valid_spec()
    invalid_inventory = tmp_path / "inventory.yml"
    invalid_inventory.write_text(
        INVENTORY.read_text(encoding="utf-8").replace(
            "fleet_target_id: test-vehicle-04",
            "fleet_target_id: wrong-target",
        ),
        encoding="utf-8",
    )
    server, thread = _start_server(spec)
    try:
        result = _run_playbook(
            ansible_playbook,
            tmp_path,
            server,
            spec,
            inventory_path=invalid_inventory,
        )
    finally:
        _stop_server(server, thread)

    assert result.returncode != 0
    assert server.requests == []
