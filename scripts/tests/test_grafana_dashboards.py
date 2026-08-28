"""Tests for the provisioned Grafana dashboard definitions.

Revision history:
  2026-08-28  Add contract coverage for provisioned cooling state, runtime,
              session, and heat/cool-history panels while protecting the
              existing heating queries from accidental removal.
"""

import json
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "grafana" / "dashboards"
EXPECTED_DASHBOARDS = {
    "daily-summary.json",
    "floor-temperatures.json",
    "outdoor-correlation.json",
    "zone-runtimes.json",
}
COOLING_METRICS = {
    "ac_cooling_active",
    "cooling_floor_call_active",
    "cooling_zone_runtime_today_seconds",
    "cooling_zone_call_count_today",
    "cooling_session_duration_seconds",
    "cooling_runtime_today_seconds",
    "cooling_session_count_today",
}
DAILY_COOLING_METRICS = COOLING_METRICS - {
    "ac_cooling_active",
    "cooling_floor_call_active",
}
HEATING_METRICS = {
    "furnace_heating_active",
    "floor_call_active",
    "zone_runtime_today_seconds",
    "heating_session_duration_seconds",
}
FLOOR_SELECTOR = 'floor=~"floor_[123]"'


def _load_dashboard(filename: str) -> dict:
    return json.loads((DASHBOARD_DIR / filename).read_text(encoding="utf-8"))


def _panels(dashboard: dict) -> list[dict]:
    """Flatten panels, including any future row/group nesting."""
    result: list[dict] = []
    for panel in dashboard.get("panels", []):
        result.append(panel)
        result.extend(_panels(panel))
    return result


def _expressions(panel: dict) -> list[str]:
    return [target.get("expr", "") for target in panel.get("targets", [])]


def _panel_text(panel: dict) -> str:
    legends = " ".join(target.get("legendFormat", "") for target in panel.get("targets", []))
    return " ".join(
        (
            panel.get("title", ""),
            panel.get("description", ""),
            legends,
        )
    ).lower()


def test_provisioned_dashboard_json_is_valid_and_panel_ids_are_unique() -> None:
    actual = {path.name for path in DASHBOARD_DIR.glob("*.json")}
    assert actual == EXPECTED_DASHBOARDS

    for filename in sorted(actual):
        dashboard = _load_dashboard(filename)
        assert dashboard["uid"].startswith("homeops-")
        assert dashboard["panels"]
        panel_ids = [panel["id"] for panel in _panels(dashboard)]
        assert len(panel_ids) == len(set(panel_ids)), filename
        for panel in _panels(dashboard):
            assert panel["datasource"]["type"] == "prometheus"
            assert panel["gridPos"]["x"] >= 0
            assert panel["gridPos"]["w"] > 0
            assert panel["gridPos"]["x"] + panel["gridPos"]["w"] <= 24


def test_zone_dashboard_covers_live_inferred_ac_and_per_floor_cooling_activity() -> None:
    dashboard = _load_dashboard("zone-runtimes.json")
    panels = {panel["title"]: panel for panel in _panels(dashboard)}

    ac_panel = panels["Inferred AC Status"]
    zone_status = panels["Cooling Zone Call Status"]
    activity = panels["Cooling Call + Inferred AC Activity (time series)"]
    runtime = panels["Cooling Runtime Today (seconds, cumulative)"]

    assert any("ac_cooling_active" in expression for expression in _expressions(ac_panel))
    assert all(FLOOR_SELECTOR in expression for expression in _expressions(zone_status))
    assert any(FLOOR_SELECTOR in expression for expression in _expressions(activity))
    assert any(
        "cooling_zone_runtime_today_seconds" in expression for expression in _expressions(runtime)
    )


def test_daily_dashboard_covers_cooling_runtime_sessions_and_counts() -> None:
    dashboard = _load_dashboard("daily-summary.json")
    panels = _panels(dashboard)
    expressions = {expression for panel in panels for expression in _expressions(panel)}

    assert all(
        any(metric in expression for expression in expressions) for metric in DAILY_COOLING_METRICS
    )
    titles = {panel["title"] for panel in panels}
    assert {
        "Inferred AC Runtime Today",
        "Cooling Sessions Today",
        "Last Cooling Session Duration",
        "Cooling Runtime Today by Zone (cumulative)",
        "Heating vs Cooling Session Duration History",
        "Cooling Call Count Today by Zone",
    } <= titles


def test_cooling_history_is_explicitly_labeled_as_inferred_thermostat_demand() -> None:
    for filename in ("zone-runtimes.json", "daily-summary.json"):
        dashboard = _load_dashboard(filename)
        assert "inferred" in dashboard["description"].lower()
        assert "not compressor" in dashboard["description"].lower()
        cooling_panels = [
            panel
            for panel in _panels(dashboard)
            if "cooling" in panel.get("title", "").lower()
            or panel.get("title", "").lower().startswith("inferred ac")
        ]
        assert cooling_panels
        for panel in cooling_panels:
            text = _panel_text(panel)
            assert "inferred" in text, panel["title"]
            assert "not compressor" in text, panel["title"]


def test_existing_heating_queries_remain_provisioned() -> None:
    expressions = {
        expression
        for filename in ("zone-runtimes.json", "daily-summary.json")
        for panel in _panels(_load_dashboard(filename))
        for expression in _expressions(panel)
    }

    for metric in HEATING_METRICS:
        assert any(metric in expression for expression in expressions), metric
