"""Tests for the FastAPI endpoints in main.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from main import app, reader
from pi_reader import TempsData, ThermostatReading

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_TEMPS = TempsData(
    zones={
        "floor_1": ThermostatReading(
            zone="floor_1",
            entity_id="climate.floor_1_thermostat",
            current_temp_f=70,
            setpoint_f=68,
            hvac_mode="heat",
            hvac_action="idle",
            last_updated="2026-04-02T20:00:00Z",
        ),
        "floor_2": ThermostatReading(
            zone="floor_2",
            entity_id="climate.floor_2_thermostat",
            current_temp_f=71,
            setpoint_f=68,
            hvac_mode="heat",
            hvac_action="heating",
            last_updated="2026-04-02T20:01:00Z",
        ),
        "floor_3": ThermostatReading(
            zone="floor_3",
            entity_id="climate.floor_3_thermostat",
            current_temp_f=76,
            setpoint_f=68,
            hvac_mode="heat",
            hvac_action="idle",
            last_updated="2026-04-02T20:02:00Z",
        ),
    },
    outdoor_temp_f=52.5,
    outdoor_last_updated="2026-04-02T19:59:00Z",
    fetched_at=1743638400.0,
)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def mock_reader():
    with patch.object(reader, "get_temps", return_value=FAKE_TEMPS) as m:
        yield m


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_response_body(client):
    resp = client.get("/health")
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /api/current-temps — happy path
# ---------------------------------------------------------------------------


def test_current_temps_returns_200(client, mock_reader):
    resp = client.get("/api/current-temps")
    assert resp.status_code == 200


def test_current_temps_has_zones(client, mock_reader):
    body = client.get("/api/current-temps").json()
    assert "zones" in body
    assert set(body["zones"].keys()) == {"floor_1", "floor_2", "floor_3"}


def test_current_temps_zone_shape(client, mock_reader):
    body = client.get("/api/current-temps").json()
    f1 = body["zones"]["floor_1"]
    assert f1["zone"] == "floor_1"
    assert f1["current_temp_f"] == 70
    assert f1["setpoint_f"] == 68
    assert f1["hvac_mode"] == "heat"
    assert f1["hvac_action"] == "idle"
    assert "last_updated" in f1


def test_current_temps_outdoor_temp(client, mock_reader):
    body = client.get("/api/current-temps").json()
    assert body["outdoor_temp_f"] == pytest.approx(52.5)
    assert body["outdoor_last_updated"] == "2026-04-02T19:59:00Z"


def test_current_temps_fetched_at(client, mock_reader):
    body = client.get("/api/current-temps").json()
    assert body["fetched_at"] == pytest.approx(1743638400.0)


def test_current_temps_calls_reader(client, mock_reader):
    client.get("/api/current-temps")
    mock_reader.assert_called_once()


# ---------------------------------------------------------------------------
# /api/current-temps — error handling
# ---------------------------------------------------------------------------


def test_current_temps_503_on_ssh_error(client):
    with patch.object(reader, "get_temps", side_effect=ConnectionError("timeout")):
        resp = client.get("/api/current-temps")
    assert resp.status_code == 503
    assert "Failed to read from Pi" in resp.json()["detail"]


def test_current_temps_503_on_generic_error(client):
    with patch.object(reader, "get_temps", side_effect=RuntimeError("unexpected")):
        resp = client.get("/api/current-temps")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /api/current-temps — partial data
# ---------------------------------------------------------------------------


def test_current_temps_null_outdoor(client):
    partial = TempsData(
        zones=FAKE_TEMPS.zones,
        outdoor_temp_f=None,
        outdoor_last_updated=None,
        fetched_at=1743638400.0,
    )
    with patch.object(reader, "get_temps", return_value=partial):
        body = client.get("/api/current-temps").json()
    assert body["outdoor_temp_f"] is None
    assert body["outdoor_last_updated"] is None


def test_current_temps_empty_zones(client):
    empty = TempsData(
        zones={},
        outdoor_temp_f=74.0,
        outdoor_last_updated="2026-04-02T20:00:00Z",
        fetched_at=1743638400.0,
    )
    with patch.object(reader, "get_temps", return_value=empty):
        body = client.get("/api/current-temps").json()
    assert body["zones"] == {}
    assert body["outdoor_temp_f"] == pytest.approx(74.0)
