"""Tests for the deterministic staged mitigation replay harness.

Revision history:
  2026-08-25  Add regression coverage for three simulated short-cycle attempts,
              HA event flow, rollback, alerting, and replay deduplication.
"""

from __future__ import annotations

from mitigation_e2e import run_scenario


def test_three_cycle_replay_rolls_back_and_alerts_once() -> None:
    report = run_scenario()

    assert [cycle["attempt"] for cycle in report["cycles"]] == [1, 2, 3]
    assert [cycle["rollback_triggered"] for cycle in report["cycles"]] == [False, False, True]
    assert all(cycle["zone_outcome"] == "applied" for cycle in report["cycles"])
    assert report["mitigation_enabled_after_rollback"] is False
    assert report["derived_event_count"] == 4
    assert report["telegram_alert_count"] == 1
    assert report["replay_emitted"] is False


def test_replay_uses_three_fresh_furnace_sessions_inside_one_storm_window() -> None:
    report = run_scenario()

    assert report["ha_event_count"] == 4
    assert report["observer_event_count"] == 4
    assert report["service_call_count"] == 6
