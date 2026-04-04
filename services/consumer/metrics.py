"""
Prometheus metrics server for the HomeOps consumer service.

Exposes live HVAC telemetry at GET /metrics (port 8001) in Prometheus exposition format.
Used as the data pipeline source for the homeops.now public dashboard:
  Pi consumer /metrics → Prometheus scrape → remote_write to EC2 → Grafana

Gauges
------
furnace_heating_active              1 if furnace currently on, 0 if idle
heating_session_duration_seconds    duration of most recent completed heating session
floor_temperature_fahrenheit        latest thermostat current temp per floor (label: floor)
outdoor_temperature_fahrenheit      latest outdoor temperature reading
floor_call_active                   1 if floor currently calling for heat (label: floor)
zone_runtime_today_seconds          accumulated floor heating runtime today in seconds
                                    (label: floor)
floor_runtime_anomaly_total         cumulative count of floor_runtime_anomaly.v1 events
                                    (label: floor)

Usage
-----
    from metrics import HvacMetrics
    metrics = HvacMetrics(port=8001)
    metrics.start()          # starts HTTP server in background thread
    metrics.set_furnace_active(True)
    metrics.set_outdoor_temp(42.5)
"""

from __future__ import annotations

import logging
import threading

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

logger = logging.getLogger(__name__)

_FLOORS = ["floor_1", "floor_2", "floor_3"]


class HvacMetrics:
    """Holds all Prometheus gauges and manages the metrics HTTP server."""

    def __init__(self, port: int = 8001, registry: CollectorRegistry | None = None) -> None:
        self._port = port
        self._started = False
        # Use a private registry so multiple instances (e.g. in tests) don't collide.
        # In production, pass registry=None to use a fresh isolated registry; the HTTP
        # server will serve metrics from this registry only.
        self._registry = registry if registry is not None else CollectorRegistry()

        self.furnace_heating_active = Gauge(
            "furnace_heating_active",
            "1 if the furnace is currently in a heating session, 0 if idle",
            registry=self._registry,
        )
        self.heating_session_duration_seconds = Gauge(
            "heating_session_duration_seconds",
            "Duration in seconds of the most recently completed heating session",
            registry=self._registry,
        )
        self.floor_temperature_fahrenheit = Gauge(
            "floor_temperature_fahrenheit",
            "Latest thermostat current temperature reading per floor (°F)",
            ["floor"],
            registry=self._registry,
        )
        self.outdoor_temperature_fahrenheit = Gauge(
            "outdoor_temperature_fahrenheit",
            "Latest outdoor temperature reading (°F)",
            registry=self._registry,
        )
        self.floor_call_active = Gauge(
            "floor_call_active",
            "1 if the floor is currently calling for heat, 0 otherwise",
            ["floor"],
            registry=self._registry,
        )
        self.zone_runtime_today_seconds = Gauge(
            "zone_runtime_today_seconds",
            "Accumulated floor heating call runtime today in seconds",
            ["floor"],
            registry=self._registry,
        )
        self.floor_runtime_anomaly_total = Counter(
            "floor_runtime_anomaly_total",
            "Cumulative count of floor_runtime_anomaly.v1 events emitted",
            ["floor"],
            registry=self._registry,
        )
        self.floor_setpoint_fahrenheit = Gauge(
            "floor_setpoint_fahrenheit",
            "Latest thermostat setpoint (target temperature) per floor (°F)",
            ["floor"],
            registry=self._registry,
        )

        # Initialise labelled gauges to 0 for all floors so Prometheus sees them immediately
        for floor in _FLOORS:
            self.floor_temperature_fahrenheit.labels(floor=floor).set(0)
            self.floor_call_active.labels(floor=floor).set(0)
            self.zone_runtime_today_seconds.labels(floor=floor).set(0)
            self.floor_setpoint_fahrenheit.labels(floor=floor).set(0)

    def start(self) -> None:
        """Start the Prometheus HTTP server in a daemon thread."""
        if self._started:
            return
        t = threading.Thread(
            target=start_http_server,
            args=(self._port,),
            kwargs={"registry": self._registry},
            daemon=True,
            name="prometheus-metrics-server",
        )
        t.start()
        self._started = True
        logger.info("Prometheus metrics server started on port %d", self._port)

    # ── Update helpers ────────────────────────────────────────────────────────

    def set_furnace_active(self, active: bool) -> None:
        self.furnace_heating_active.set(1 if active else 0)

    def set_heating_session_duration(self, duration_s: int | float | None) -> None:
        if duration_s is not None:
            self.heating_session_duration_seconds.set(float(duration_s))

    def set_floor_temperature(self, floor: str, temp_f: float) -> None:
        self.floor_temperature_fahrenheit.labels(floor=floor).set(temp_f)

    def set_outdoor_temperature(self, temp_f: float) -> None:
        self.outdoor_temperature_fahrenheit.set(temp_f)

    def set_floor_setpoint(self, floor: str, setpoint_f: float) -> None:
        self.floor_setpoint_fahrenheit.labels(floor=floor).set(setpoint_f)

    def set_floor_call_active(self, floor: str, active: bool) -> None:
        self.floor_call_active.labels(floor=floor).set(1 if active else 0)

    def add_floor_runtime(self, floor: str, duration_s: int | float) -> None:
        """Increment today's accumulated runtime for the given floor."""
        current = self.zone_runtime_today_seconds.labels(floor=floor)
        current.set(current._value.get() + float(duration_s))

    def reset_daily_runtimes(self) -> None:
        """Reset all zone_runtime_today_seconds to 0 at day rollover."""
        for floor in _FLOORS:
            self.zone_runtime_today_seconds.labels(floor=floor).set(0)

    def inc_floor_runtime_anomaly(self, floor: str) -> None:
        self.floor_runtime_anomaly_total.labels(floor=floor).inc()

    def update_from_event(self, schema: str, data: dict) -> None:
        """
        Dispatch a derived event dict to the appropriate gauge update.

        Called by consumer.py after each derived event is emitted.
        """
        if schema == "homeops.consumer.heating_session_started.v1":
            self.set_furnace_active(True)

        elif schema == "homeops.consumer.heating_session_ended.v1":
            self.set_furnace_active(False)
            self.set_heating_session_duration(data.get("duration_s"))

        elif schema == "homeops.consumer.thermostat_current_temp_updated.v1":
            # Event data uses "zone" and "current_temp" (not "floor"/"temperature_f")
            floor = data.get("zone", data.get("floor"))
            temp_f = data.get("current_temp", data.get("temperature_f"))
            if floor and temp_f is not None:
                self.set_floor_temperature(floor, float(temp_f))
            # Both current_temp and setpoint_changed events carry "setpoint" in common.
            setpoint = data.get("setpoint")
            if floor and setpoint is not None:
                self.set_floor_setpoint(floor, float(setpoint))

        elif schema == "homeops.consumer.thermostat_setpoint_changed.v1":
            floor = data.get("zone", data.get("floor"))
            setpoint = data.get("setpoint")
            if floor and setpoint is not None:
                self.set_floor_setpoint(floor, float(setpoint))

        elif schema == "homeops.consumer.outdoor_temp_updated.v1":
            temp_f = data.get("temperature_f")
            if temp_f is not None:
                self.set_outdoor_temperature(float(temp_f))

        elif schema == "homeops.consumer.floor_call_started.v1":
            floor = data.get("floor")
            if floor:
                self.set_floor_call_active(floor, True)

        elif schema == "homeops.consumer.floor_call_ended.v1":
            floor = data.get("floor")
            duration_s = data.get("duration_s")
            if floor:
                self.set_floor_call_active(floor, False)
                if duration_s is not None:
                    self.add_floor_runtime(floor, duration_s)

        elif schema == "homeops.consumer.floor_runtime_anomaly.v1":
            floor = data.get("floor")
            if floor:
                self.inc_floor_runtime_anomaly(floor)

        elif schema == "homeops.consumer.floor_daily_summary.v1":
            # At day rollover: sync zone_runtime_today_seconds from the authoritative summary
            floor = data.get("floor")
            total_runtime_s = data.get("total_runtime_s")
            if floor and total_runtime_s is not None:
                self.zone_runtime_today_seconds.labels(floor=floor).set(float(total_runtime_s))
