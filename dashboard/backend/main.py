"""HomeOps Dashboard API — FastAPI backend.

Queries EC2-local Prometheus for live HVAC telemetry and exposes it
at GET /api/current-temps.  Returns nulls (not 500) when Prometheus
is unreachable so the frontend can show a degraded-mode UI.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"

FLOORS = ["floor_1", "floor_2", "floor_3"]

SYSTEM_PROMPT = (
    "You are an HVAC diagnostic assistant for a real home monitoring system in Pittsburgh, PA. "
    "You are given live data from a 3-zone HVAC system. Floor 2 has only 3 vents and is prone to "
    "overheating the furnace if it runs too long. Be specific about numbers, flag anything unusual,"
    " and keep your response under 200 words. Write for a technically curious homeowner."
)

app = FastAPI(
    title="HomeOps Dashboard API",
    version="0.1.0",
    description="Live HVAC data served from EC2-local Prometheus.",
)

# CORS is handled entirely by Nginx (api.homeops.now.conf).
# Do NOT add FastAPI CORSMiddleware here — duplicate Access-Control-Allow-Origin
# headers cause Safari/iOS to reject the response with "Load failed".


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CurrentTempsResponse(BaseModel):
    """Live HVAC telemetry snapshot.

    All temperature fields are in °F. Boolean call/furnace fields indicate
    whether that zone is actively calling for heat. ``null`` values mean
    the metric was not yet available in Prometheus (e.g. sensor offline or
    consumer just restarted).
    """

    floor_1: float | None = Field(None, description="Floor 1 current temperature (°F)")
    floor_2: float | None = Field(None, description="Floor 2 current temperature (°F)")
    floor_3: float | None = Field(None, description="Floor 3 current temperature (°F)")
    outdoor: float | None = Field(None, description="Outdoor current temperature (°F)")

    furnace_active: bool | None = Field(None, description="True when furnace is heating")

    floor_1_call: bool | None = Field(None, description="True when floor 1 is calling for heat")
    floor_2_call: bool | None = Field(None, description="True when floor 2 is calling for heat")
    floor_3_call: bool | None = Field(None, description="True when floor 3 is calling for heat")

    floor_1_setpoint: float | None = Field(None, description="Floor 1 thermostat setpoint (°F)")
    floor_2_setpoint: float | None = Field(None, description="Floor 2 thermostat setpoint (°F)")
    floor_3_setpoint: float | None = Field(None, description="Floor 3 thermostat setpoint (°F)")

    last_updated: str = Field(..., description="ISO-8601 UTC timestamp of this snapshot")
    error: str | None = Field(None, description="Set when Prometheus was unreachable")


_DEFAULT_QUESTION = (
    "Analyze the current HVAC behavior and flag anything unusual or worth"
    " the homeowner's attention."
)


class DiagnosticRequest(BaseModel):
    question: str = _DEFAULT_QUESTION


class DiagnosticResponse(BaseModel):
    answer: str
    context_chars: int
    error: str | None = None


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


def _fmt_runtime(seconds: float | None) -> str:
    """Format runtime seconds into a human-readable string like '1h 12m' or '24m 30s'."""
    if seconds is None:
        return "\u2014"
    secs = int(seconds)
    hours = secs // 3600
    minutes = (secs % 3600) // 60
    remaining_secs = secs % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {remaining_secs}s"


async def _build_hvac_context() -> str:
    """Build a structured plain-text context string from live Prometheus data."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        async with httpx.AsyncClient() as client:
            floor_temps: dict[str, float | None] = {}
            for floor in FLOORS:
                result = await _query(client, f'floor_temperature_fahrenheit{{floor="{floor}"}}')
                floor_temps[floor] = _first_value(result)

            outdoor_result = await _query(client, "outdoor_temperature_fahrenheit")
            outdoor = _first_value(outdoor_result)

            furnace_result = await _query(client, "furnace_heating_active")
            furnace_raw = _first_value(furnace_result)
            furnace_active = bool(furnace_raw) if furnace_raw is not None else None

            floor_calls: dict[str, bool | None] = {}
            for floor in FLOORS:
                result = await _query(client, f'floor_call_active{{floor="{floor}"}}')
                raw = _first_value(result)
                floor_calls[floor] = bool(raw) if raw is not None else None

            floor_runtimes: dict[str, float | None] = {}
            for floor in FLOORS:
                result = await _query(client, f'zone_runtime_today_seconds{{floor="{floor}"}}')
                floor_runtimes[floor] = _first_value(result)

            floor_setpoints: dict[str, float | None] = {}
            for floor in FLOORS:
                result = await _query(client, f'floor_setpoint_fahrenheit{{floor="{floor}"}}')
                floor_setpoints[floor] = _first_value(result)

        prometheus_note = ""
    except Exception as exc:  # noqa: BLE001
        floor_temps = {f: None for f in FLOORS}
        outdoor = None
        furnace_active = None
        floor_calls = {f: None for f in FLOORS}
        floor_runtimes = {f: None for f in FLOORS}
        floor_setpoints = {f: None for f in FLOORS}
        prometheus_note = f"\nNote: Prometheus unreachable — data unavailable ({exc})\n"

    def _temp_str(floor: str) -> str:
        t = floor_temps.get(floor)
        sp = floor_setpoints.get(floor)
        call = floor_calls.get(floor)
        t_str = f"{t:.0f}\u00b0F" if t is not None else "\u2014"
        sp_str = f"{sp:.0f}\u00b0F" if sp is not None else "\u2014"
        state = "calling for heat" if call else "idle"
        return f"{t_str} (setpoint: {sp_str}) \u2014 {state}"

    if furnace_active is None:
        furnace_str = "\u2014"
    else:
        furnace_str = "ACTIVE" if furnace_active else "OFF"
    outdoor_str = f"{outdoor:.0f}\u00b0F" if outdoor is not None else "\u2014"

    lines = [
        "=== HomeOps HVAC Snapshot ===",
        f"Timestamp: {ts}",
        prometheus_note,
        "CURRENT CONDITIONS",
        f"  Floor 1: {_temp_str('floor_1')}",
        f"  Floor 2: {_temp_str('floor_2')}",
        f"  Floor 3: {_temp_str('floor_3')}",
        f"  Outdoor: {outdoor_str}",
        f"  Furnace: {furnace_str}",
        "",
        "TODAY'S ZONE RUNTIMES",
        f"  Floor 1: {_fmt_runtime(floor_runtimes.get('floor_1'))}",
        f"  Floor 2: {_fmt_runtime(floor_runtimes.get('floor_2'))}",
        f"  Floor 3: {_fmt_runtime(floor_runtimes.get('floor_3'))}",
    ]
    return "\n".join(lines)


async def _call_gemini(context: str, question: str, api_key: str) -> str:
    """Call Gemini REST API and return the response text."""
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": f"HVAC DATA:\n{context}\n\nQUESTION: {question}"}]}],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, params={"key": api_key}, timeout=30.0)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/diagnostic", response_model=DiagnosticResponse)
async def diagnostic(request: DiagnosticRequest) -> DiagnosticResponse:
    """Ask an AI question about the current HVAC state using live Prometheus data."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return DiagnosticResponse(answer="", context_chars=0, error="GEMINI_API_KEY not configured")

    context = await _build_hvac_context()

    try:
        answer = await _call_gemini(context, request.question, api_key)
    except Exception as exc:  # noqa: BLE001
        return DiagnosticResponse(answer="", context_chars=len(context), error=str(exc))

    return DiagnosticResponse(answer=answer, context_chars=len(context))


@app.get("/health")
def health() -> dict:
    """Liveness probe — always 200 if the process is up."""
    return {"status": "ok"}


@app.get("/api/current-temps", response_model=CurrentTempsResponse)
async def current_temps() -> CurrentTempsResponse:
    """Return live HVAC temps and call/furnace state from Prometheus.

    All numeric fields are floats (°F); boolean fields indicate active
    heating state. Returns null values + an ``error`` field when
    Prometheus is unreachable.
    """
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

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

            # Per-floor setpoints
            floor_setpoints: dict[str, float | None] = {}
            for floor in FLOORS:
                result = await _query(
                    client,
                    f'floor_setpoint_fahrenheit{{floor="{floor}"}}',
                )
                floor_setpoints[floor] = _first_value(result)

    except Exception as exc:  # noqa: BLE001
        return CurrentTempsResponse(
            floor_1=None,
            floor_2=None,
            floor_3=None,
            outdoor=None,
            furnace_active=None,
            floor_1_call=None,
            floor_2_call=None,
            floor_3_call=None,
            floor_1_setpoint=None,
            floor_2_setpoint=None,
            floor_3_setpoint=None,
            last_updated=ts,
            error=f"Prometheus unreachable: {exc}",
        )

    return CurrentTempsResponse(
        floor_1=floor_temps.get("floor_1"),
        floor_2=floor_temps.get("floor_2"),
        floor_3=floor_temps.get("floor_3"),
        outdoor=outdoor,
        furnace_active=furnace_active,
        floor_1_call=floor_calls.get("floor_1"),
        floor_2_call=floor_calls.get("floor_2"),
        floor_3_call=floor_calls.get("floor_3"),
        floor_1_setpoint=floor_setpoints.get("floor_1"),
        floor_2_setpoint=floor_setpoints.get("floor_2"),
        floor_3_setpoint=floor_setpoints.get("floor_3"),
        last_updated=ts,
    )
