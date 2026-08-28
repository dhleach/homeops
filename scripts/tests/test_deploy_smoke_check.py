"""Tests for the public HomeOps deployment smoke checks.

Revision history:
  2026-08-27  Added authenticated diagnostic probe coverage so the optional
              release check verifies a non-empty, error-free complete answer.
  2026-08-18  Added healthy-stack and failure-path coverage for the release gate
              so public deployment checks cannot silently regress.
  2026-08-21  Added an OpenAPI auth/quota contract fixture so deployments cannot
              pass without the protected diagnostic route being documented.
  2026-08-27  Added Bob evaluation route and artifact fixtures so the optional
              post-Terraform smoke gate covers the published safety boundary.
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
    report = {
        "schema_version": "evaluation-report.v1",
        "mode": "deterministic",
        "status": "passed",
        "execution": {
            "network_enabled": False,
            "external_mutations_enabled": False,
            "credentials_loaded": False,
            "model_calls": 0,
        },
        "artifacts": {"seeded_traces_published": False},
        "release_gate": {"status": "passed"},
    }
    live = {
        "schema_version": "evaluation-live-trials.v1",
        "mode": "scripted",
        "execution": {
            "model_network_enabled": False,
            "tool_network_enabled": False,
            "external_mutations_enabled": False,
            "production_path_enabled": False,
        },
        "artifacts": {
            "raw_model_outputs_published": False,
            "raw_prompts_published": False,
        },
    }
    return {
        "https://frontend/": SmokeResponse(
            200, b'<title>HomeOps</title><div id="root"></div>', "https://frontend/"
        ),
        "https://frontend/bob/evals/": SmokeResponse(
            200,
            b'<title>Evaluation observability</title><div id="gate-status"></div>'
            b"<h2>Optional live-model observations</h2>",
            "https://frontend/bob/evals/",
        ),
        "https://frontend/bob/evals/evaluation-report.v1.json": SmokeResponse(
            200,
            json.dumps(report).encode(),
            "https://frontend/bob/evals/evaluation-report.v1.json",
        ),
        "https://frontend/bob/evals/evaluation-live-trials.v1.json": SmokeResponse(
            200,
            json.dumps(live).encode(),
            "https://frontend/bob/evals/evaluation-live-trials.v1.json",
        ),
        "https://api/health": SmokeResponse(200, b'{"status":"ok"}', "https://api/health"),
        "https://api/openapi.json": SmokeResponse(
            200,
            json.dumps(
                {
                    "paths": {
                        "/health": {},
                        "/api/current-temps": {},
                        "/api/diagnostic": {
                            "post": {
                                "security": [{"HTTPBearer": []}],
                                "responses": {"401": {}, "403": {}, "429": {}, "503": {}},
                            }
                        },
                    },
                    "components": {"securitySchemes": {"HTTPBearer": {"type": "http"}}},
                }
            ).encode(),
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


def _fake_poster(response: SmokeResponse, calls: list[tuple[str, dict, str, float]]):
    def post(url: str, payload: dict, token: str, timeout: float) -> SmokeResponse:
        calls.append((url, payload, token, timeout))
        return response

    return post


def test_run_smoke_checks_passes_for_healthy_stack():
    results = run_smoke_checks(
        frontend_url="https://frontend",
        api_url="https://api",
        fetcher=_fake_fetcher(_responses()),
    )

    assert len(results) == 6
    assert results[0].startswith("frontend:")
    assert results[-1].startswith("prometheus:")


def test_run_smoke_checks_can_verify_bob_evaluation_route_and_artifacts():
    results = run_smoke_checks(
        frontend_url="https://frontend",
        api_url="https://api",
        include_bob_evaluation=True,
        fetcher=_fake_fetcher(_responses()),
    )

    assert len(results) == 7
    assert results[1].startswith("bob/evals:")


def test_run_smoke_checks_can_skip_observability_checks():
    results = run_smoke_checks(
        frontend_url="https://frontend",
        api_url="https://api",
        include_observability=False,
        fetcher=_fake_fetcher(_responses()),
    )

    assert len(results) == 4
    assert all("grafana" not in result and "prometheus" not in result for result in results)


def test_run_smoke_checks_can_probe_authenticated_diagnostic():
    calls: list[tuple[str, dict, str, float]] = []
    diagnostic_response = SmokeResponse(
        200,
        b'{"answer":"Conditions look healthy.","context_chars":321,"error":null}',
        "https://api/api/diagnostic",
    )

    results = run_smoke_checks(
        frontend_url="https://frontend",
        api_url="https://api",
        include_diagnostic=True,
        diagnostic_token="probe-token",
        diagnostic_question="Is the system healthy?",
        poster=_fake_poster(diagnostic_response, calls),
        fetcher=_fake_fetcher(_responses()),
    )

    assert len(results) == 7
    assert results[4] == "api: authenticated diagnostic returned a complete answer"
    assert calls == [
        (
            "https://api/api/diagnostic",
            {"question": "Is the system healthy?"},
            "probe-token",
            15.0,
        )
    ]


def test_run_smoke_checks_requires_token_for_authenticated_diagnostic():
    calls: list[tuple[str, dict, str, float]] = []

    with pytest.raises(SmokeCheckError, match="requires HOMEOPS_DIAGNOSTIC_TOKEN"):
        run_smoke_checks(
            frontend_url="https://frontend",
            api_url="https://api",
            include_diagnostic=True,
            poster=_fake_poster(SmokeResponse(200, b"{}", "https://api/api/diagnostic"), calls),
            fetcher=_fake_fetcher(_responses()),
        )

    assert calls == []


def test_run_smoke_checks_rejects_incomplete_diagnostic_response():
    calls: list[tuple[str, dict, str, float]] = []
    diagnostic_response = SmokeResponse(
        200,
        b'{"answer":"","context_chars":321,'
        b'"error":"Diagnostic response was incomplete; please retry."}',
        "https://api/api/diagnostic",
    )

    with pytest.raises(SmokeCheckError, match="diagnostic returned an error"):
        run_smoke_checks(
            frontend_url="https://frontend",
            api_url="https://api",
            include_diagnostic=True,
            diagnostic_token="probe-token",
            poster=_fake_poster(diagnostic_response, calls),
            fetcher=_fake_fetcher(_responses()),
        )


def test_run_smoke_checks_fails_when_bob_route_falls_back_to_homeops():
    responses = _responses()
    responses["https://frontend/bob/evals/"] = SmokeResponse(
        200,
        b'<title>HomeOps</title><div id="root"></div>',
        "https://frontend/bob/evals/",
    )

    with pytest.raises(SmokeCheckError, match="Bob evaluation route missing markers"):
        run_smoke_checks(
            frontend_url="https://frontend",
            api_url="https://api",
            include_bob_evaluation=True,
            fetcher=_fake_fetcher(responses),
        )


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
