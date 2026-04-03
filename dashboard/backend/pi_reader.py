"""Read live HVAC event data from the HomeOps Pi over SSH."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field


@dataclass
class ThermostatReading:
    zone: str
    entity_id: str
    current_temp_f: float
    setpoint_f: float
    hvac_mode: str
    hvac_action: str
    last_updated: str


@dataclass
class TempsData:
    zones: dict[str, ThermostatReading]
    outdoor_temp_f: float | None
    outdoor_last_updated: str | None
    fetched_at: float = field(default_factory=time.time)


def _parse_events(raw: str) -> TempsData:
    """Parse events.jsonl content into TempsData (last value per zone wins)."""
    zones: dict[str, ThermostatReading] = {}
    outdoor_temp_f: float | None = None
    outdoor_last_updated: str | None = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        schema = event.get("schema", "")
        d = event.get("data", {})
        if not isinstance(d, dict):
            continue

        if "thermostat" in schema:
            zone = d.get("zone", "")
            if zone:
                zones[zone] = ThermostatReading(
                    zone=zone,
                    entity_id=d.get("entity_id", ""),
                    current_temp_f=d.get("current_temp"),
                    setpoint_f=d.get("setpoint"),
                    hvac_mode=d.get("hvac_mode", ""),
                    hvac_action=d.get("hvac_action", ""),
                    last_updated=d.get("ts", ""),
                )
        elif schema == "homeops.consumer.outdoor_temp_updated.v1":
            outdoor_temp_f = d.get("temperature_f")
            outdoor_last_updated = d.get("timestamp")

    return TempsData(
        zones=zones,
        outdoor_temp_f=outdoor_temp_f,
        outdoor_last_updated=outdoor_last_updated,
    )


class PiReader:
    """SSH into the HomeOps Pi, tail events.jsonl, and return the latest temps.

    Results are cached for *cache_ttl* seconds to avoid hammering the Pi on
    every HTTP request.
    """

    def __init__(
        self,
        host: str,
        user: str,
        key_path: str,
        events_path: str,
        cache_ttl: int = 30,
        tail_lines: int = 2000,
    ) -> None:
        self.host = host
        self.user = user
        self.key_path = key_path
        self.events_path = events_path
        self.cache_ttl = cache_ttl
        self.tail_lines = tail_lines
        self._cache: TempsData | None = None
        self._lock = threading.Lock()

    def _fetch_from_pi(self) -> TempsData:
        import paramiko  # deferred so tests can patch before import

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                self.host,
                username=self.user,
                key_filename=self.key_path,
                timeout=10,
            )
            _, stdout, _ = ssh.exec_command(f"tail -n {self.tail_lines} {self.events_path}")
            raw = stdout.read().decode()
        finally:
            ssh.close()

        return _parse_events(raw)

    def get_temps(self) -> TempsData:
        """Return cached data if fresh; otherwise fetch from Pi."""
        with self._lock:
            if self._cache is not None and (time.time() - self._cache.fetched_at) < self.cache_ttl:
                return self._cache
            data = self._fetch_from_pi()
            self._cache = data
            return data
