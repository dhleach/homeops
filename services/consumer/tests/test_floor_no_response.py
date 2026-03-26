"""Tests for FloorNoResponseRule (temp-based non-response detection)."""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "insights"))

from rules.floor_no_response import FloorNoResponseRule

BASE_TS = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
START_TEMP = 68.0


class TestFloorNoResponseRule:
    def test_zone_over_threshold_no_temp_rise_emits_finding(self):
        """Zone calls for 11 min with no temp increase → finding emitted."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 600})
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=START_TEMP)

        now = BASE_TS + timedelta(minutes=11)
        findings = rule.check(now)

        assert len(findings) == 1
        f = findings[0]
        assert f["zone"] == "floor_1"
        assert f["call_start_time"] == BASE_TS.isoformat()
        assert f["minutes_elapsed"] == round(11.0, 2)
        assert f["start_temp"] == START_TEMP
        assert f["current_temp"] == START_TEMP
        assert f["severity"] == "high"

    def test_zone_over_threshold_temp_did_rise_no_finding(self):
        """Zone calls for 11 min but temp rose → furnace is working, no finding."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 600})
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=START_TEMP)
        rule.on_temp_updated("floor_1", START_TEMP + 1.0)

        now = BASE_TS + timedelta(minutes=11)
        findings = rule.check(now)

        assert findings == []

    def test_zone_under_threshold_no_finding(self):
        """Zone has been calling for only 9 min → no finding."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 600})
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=START_TEMP)

        now = BASE_TS + timedelta(minutes=9)
        findings = rule.check(now)

        assert findings == []

    def test_alert_fires_only_once_per_call_session(self):
        """check() called twice over threshold → finding emitted only once."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 600})
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=START_TEMP)

        # First check over threshold → finding
        findings1 = rule.check(BASE_TS + timedelta(minutes=11))
        assert len(findings1) == 1

        # Second check even later → no duplicate
        findings2 = rule.check(BASE_TS + timedelta(minutes=15))
        assert findings2 == []

    def test_floor_call_ended_clears_state(self):
        """Zone call ends → no finding, even if we pass the threshold later."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 600})
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=START_TEMP)
        rule.on_floor_call_ended("floor_1")

        now = BASE_TS + timedelta(minutes=15)
        findings = rule.check(now)

        assert findings == []

    def test_default_per_floor_thresholds(self):
        """Default thresholds: floor_1=600s, floor_2=900s, floor_3=360s."""
        rule = FloorNoResponseRule()

        # floor_1: 9 min (< 600s) → no finding; 11 min (> 600s) → finding
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=START_TEMP)
        assert rule.check(BASE_TS + timedelta(minutes=9)) == []
        assert len(rule.check(BASE_TS + timedelta(minutes=11))) == 1
        rule.on_floor_call_ended("floor_1")

        # floor_2: 14 min (< 900s) → no finding; 16 min (> 900s) → finding
        rule.on_floor_call_started("floor_2", BASE_TS, start_temp=START_TEMP)
        assert rule.check(BASE_TS + timedelta(minutes=14)) == []
        assert len(rule.check(BASE_TS + timedelta(minutes=16))) == 1
        rule.on_floor_call_ended("floor_2")

        # floor_3: 5 min (< 360s) → no finding; 7 min (> 360s) → finding
        rule.on_floor_call_started("floor_3", BASE_TS, start_temp=START_TEMP)
        assert rule.check(BASE_TS + timedelta(minutes=5)) == []
        assert len(rule.check(BASE_TS + timedelta(minutes=7))) == 1
        rule.on_floor_call_ended("floor_3")

    def test_start_temp_unknown_no_finding(self):
        """If start_temp is None (unknown), can't detect non-response → no finding."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 600})
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=None)

        now = BASE_TS + timedelta(minutes=20)
        findings = rule.check(now)

        assert findings == []

    def test_no_zones_calling_no_findings(self):
        """No zones calling → empty findings."""
        rule = FloorNoResponseRule()
        findings = rule.check(BASE_TS + timedelta(minutes=30))
        assert findings == []

    def test_custom_thresholds_dict(self):
        """Custom thresholds dict overrides defaults."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 120, "floor_2": 180, "floor_3": 60})
        rule.on_floor_call_started("floor_3", BASE_TS, start_temp=START_TEMP)

        # 59s → under threshold
        assert rule.check(BASE_TS + timedelta(seconds=59)) == []
        # 61s → over threshold, no temp rise → finding
        findings = rule.check(BASE_TS + timedelta(seconds=61))
        assert len(findings) == 1
        assert findings[0]["zone"] == "floor_3"

    def test_temp_updated_for_non_calling_zone_is_ignored(self):
        """on_temp_updated for a zone that isn't calling should not error."""
        rule = FloorNoResponseRule()
        rule.on_temp_updated("floor_2", 72.0)  # should be a no-op
        assert rule.check(BASE_TS) == []

    def test_finding_includes_current_temp_field(self):
        """Finding current_temp reflects the max_temp_seen at alert time."""
        rule = FloorNoResponseRule(thresholds_s={"floor_2": 300})
        rule.on_floor_call_started("floor_2", BASE_TS, start_temp=65.0)
        # A temp update that is NOT an increase (same value)
        rule.on_temp_updated("floor_2", 65.0)

        findings = rule.check(BASE_TS + timedelta(seconds=310))
        assert len(findings) == 1
        assert findings[0]["start_temp"] == 65.0
        assert findings[0]["current_temp"] == 65.0

    def test_new_call_after_ended_resets_alert(self):
        """After floor_call_ended + new floor_call_started, alert can fire again."""
        rule = FloorNoResponseRule(thresholds_s={"floor_1": 600})
        # First call: fires and clears
        rule.on_floor_call_started("floor_1", BASE_TS, start_temp=START_TEMP)
        assert len(rule.check(BASE_TS + timedelta(minutes=11))) == 1
        rule.on_floor_call_ended("floor_1")

        # Second call: should be able to fire again
        t2 = BASE_TS + timedelta(hours=1)
        rule.on_floor_call_started("floor_1", t2, start_temp=START_TEMP)
        findings = rule.check(t2 + timedelta(minutes=11))
        assert len(findings) == 1
