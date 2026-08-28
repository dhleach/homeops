"""Tests for the FastAPI backend.

Revision history:
  2026-08-28  Added mode-aware cooling API, mixed heat/cool, unavailable-data,
              and OpenAPI contract coverage for the current-telemetry route.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from main import app

EXPECTED_KEYS = {
    "floor_1",
    "floor_2",
    "floor_3",
    "outdoor",
    "furnace_active",
    "floor_1_call",
    "floor_2_call",
    "floor_3_call",
    "ac_cooling_active",
    "floor_1_cooling_call",
    "floor_2_cooling_call",
    "floor_3_cooling_call",
    "floor_1_hvac_action",
    "floor_2_hvac_action",
    "floor_3_hvac_action",
    "floor_1_setpoint",
    "floor_2_setpoint",
    "floor_3_setpoint",
    "last_updated",
}

client = TestClient(app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_200():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /api/current-temps — Prometheus mocked with respx
# ---------------------------------------------------------------------------


def _prom_response(value: float | int) -> dict:
    """Build a minimal Prometheus instant-query response."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1712163600, str(value)]}],
        },
    }


def _prom_empty() -> dict:
    """Build a Prometheus response with an empty result set."""
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }


@pytest.mark.respx(base_url="http://localhost:9090")
def test_current_temps_returns_200_with_expected_keys(respx_mock):
    """When Prometheus answers every query, all expected keys should be present."""
    # Wire every GET to /api/v1/query to return sensible values.
    respx_mock.get("/api/v1/query").mock(side_effect=_prom_side_effect)

    resp = client.get("/api/current-temps")
    assert resp.status_code == 200
    data = resp.json()
    assert EXPECTED_KEYS.issubset(data.keys()), f"Missing keys: {EXPECTED_KEYS - data.keys()}"


def _prom_side_effect(request: httpx.Request) -> httpx.Response:
    """Return mock Prometheus data keyed by the query param."""
    query = request.url.params.get("query", "")
    if "floor_temperature_fahrenheit" in query:
        if "floor_1" in query:
            return httpx.Response(200, json=_prom_response(68.5))
        if "floor_2" in query:
            return httpx.Response(200, json=_prom_response(71.2))
        if "floor_3" in query:
            return httpx.Response(200, json=_prom_response(69.0))
    if "outdoor_temperature_fahrenheit" in query:
        return httpx.Response(200, json=_prom_response(42.1))
    if "furnace_heating_active" in query:
        return httpx.Response(200, json=_prom_response(1))
    if "ac_cooling_active" in query:
        return httpx.Response(200, json=_prom_response(1))
    if "cooling_floor_call_active" in query:
        if "floor_1" in query:
            return httpx.Response(200, json=_prom_response(1))
        return httpx.Response(200, json=_prom_response(0))
    if "floor_call_active" in query:
        if "floor_2" in query:
            return httpx.Response(200, json=_prom_response(1))
        return httpx.Response(200, json=_prom_response(0))
    if "floor_setpoint_fahrenheit" in query:
        if "floor_1" in query:
            return httpx.Response(200, json=_prom_response(68))
        if "floor_2" in query:
            return httpx.Response(200, json=_prom_response(72))
        return httpx.Response(200, json=_prom_response(70))
    return httpx.Response(200, json=_prom_empty())


@pytest.mark.respx(base_url="http://localhost:9090")
def test_current_temps_values_match_mock(respx_mock):
    """Numeric values should match what the mock returns."""
    respx_mock.get("/api/v1/query").mock(side_effect=_prom_side_effect)

    resp = client.get("/api/current-temps")
    assert resp.status_code == 200
    data = resp.json()
    assert data["floor_1"] == pytest.approx(68.5)
    assert data["floor_2"] == pytest.approx(71.2)
    assert data["floor_3"] == pytest.approx(69.0)
    assert data["outdoor"] == pytest.approx(42.1)
    assert data["furnace_active"] is True
    assert data["floor_1_call"] is False
    assert data["floor_2_call"] is True
    assert data["floor_3_call"] is False
    assert data["ac_cooling_active"] is True
    assert data["floor_1_cooling_call"] is True
    assert data["floor_2_cooling_call"] is False
    assert data["floor_3_cooling_call"] is False
    assert data["floor_1_hvac_action"] == "cooling"
    assert data["floor_2_hvac_action"] == "heating"
    assert data["floor_3_hvac_action"] == "idle"
    assert data["floor_1_setpoint"] == pytest.approx(68)
    assert data["floor_2_setpoint"] == pytest.approx(72)
    assert data["floor_3_setpoint"] == pytest.approx(70)


@pytest.mark.parametrize(
    ("heating_call", "cooling_call", "expected"),
    [
        (True, False, "heating"),
        (False, True, "cooling"),
        (False, False, "idle"),
        (None, False, None),
        (True, True, None),
    ],
)
def test_derive_hvac_action_is_conservative(heating_call, cooling_call, expected):
    """Only complete, non-conflicting call pairs produce an active/idle action."""
    from main import _derive_hvac_action

    assert _derive_hvac_action(heating_call, cooling_call) == expected


def _prom_cooling_missing_side_effect(request: httpx.Request) -> httpx.Response:
    """Return the normal fixture except for absent cooling samples."""
    query = request.url.params.get("query", "")
    if "cooling_floor_call_active" in query or "ac_cooling_active" in query:
        return httpx.Response(200, json=_prom_empty())
    return _prom_side_effect(request)


@pytest.mark.respx(base_url="http://localhost:9090")
def test_current_temps_marks_cooling_action_unavailable_when_cooling_metrics_are_missing(
    respx_mock,
):
    """Missing cooling samples must not be mistaken for idle or heating."""
    respx_mock.get("/api/v1/query").mock(side_effect=_prom_cooling_missing_side_effect)

    resp = client.get("/api/current-temps")

    assert resp.status_code == 200
    data = resp.json()
    assert data["floor_1_call"] is False
    assert data["floor_1_cooling_call"] is None
    assert data["ac_cooling_active"] is None
    assert data["floor_1_hvac_action"] is None
    assert data["floor_2_hvac_action"] is None
    assert data["floor_3_hvac_action"] is None


def test_current_temps_openapi_documents_cooling_contract():
    """The generated API contract describes nullable cooling fields and actions."""
    properties = app.openapi()["components"]["schemas"]["CurrentTempsResponse"]["properties"]

    assert "AC demand" in properties["ac_cooling_active"]["description"]
    assert "cooling" in properties["floor_1_cooling_call"]["description"]
    action_schema = properties["floor_1_hvac_action"]
    assert {"heating", "cooling", "idle"} in [
        set(choice.get("enum", [])) for choice in action_schema.get("anyOf", [])
    ]


@pytest.mark.respx(base_url="http://localhost:9090")
def test_current_temps_returns_nulls_when_prometheus_unreachable(respx_mock):
    """When Prometheus is unreachable the endpoint should return nulls + error key."""
    respx_mock.get("/api/v1/query").mock(side_effect=httpx.ConnectError("connection refused"))

    resp = client.get("/api/current-temps")
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["floor_1"] is None
    assert data["floor_2"] is None
    assert data["floor_3"] is None
    assert data["outdoor"] is None
    assert data["furnace_active"] is None
    assert data["ac_cooling_active"] is None
    assert data["floor_1_cooling_call"] is None
    assert data["floor_2_cooling_call"] is None
    assert data["floor_3_cooling_call"] is None
    assert data["floor_1_hvac_action"] is None
    assert data["floor_2_hvac_action"] is None
    assert data["floor_3_hvac_action"] is None
