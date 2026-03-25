"""Tests for FloorNoResponseRule."""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "insights"))

from rules.floor_no_response import FloorNoResponseRule

BASE_TS = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)


class TestFloorNoResponseRule:
    def test_zone_calling_over_threshold_emits_finding(self):
        """Zone calls for 11 minutes with no furnace response → finding emitted."""
        rule = FloorNoResponseRule(threshold_s=600)
        rule.on_floor_call_started("floor_1", BASE_TS)

        now = BASE_TS + timedelta(minutes=11)
        findings = rule.check(now)

        assert len(findings) == 1
        f = findings[0]
        assert f["zone"] == "floor_1"
        assert f["call_start_time"] == BASE_TS.isoformat()
        assert f["minutes_elapsed"] == round(11.0, 2)
        assert f["severity"] == "high"

    def test_furnace_response_before_threshold_clears_zone(self):
        """Zone calls for 11 min but furnace responded at 5 min → no finding."""
        rule = FloorNoResponseRule(threshold_s=600)
        rule.on_floor_call_started("floor_1", BASE_TS)

        # Furnace turns on at +5 min
        rule.on_heating_session_started()

        now = BASE_TS + timedelta(minutes=11)
        findings = rule.check(now)

        assert findings == []

    def test_zone_under_threshold_no_finding(self):
        """Zone has been calling for only 9 minutes → no finding."""
        rule = FloorNoResponseRule(threshold_s=600)
        rule.on_floor_call_started("floor_1", BASE_TS)

        now = BASE_TS + timedelta(minutes=9)
        findings = rule.check(now)

        assert findings == []

    def test_two_zones_calling_furnace_responds_clears_both(self):
        """Two zones calling; furnace turns on → both cleared, no findings."""
        rule = FloorNoResponseRule(threshold_s=600)
        rule.on_floor_call_started("floor_1", BASE_TS)
        rule.on_floor_call_started("floor_2", BASE_TS)

        rule.on_heating_session_started()

        now = BASE_TS + timedelta(minutes=15)
        findings = rule.check(now)

        assert findings == []

    def test_floor_call_ended_clears_zone(self):
        """Zone call ends before threshold → no finding."""
        rule = FloorNoResponseRule(threshold_s=600)
        rule.on_floor_call_started("floor_1", BASE_TS)
        rule.on_floor_call_ended("floor_1")

        now = BASE_TS + timedelta(minutes=15)
        findings = rule.check(now)

        assert findings == []

    def test_finding_includes_correct_minutes_elapsed(self):
        """minutes_elapsed reflects actual elapsed time."""
        rule = FloorNoResponseRule(threshold_s=600)
        rule.on_floor_call_started("floor_2", BASE_TS)

        now = BASE_TS + timedelta(seconds=750)  # 12.5 minutes
        findings = rule.check(now)

        assert len(findings) == 1
        assert findings[0]["minutes_elapsed"] == round(750 / 60.0, 2)

    def test_no_zones_calling_no_findings(self):
        """No zones calling → empty findings."""
        rule = FloorNoResponseRule(threshold_s=600)
        findings = rule.check(BASE_TS + timedelta(minutes=30))
        assert findings == []

    def test_custom_threshold(self):
        """Custom threshold of 5 minutes is respected."""
        rule = FloorNoResponseRule(threshold_s=300)
        rule.on_floor_call_started("floor_3", BASE_TS)

        # At 4 min 59s — just under threshold
        assert rule.check(BASE_TS + timedelta(seconds=299)) == []

        # At 5 min exactly — at threshold
        findings = rule.check(BASE_TS + timedelta(seconds=300))
        assert len(findings) == 1
        assert findings[0]["zone"] == "floor_3"
