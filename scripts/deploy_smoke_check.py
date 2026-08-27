#!/usr/bin/env python3
"""Verify the public HomeOps deployment surfaces after a release.

Revision history:
  2026-08-18  Added dependency-free frontend, API, Grafana, and Prometheus smoke checks
              so deployment workflows fail closed when the public system is unhealthy.
  2026-08-18  Required thermostat setpoint fields in the telemetry contract so a
              partially populated API response cannot pass the release gate.
  2026-08-21  Validate that the deployed OpenAPI contract includes the authenticated
              diagnostic route and its safe auth/quota response set before release.
  2026-08-27  Added an opt-in Bob evaluation route check that validates the short
              CloudFront path and the published artifacts' offline, redacted,
              non-production boundary after the separate Terraform rollout.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_FRONTEND_URL = "https://homeops.now"
DEFAULT_API_URL = "https://api.homeops.now"
DEFAULT_BOB_EVALUATION_PATH = "/bob/evals/"
_MAX_BODY_BYTES = 1_000_000


class SmokeCheckError(RuntimeError):
    """Raised when a deployment smoke check fails."""


@dataclass(frozen=True)
class SmokeResponse:
    """Small, testable subset of an HTTP response."""

    status: int
    body: bytes
    url: str


Fetcher = Callable[[str, float], SmokeResponse]


def fetch_url(url: str, timeout: float) -> SmokeResponse:
    """Fetch a URL with a bounded response body and a descriptive failure."""
    request = Request(url, headers={"User-Agent": "homeops-deploy-smoke-check/1"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return SmokeResponse(
                status=response.status,
                body=response.read(_MAX_BODY_BYTES),
                url=response.geturl(),
            )
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace").strip()
        suffix = f": {detail[:200]}" if detail else ""
        raise SmokeCheckError(f"{url}: HTTP {exc.code}{suffix}") from exc
    except URLError as exc:
        raise SmokeCheckError(f"{url}: request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SmokeCheckError(f"{url}: request timed out") from exc


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _require_status(response: SmokeResponse, expected: int = 200) -> None:
    if response.status != expected:
        raise SmokeCheckError(f"{response.url}: expected HTTP {expected}, got {response.status}")


def _json(response: SmokeResponse) -> object:
    try:
        return json.loads(response.body)
    except json.JSONDecodeError as exc:
        raise SmokeCheckError(f"{response.url}: response was not valid JSON") from exc


def _check_frontend(base_url: str, fetcher: Fetcher, timeout: float) -> str:
    response = fetcher(_join(base_url, "/"), timeout)
    _require_status(response)
    body = response.body.decode("utf-8", errors="replace").lower()
    required_markers = ('id="root"', "homeops")
    missing = [marker for marker in required_markers if marker not in body]
    if missing:
        raise SmokeCheckError(f"{response.url}: frontend missing markers: {', '.join(missing)}")
    return "frontend: HTTP 200 and SPA shell present"


def _check_bob_evaluation(base_url: str, fetcher: Fetcher, timeout: float) -> str:
    """Verify the public Bob route and its safe evaluation artifact boundary."""
    page_response = fetcher(_join(base_url, DEFAULT_BOB_EVALUATION_PATH), timeout)
    _require_status(page_response)
    page = page_response.body.decode("utf-8", errors="replace").lower()
    required_markers = (
        "evaluation observability",
        'id="gate-status"',
        "optional live-model observations",
    )
    missing = [marker for marker in required_markers if marker not in page]
    if missing:
        raise SmokeCheckError(
            f"{page_response.url}: Bob evaluation route missing markers: {', '.join(missing)}"
        )

    report_response = fetcher(
        _join(base_url, f"{DEFAULT_BOB_EVALUATION_PATH}evaluation-report.v1.json"), timeout
    )
    _require_status(report_response)
    report = _json(report_response)
    if not isinstance(report, dict):
        raise SmokeCheckError(f"{report_response.url}: deterministic report is not an object")
    if report.get("schema_version") != "evaluation-report.v1":
        raise SmokeCheckError(f"{report_response.url}: unexpected deterministic report schema")
    if report.get("mode") != "deterministic" or report.get("status") != "passed":
        raise SmokeCheckError(
            f"{report_response.url}: deterministic report is not a passed release result"
        )
    report_execution = report.get("execution")
    if not isinstance(report_execution, dict):
        raise SmokeCheckError(f"{report_response.url}: deterministic execution metadata is missing")
    if (
        any(
            report_execution.get(field) is not False
            for field in ("network_enabled", "external_mutations_enabled", "credentials_loaded")
        )
        or report_execution.get("model_calls") != 0
    ):
        raise SmokeCheckError(
            f"{report_response.url}: deterministic report does not prove offline execution"
        )
    report_artifacts = report.get("artifacts")
    if (
        not isinstance(report_artifacts, dict)
        or report_artifacts.get("seeded_traces_published") is not False
    ):
        raise SmokeCheckError(f"{report_response.url}: seeded traces are not withheld")
    release_gate = report.get("release_gate")
    if not isinstance(release_gate, dict) or release_gate.get("status") != "passed":
        raise SmokeCheckError(f"{report_response.url}: release gate is not passed")

    live_response = fetcher(
        _join(base_url, f"{DEFAULT_BOB_EVALUATION_PATH}evaluation-live-trials.v1.json"), timeout
    )
    _require_status(live_response)
    live = _json(live_response)
    if not isinstance(live, dict):
        raise SmokeCheckError(f"{live_response.url}: live-trial fixture is not an object")
    if live.get("schema_version") != "evaluation-live-trials.v1" or live.get("mode") != "scripted":
        raise SmokeCheckError(
            f"{live_response.url}: live-trial artifact is not the scripted fixture"
        )
    live_execution = live.get("execution")
    if not isinstance(live_execution, dict) or any(
        live_execution.get(field) is not False
        for field in (
            "model_network_enabled",
            "tool_network_enabled",
            "external_mutations_enabled",
            "production_path_enabled",
        )
    ):
        raise SmokeCheckError(f"{live_response.url}: live-trial fixture is not sandbox-only")
    live_artifacts = live.get("artifacts")
    if not isinstance(live_artifacts, dict) or any(
        live_artifacts.get(field) is not False
        for field in ("raw_model_outputs_published", "raw_prompts_published")
    ):
        raise SmokeCheckError(f"{live_response.url}: raw live-trial material is not withheld")

    return "bob/evals: public route and redacted deterministic/live artifacts are safe"


def _check_api(base_url: str, fetcher: Fetcher, timeout: float) -> list[str]:
    health_response = fetcher(_join(base_url, "/health"), timeout)
    _require_status(health_response)
    health = _json(health_response)
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise SmokeCheckError(f"{health_response.url}: health response is not status=ok")

    openapi_response = fetcher(_join(base_url, "/openapi.json"), timeout)
    _require_status(openapi_response)
    openapi = _json(openapi_response)
    paths = openapi.get("paths", {}) if isinstance(openapi, dict) else {}
    required_paths = {"/health", "/api/current-temps", "/api/diagnostic"}
    if not required_paths.issubset(paths):
        missing = sorted(required_paths - set(paths))
        raise SmokeCheckError(
            f"{openapi_response.url}: OpenAPI missing paths: {', '.join(missing)}"
        )
    components = openapi.get("components", {}) if isinstance(openapi, dict) else {}
    security_schemes = components.get("securitySchemes", {}) if isinstance(components, dict) else {}
    diagnostic = paths.get("/api/diagnostic", {}) if isinstance(paths, dict) else {}
    post_diagnostic = diagnostic.get("post", {}) if isinstance(diagnostic, dict) else {}
    response_codes = (
        set(post_diagnostic.get("responses", {})) if isinstance(post_diagnostic, dict) else set()
    )
    if not isinstance(security_schemes, dict) or not security_schemes:
        raise SmokeCheckError(f"{openapi_response.url}: OpenAPI missing security schemes")
    if not isinstance(post_diagnostic, dict) or not post_diagnostic.get("security"):
        raise SmokeCheckError(f"{openapi_response.url}: diagnostic route is not authenticated")
    if not {"401", "403", "429", "503"}.issubset(response_codes):
        raise SmokeCheckError(
            f"{openapi_response.url}: diagnostic route missing auth/quota responses"
        )

    telemetry_response = fetcher(_join(base_url, "/api/current-temps"), timeout)
    _require_status(telemetry_response)
    telemetry = _json(telemetry_response)
    required_fields = {
        "floor_1",
        "floor_2",
        "floor_3",
        "outdoor",
        "furnace_active",
        "floor_1_call",
        "floor_2_call",
        "floor_3_call",
        "floor_1_setpoint",
        "floor_2_setpoint",
        "floor_3_setpoint",
        "last_updated",
        "error",
    }
    if not isinstance(telemetry, dict):
        raise SmokeCheckError(f"{telemetry_response.url}: telemetry response is not an object")
    missing = sorted(required_fields - set(telemetry))
    if missing:
        raise SmokeCheckError(
            f"{telemetry_response.url}: telemetry missing fields: {', '.join(missing)}"
        )
    if telemetry.get("error") is not None:
        raise SmokeCheckError(f"{telemetry_response.url}: telemetry reports an error")

    return [
        "api: health endpoint is ok",
        "api: OpenAPI exposes health and current telemetry",
        "api: live telemetry schema is available without a backend error",
    ]


def _check_observability(base_url: str, fetcher: Fetcher, timeout: float) -> list[str]:
    grafana_response = fetcher(_join(base_url, "/grafana/api/health"), timeout)
    _require_status(grafana_response)
    grafana = _json(grafana_response)
    if not isinstance(grafana, dict) or grafana.get("database") != "ok":
        raise SmokeCheckError(f"{grafana_response.url}: Grafana database is not healthy")

    prometheus_response = fetcher(_join(base_url, "/prometheus/-/healthy"), timeout)
    _require_status(prometheus_response)
    if b"healthy" not in prometheus_response.body.lower():
        raise SmokeCheckError(f"{prometheus_response.url}: Prometheus did not report healthy")

    return [
        "grafana: database health is ok",
        "prometheus: readiness endpoint reports healthy",
    ]


def run_smoke_checks(
    frontend_url: str = DEFAULT_FRONTEND_URL,
    api_url: str = DEFAULT_API_URL,
    timeout: float = 15.0,
    include_observability: bool = True,
    fetcher: Fetcher = fetch_url,
    include_bob_evaluation: bool = False,
) -> list[str]:
    """Run all release checks in sequence and return human-readable results."""
    results = [_check_frontend(frontend_url, fetcher, timeout)]
    if include_bob_evaluation:
        results.append(_check_bob_evaluation(frontend_url, fetcher, timeout))
    results.extend(_check_api(api_url, fetcher, timeout))
    if include_observability:
        results.extend(_check_observability(api_url, fetcher, timeout))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default=DEFAULT_FRONTEND_URL)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--skip-observability",
        action="store_true",
        help="Only check the frontend and API; useful before Grafana is provisioned.",
    )
    parser.add_argument(
        "--check-bob-evaluation",
        action="store_true",
        help="Also verify the deployed /bob/evals/ route and redacted artifacts.",
    )
    args = parser.parse_args(argv)

    try:
        results = run_smoke_checks(
            frontend_url=args.frontend_url,
            api_url=args.api_url,
            timeout=args.timeout,
            include_observability=not args.skip_observability,
            include_bob_evaluation=args.check_bob_evaluation,
        )
    except SmokeCheckError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    for result in results:
        print(f"PASS: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
