"""Tests for the public HomeOps deployment smoke checks.

Revision history:
  2026-08-18  Added healthy-stack and failure-path coverage for the release gate
              so public deployment checks cannot silently regress.
"""

from __future__ import annotations

import json

import pytest
from deploy_smoke_check import SmokeCheckError, SmokeResponse, run_smoke_checks


def _responses() -> dict[str, SmokeResponse]:
    telemetry = {
        "floor_1": 68.0,
        "floor_2": 67.5,
        "floor_3": 69.0,
        "outdoor": 42.0,
        "furnace_active": False,
        "floor_1_call": False,
        "floor_2_call": False,
        "floor_3_call": False,
        "floor_1_setpoint": 68.0,
        "floor_2_setpoint": 68.0,
        "floor_3_setpoint": 68.0,
        "last_updated": "2026-08-18T22:00:00Z",
        "error": None,
    }
    return {
        "https://frontend/": SmokeResponse(
            200, b'<title>HomeOps</title><div id="root"></div>', "https://frontend/"
        ),
        "https://api/health": SmokeResponse(200, b'{"status":"ok"}', "https://api/health"),
        "https://api/openapi.json": SmokeResponse(
            200,
            json.dumps({"paths": {"/health": {}, "/api/current-temps": {}}}).encode(),
            "https://api/openapi.json",
        ),
        "https://api/api/current-temps": SmokeResponse(
            200, json.dumps(telemetry).encode(), "https://api/api/current-temps"
        ),
        "https://api/grafana/api/health": SmokeResponse(
            200, b'{"database":"ok"}', "https://api/grafana/api/health"
        ),
        "https://api/prometheus/-/healthy": SmokeResponse(
            200, b"Prometheus Server is Healthy.", "https://api/prometheus/-/healthy"
        ),
    }


def _fake_fetcher(responses: dict[str, SmokeResponse]):
    def fetch(url: str, timeout: float) -> SmokeResponse:
        del timeout
        return responses[url]

    return fetch


def test_run_smoke_checks_passes_for_healthy_stack():
    results = run_smoke_checks(
        frontend_url="https://frontend",
        api_url="https://api",
        fetcher=_fake_fetcher(_responses()),
    )

    assert len(results) == 6
    assert results[0].startswith("frontend:")
    assert results[-1].startswith("prometheus:")


def test_run_smoke_checks_can_skip_observability_checks():
    results = run_smoke_checks(
        frontend_url="https://frontend",
        api_url="https://api",
        include_observability=False,
        fetcher=_fake_fetcher(_responses()),
    )

    assert len(results) == 4
    assert all("grafana" not in result and "prometheus" not in result for result in results)


def test_run_smoke_checks_fails_when_telemetry_reports_backend_error():
    responses = _responses()
    telemetry = json.loads(responses["https://api/api/current-temps"].body)
    telemetry["error"] = "Prometheus unreachable"
    responses["https://api/api/current-temps"] = SmokeResponse(
        200, json.dumps(telemetry).encode(), "https://api/api/current-temps"
    )

    with pytest.raises(SmokeCheckError, match="telemetry reports an error"):
        run_smoke_checks(
            frontend_url="https://frontend",
            api_url="https://api",
            fetcher=_fake_fetcher(responses),
        )


def test_run_smoke_checks_fails_when_frontend_is_not_the_homeops_spa():
    responses = _responses()
    responses["https://frontend/"] = SmokeResponse(
        200, b"<html><body>unexpected application</body></html>", "https://frontend/"
    )

    with pytest.raises(SmokeCheckError, match="frontend missing markers"):
        run_smoke_checks(
            frontend_url="https://frontend",
            api_url="https://api",
            fetcher=_fake_fetcher(responses),
        )
