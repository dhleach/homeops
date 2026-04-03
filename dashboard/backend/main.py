"""HomeOps Dashboard API — FastAPI backend."""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pi_reader import PiReader

app = FastAPI(
    title="HomeOps Dashboard API",
    version="0.1.0",
    description="Live HVAC data from the HomeOps Raspberry Pi.",
)

_cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

reader = PiReader(
    host=os.getenv("PI_HOST", "100.115.21.72"),
    user=os.getenv("PI_SSH_USER", "bob"),
    key_path=os.getenv("PI_SSH_KEY_PATH", "/app/keys/id_ed25519"),
    events_path=os.getenv(
        "PI_EVENTS_PATH",
        "/home/leachd/repos/homeops/state/consumer/events.jsonl",
    ),
    cache_ttl=int(os.getenv("CACHE_TTL_SECONDS", "30")),
)


@app.get("/health")
def health() -> dict:
    """Liveness probe — always returns 200 if the process is up."""
    return {"status": "ok"}


@app.get("/api/current-temps")
def current_temps() -> dict:
    """Return the latest temperature readings for all zones and outdoor sensor."""
    try:
        data = reader.get_temps()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to read from Pi: {exc}",
        ) from exc

    return {
        "zones": {
            zone: {
                "zone": r.zone,
                "current_temp_f": r.current_temp_f,
                "setpoint_f": r.setpoint_f,
                "hvac_mode": r.hvac_mode,
                "hvac_action": r.hvac_action,
                "last_updated": r.last_updated,
            }
            for zone, r in data.zones.items()
        },
        "outdoor_temp_f": data.outdoor_temp_f,
        "outdoor_last_updated": data.outdoor_last_updated,
        "fetched_at": data.fetched_at,
    }
