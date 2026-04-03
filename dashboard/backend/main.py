"""HomeOps Dashboard API — FastAPI backend.

Queries EC2-local Prometheus for live HVAC telemetry and exposes it
at GET /api/current-temps.  Returns nulls (not 500) when Prometheus
is unreachable so the frontend can show a degraded-mode UI.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"

FLOORS = ["floor_1", "floor_2", "floor_3"]

app = FastAPI(
    title="HomeOps Dashboard API",
    version="0.1.0",
    description="Live HVAC data served from EC2-local Prometheus.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_value(result: list) -> float | None:
    """Return the float value from the first Prometheus instant-query result."""
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


async def _query(client: httpx.AsyncClient, promql: str) -> list:
    """Run a single PromQL instant query; return the result list (may be [])."""
    resp = await client.get(PROMETHEUS_URL, params={"query": promql}, timeout=5.0)
    resp.raise_for_status()
    return resp.json().get("data", {}).get("result", [])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Liveness probe — always 200 if the process is up."""
    return {"status": "ok"}


@app.get("/api/current-temps")
async def current_temps() -> dict:
    """Return live HVAC temps and call/furnace state from Prometheus.

    All numeric fields are floats; boolean fields are bools.
    Returns null values + an ``error`` field when Prometheus is unreachable.
    """
    try:
        async with httpx.AsyncClient() as client:
            # Floor temps
            floor_temps: dict[str, float | None] = {}
            for floor in FLOORS:
                result = await _query(
                    client,
                    f'floor_temperature_fahrenheit{{floor="{floor}"}}',
                )
                floor_temps[floor] = _first_value(result)

            # Outdoor temp
            outdoor_result = await _query(client, "outdoor_temperature_fahrenheit")
            outdoor = _first_value(outdoor_result)

            # Furnace active (1.0 == True)
            furnace_result = await _query(client, "furnace_heating_active")
            furnace_raw = _first_value(furnace_result)
            furnace_active = bool(furnace_raw) if furnace_raw is not None else None

            # Per-floor call active
            floor_calls: dict[str, bool | None] = {}
            for floor in FLOORS:
                result = await _query(
                    client,
                    f'floor_call_active{{floor="{floor}"}}',
                )
                raw = _first_value(result)
                floor_calls[floor] = bool(raw) if raw is not None else None

    except Exception as exc:  # noqa: BLE001
        return {
            "floor_1": None,
            "floor_2": None,
            "floor_3": None,
            "outdoor": None,
            "furnace_active": None,
            "floor_1_call": None,
            "floor_2_call": None,
            "floor_3_call": None,
            "last_updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": f"Prometheus unreachable: {exc}",
        }

    return {
        "floor_1": floor_temps.get("floor_1"),
        "floor_2": floor_temps.get("floor_2"),
        "floor_3": floor_temps.get("floor_3"),
        "outdoor": outdoor,
        "furnace_active": furnace_active,
        "floor_1_call": floor_calls.get("floor_1"),
        "floor_2_call": floor_calls.get("floor_2"),
        "floor_3_call": floor_calls.get("floor_3"),
        "last_updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
