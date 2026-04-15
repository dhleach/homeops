"""Tests for POST /api/diagnostic and related helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from main import _build_hvac_context, app

client = TestClient(app)

# ---------------------------------------------------------------------------
# Prometheus mock helpers
# ---------------------------------------------------------------------------


def _prom_response(value: float | int) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1712163600, str(value)]}],
        },
    }


def _prom_empty() -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _prom_side_effect(request: httpx.Request) -> httpx.Response:
    """Return varied mock values for each PromQL metric."""
    query = request.url.params.get("query", "")
    if "floor_temperature_fahrenheit" in query:
        if "floor_1" in query:
            return httpx.Response(200, json=_prom_response(68.0))
        if "floor_2" in query:
            return httpx.Response(200, json=_prom_response(65.0))
        if "floor_3" in query:
            return httpx.Response(200, json=_prom_response(72.0))
    if "outdoor_temperature_fahrenheit" in query:
        return httpx.Response(200, json=_prom_response(42.0))
    if "furnace_heating_active" in query:
        return httpx.Response(200, json=_prom_response(1))
    if "floor_call_active" in query:
        if "floor_2" in query:
            return httpx.Response(200, json=_prom_response(1))
        return httpx.Response(200, json=_prom_response(0))
    if "zone_runtime_today_seconds" in query:
        if "floor_1" in query:
            return httpx.Response(200, json=_prom_response(1470))  # 24m 30s
        if "floor_2" in query:
            return httpx.Response(200, json=_prom_response(4320))  # 1h 12m
        if "floor_3" in query:
            return httpx.Response(200, json=_prom_response(480))  # 8m 0s
    if "floor_setpoint_fahrenheit" in query:
        if "floor_1" in query:
            return httpx.Response(200, json=_prom_response(70.0))
        if "floor_2" in query:
            return httpx.Response(200, json=_prom_response(70.0))
        if "floor_3" in query:
            return httpx.Response(200, json=_prom_response(68.0))
    return httpx.Response(200, json=_prom_empty())


# ---------------------------------------------------------------------------
# _build_hvac_context
# ---------------------------------------------------------------------------


@pytest.mark.respx(base_url="http://localhost:9090")
@pytest.mark.asyncio
async def test_build_hvac_context_returns_expected_sections(respx_mock):
    """Context string should contain the expected headers and floor data."""
    respx_mock.get("/api/v1/query").mock(side_effect=_prom_side_effect)

    context = await _build_hvac_context()

    assert "=== HomeOps HVAC Snapshot ===" in context
    assert "CURRENT CONDITIONS" in context
    assert "TODAY'S ZONE RUNTIMES" in context
    assert "Floor 1:" in context
    assert "Floor 2:" in context
    assert "Floor 3:" in context
    assert "Furnace:" in context
    assert "Outdoor:" in context
    assert "68°F" in context


@pytest.mark.respx(base_url="http://localhost:9090")
@pytest.mark.asyncio
async def test_build_hvac_context_handles_none_values_gracefully(respx_mock):
    """When Prometheus returns empty results, None fields should show — not crash."""
    respx_mock.get("/api/v1/query").mock(return_value=httpx.Response(200, json=_prom_empty()))

    context = await _build_hvac_context()

    assert "=== HomeOps HVAC Snapshot ===" in context
    assert "CURRENT CONDITIONS" in context
    assert "—" in context


# ---------------------------------------------------------------------------
# POST /api/diagnostic
# ---------------------------------------------------------------------------


@pytest.mark.respx(base_url="http://localhost:9090")
def test_diagnostic_returns_200_with_answer_when_gemini_responds(respx_mock, monkeypatch):
    """Happy path: Prometheus + Gemini both respond → answer field populated."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    respx_mock.get("/api/v1/query").mock(side_effect=_prom_side_effect)

    with patch("main._call_gemini", new=AsyncMock(return_value="Everything looks normal.")):
        resp = client.post("/api/diagnostic", json={"question": "Is my HVAC normal?"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "Everything looks normal."
    assert data["error"] is None
    assert data["context_chars"] > 0


def test_diagnostic_returns_error_when_api_key_not_set(monkeypatch):
    """If GEMINI_API_KEY is missing, return error without hitting Gemini."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    resp = client.post("/api/diagnostic", json={"question": "hello"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] == "GEMINI_API_KEY not configured"
    assert data["answer"] == ""
    assert data["context_chars"] == 0


@pytest.mark.respx(base_url="http://localhost:9090")
def test_diagnostic_returns_error_when_gemini_call_fails(respx_mock, monkeypatch):
    """If the Gemini call raises, return error field with exception message."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    respx_mock.get("/api/v1/query").mock(side_effect=_prom_side_effect)

    with patch(
        "main._call_gemini",
        new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    ):
        resp = client.post("/api/diagnostic", json={"question": "Are temperatures normal?"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] is not None
    assert "connection refused" in data["error"]
    assert data["answer"] == ""
    assert data["context_chars"] > 0


@pytest.mark.respx(base_url="http://localhost:9090")
def test_diagnostic_uses_default_question_when_none_provided(respx_mock, monkeypatch):
    """Omitting question field should use the default question and succeed."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
    respx_mock.get("/api/v1/query").mock(side_effect=_prom_side_effect)

    gemini_mock = AsyncMock(return_value="No anomalies detected.")
    with patch("main._call_gemini", new=gemini_mock) as mock_gemini:
        resp = client.post("/api/diagnostic", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "No anomalies detected."
    assert data["error"] is None
    # Verify the default question was passed
    call_args = mock_gemini.call_args
    assert "Analyze the current HVAC behavior" in call_args[0][1]
