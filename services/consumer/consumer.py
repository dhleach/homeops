#!/usr/bin/env python3
"""
HomeOps consumer service — lean entry point.

Business logic lives in focused modules:
  - constants.py   shared entity maps and configuration
  - utils.py       utc_ts, follow, append_jsonl, _parse_dt, _get_version
  - state.py       _empty_daily_state, _save_state, _load_state, last_furnace_on_since
  - processors.py  process_floor_event, process_furnace_event, process_climate_event,
                   process_outdoor_temp_event
  - alerts.py      check_floor_2_warning, check_floor_2_escalation, check_observer_silence,
                   write_zone_temp_snapshot
  - reporting.py   emit_daily_summary, emit_floor_daily_summaries, format_daily_summary_message
"""

from __future__ import annotations

import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from metrics import HvacMetrics

from alerts import (
    check_floor_2_escalation,
    check_floor_2_warning,
    check_observer_silence,
    is_floor_2_telegram_rate_limited,
    write_zone_temp_snapshot,
)
from constants import (
    _FLOOR_ENTITIES,
    _ZONE_TO_CLIMATE_ENTITY,
    CLIMATE_ENTITIES,
    SLOW_TO_HEAT_THRESHOLDS_S,
    ZONE_TEMP_SNAPSHOT_INTERVAL_S,
)
from processors import (
    process_climate_event,
    process_floor_event,
    process_furnace_event,
    process_outdoor_temp_event,
)
from reporting import emit_daily_summary, emit_floor_daily_summaries, format_daily_summary_message
from state import (
    STATE_FILE,
    _empty_daily_state,
    _load_last_consumed_ts,
    _load_last_outdoor_temp,
    _load_state,
    _parse_dt,
    _save_state,
    last_furnace_on_since,
)
from telegram_commands import handle_telegram_commands
from utils import _get_version, append_jsonl, follow, utc_ts

sys.path.insert(0, "/tmp/homeops_ralph/services/observer")
from log_config import get_logger  # noqa: E402

_logger = get_logger("consumer")

# Module-level metrics singleton — set to an HvacMetrics instance in main() when the
# METRICS_PORT env var is present (or always).  None during unit tests that don't want
# a live HTTP server.
_metrics: HvacMetrics | None = None

_RESTART_CLEAR_SCHEMAS = frozenset(
    {
        "homeops.consumer.zone_setpoint_miss.v1",
        "homeops.consumer.zone_time_to_temp.v1",
    }
)


def _event_ts_suffix(processing_ts: str | None, wall_ts: datetime) -> str:
    """
    Return a Telegram message suffix showing the event timestamp.

    Always appends ``Event time: HH:MM UTC``. If the event timestamp differs
    from *wall_ts* by more than 5 minutes (e.g. during playback of old events)
    an additional note clarifies when the alert was actually sent.
    """
    if not processing_ts:
        return ""
    try:
        from dateutil.parser import isoparse as _isoparse

        event_dt = _isoparse(processing_ts)
        event_time_str = event_dt.strftime("%H:%M UTC")
        diff_s = abs((wall_ts - event_dt).total_seconds())
        if diff_s > 300:  # 5 min
            sent_time_str = wall_ts.strftime("%H:%M UTC")
            return (
                f"\nEvent time: {event_time_str}"
                f" (alert sent at {sent_time_str} — replayed from downtime)"
            )
        return f"\nEvent time: {event_time_str}"
    except Exception:
        return ""


def _emit_derived(derived: dict[str, Any], derived_log: str, fresh_restart: bool) -> bool:
    """
    Print + append a derived event; tag with across_restart when applicable.

    Returns the updated fresh_restart flag (cleared after the first full session).
    """
    if fresh_restart:
        derived["data"]["across_restart"] = True
    print(json.dumps(derived), flush=True)
    append_jsonl(derived_log, derived)
    if _metrics is not None:
        _metrics.update_from_event(derived.get("schema", ""), derived.get("data", {}))
    if fresh_restart and derived.get("schema") in _RESTART_CLEAR_SCHEMAS:
        _logger.info("Cleared fresh_restart after first full heating session")
        return False
    return fresh_restart


def _send_telegram(bot_token: str, chat_id: str, msg: str) -> None:
    """Fire-and-forget Telegram sendMessage; silently logs on failure."""
    if not (bot_token and chat_id):
        return
    import urllib.parse as _parse
    import urllib.request as _urllib

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = _parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
    try:
        _urllib.urlopen(url, data=data, timeout=10)
    except Exception as _e:
        _logger.warning("WARN: Telegram send failed: %s", _e)


def _make_furnace_short_call_event(
    duration_s: int,
    threshold_s: int,
    ended_at: str | None,
    processing_ts: str | None = None,
) -> dict[str, Any]:
    """Build a furnace_short_call_warning.v1 event dict."""
    return {
        "schema": "homeops.consumer.furnace_short_call_warning.v1",
        "source": "consumer.v1",
        "ts": processing_ts or utc_ts(),
        "data": {
            "duration_s": duration_s,
            "threshold_s": threshold_s,
            "ended_at": ended_at,
        },
    }


def _format_furnace_short_call_message(data: dict) -> str:
    """Format a Telegram alert for a furnace_short_call_warning.v1 event."""
    duration_s = data.get("duration_s", 0)
    threshold_s = data.get("threshold_s", 120)
    return (
        f"⚡ Furnace short-call warning!\n"
        f"Session ended in {duration_s}s (threshold: {threshold_s}s).\n"
        f"Rapid cycling is a precursor to lockout and equipment stress.\n"
        f"Check thermostat setpoints and HVAC filter."
    )


def _format_floor_anomaly_message(data: dict) -> str:
    """Format a Telegram alert message for a floor_runtime_anomaly.v1 event."""
    floor = data.get("floor", "unknown")
    floor_label = floor.replace("_", " ").title()
    date = data.get("date", "unknown")
    runtime_s = data.get("runtime_s", 0)
    baseline_mean_s = data.get("baseline_mean_s", 0.0)
    history_count = data.get("history_count", 0)
    severity = data.get("severity", "unknown")
    confidence = data.get("confidence", 0.0)

    runtime_h = round(runtime_s / 3600, 1)
    baseline_h = round(float(baseline_mean_s) / 3600, 1)
    if baseline_mean_s and baseline_mean_s > 0:
        multiplier = round(runtime_s / float(baseline_mean_s), 1)
    else:
        multiplier = 0.0

    severity_emoji = {"high": "🚨", "medium": "⚠️", "low": "📊"}.get(severity, "📊")

    return (
        f"{severity_emoji} {floor_label} runtime anomaly!\n"
        f"Date: {date}\n"
        f"Runtime: {runtime_s:,}s ({runtime_h}h) — {multiplier}× above baseline\n"
        f"Baseline: {baseline_mean_s:,.0f}s ({baseline_h}h avg over {history_count} days)\n"
        f"Severity: {severity} | Confidence: {round(confidence, 2)}"
    )


def _playback_phase(
    observer_log: str,
    last_consumed_ts: str,
    *,
    derived_log: str,
    floor_on_since: dict[str, datetime | None],
    furnace_on_since: datetime | None,
    climate_state: dict[str, Any],
    daily_state: dict[str, Any],
    floor_2_warn_sent: bool,
    fresh_restart: bool,
    current_date: str | None,
    floor_entities: dict[str, str],
    floor_no_response_rule: Any,
    furnace_session_anomaly_rule: Any,
    furnace_short_call_threshold_s: int = 120,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
) -> dict[str, Any]:
    """
    Replay missed observer events from *last_consumed_ts* to EOF.

    Reads the observer JSONL forward from the line whose ``ts`` field is
    ``>=`` *last_consumed_ts*, processes each event through the consumer
    state machine, emits derived events to *derived_log*, and sends Telegram
    alerts with the original event timestamp.

    Returns a dict of updated state keys:
        floor_on_since, furnace_on_since, climate_state, daily_state,
        floor_2_warn_sent, fresh_restart, current_date,
        last_consumed_observer_ts
    """
    from dateutil.parser import isoparse as _isoparse

    _LOG = "[PLAYBACK]"
    last_consumed_observer_ts: str | None = last_consumed_ts

    _logger.info("%s Starting catch-up replay from ts=%s", _LOG, last_consumed_ts)

    try:
        obs_file = open(observer_log, encoding="utf-8")  # noqa: SIM115
    except FileNotFoundError:
        _logger.warning("%s Observer log not found, skipping playback", _LOG)
        return {
            "floor_on_since": floor_on_since,
            "furnace_on_since": furnace_on_since,
            "climate_state": climate_state,
            "daily_state": daily_state,
            "floor_2_warn_sent": floor_2_warn_sent,
            "fresh_restart": fresh_restart,
            "current_date": current_date,
            "last_consumed_observer_ts": last_consumed_observer_ts,
        }

    event_count = 0

    with obs_file:
        # Seek forward until we find ts >= last_consumed_ts, then replay from there.
        found_start = False
        for raw_line in obs_file:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            schema = evt.get("schema")
            if schema != "homeops.observer.state_changed.v1":
                # Non-observer lines still count toward seek position but we
                # need to check ts for the seek decision.
                if not found_start:
                    evt_ts_str = evt.get("ts", "")
                    if evt_ts_str >= last_consumed_ts:
                        found_start = True
                continue  # skip non-observer events in all cases

            if not found_start:
                evt_ts_str = evt.get("ts", "")
                if evt_ts_str < last_consumed_ts:
                    continue
                found_start = True

            # --- process this observer event ---
            ts_str: str | None = evt.get("ts")
            if ts_str:
                last_consumed_observer_ts = ts_str
            data = evt.get("data", {})
            entity_id: str = data.get("entity_id", "")
            old_state: str | None = data.get("old_state")
            new_state: str | None = data.get("new_state")
            attributes: dict[str, Any] = data.get("attributes") or {}

            _logger.info(
                "%s %s %s %s: %s -> %s", _LOG, ts_str, schema, entity_id, old_state, new_state
            )

            ts: datetime | None = None
            try:
                ts = _isoparse(ts_str) if ts_str else None
            except Exception:
                pass

            # Date rollover during playback
            if ts is not None:
                evt_date = ts.strftime("%Y-%m-%d")
                if current_date is None:
                    current_date = evt_date
                elif evt_date != current_date:
                    summary = emit_daily_summary(daily_state, current_date)
                    _logger.info("%s date rollover → emitting daily summary", _LOG)
                    print(json.dumps(summary), flush=True)
                    append_jsonl(derived_log, summary)
                    for _floor_evt in emit_floor_daily_summaries(daily_state, current_date):
                        print(json.dumps(_floor_evt), flush=True)
                        append_jsonl(derived_log, _floor_evt)
                    if telegram_bot_token and telegram_chat_id:
                        from reporting import format_daily_summary_message  # noqa: PLC0415

                        tg_msg = format_daily_summary_message(summary["data"])
                        tg_msg += _event_ts_suffix(ts_str, datetime.now(UTC))
                        _send_telegram(telegram_bot_token, telegram_chat_id, tg_msg)
                    daily_state = _empty_daily_state()
                    _metrics.reset_daily_runtimes()
                    current_date = evt_date

                    # Floor runtime anomaly check
                    from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule  # noqa: PLC0415

                    _prior_summaries: list[dict] = []
                    _summary_date = summary["data"]["date"]
                    if Path(derived_log).exists():
                        try:
                            with open(derived_log, encoding="utf-8") as _dlog:
                                for _dline in _dlog:
                                    _dline = _dline.strip()
                                    if not _dline:
                                        continue
                                    try:
                                        _devt = json.loads(_dline)
                                    except json.JSONDecodeError:
                                        continue
                                    if (
                                        _devt.get("schema")
                                        == "homeops.consumer.furnace_daily_summary.v1"
                                        and _devt.get("data", {}).get("date") != _summary_date
                                    ):
                                        _prior_summaries.append(_devt)
                        except Exception:
                            pass
                    _runtime_anomaly_rule = FloorRuntimeAnomalyRule(history=_prior_summaries)
                    _per_floor = summary["data"].get("per_floor_runtime_s", {})
                    for _floor, _floor_runtime_s in _per_floor.items():
                        for _anom_evt in _runtime_anomaly_rule.check_daily_runtime(
                            _floor, _floor_runtime_s, summary["data"]["date"]
                        ):
                            print(json.dumps(_anom_evt), flush=True)
                            append_jsonl(derived_log, _anom_evt)
                            if telegram_bot_token and telegram_chat_id:
                                _anom_msg = _format_floor_anomaly_message(_anom_evt["data"])
                                _send_telegram(telegram_bot_token, telegram_chat_id, _anom_msg)

            state_saved = False

            # Floor events
            if entity_id in floor_entities:
                derived_events, floor_on_since, floor_2_warn_sent = process_floor_event(
                    entity_id,
                    old_state,
                    new_state,
                    ts,
                    ts_str,
                    floor_on_since,
                    floor_2_warn_sent,
                    processing_ts=ts_str,
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.floor_call_started.v1":
                        zone = derived["data"]["floor"]
                        climate_eid = _ZONE_TO_CLIMATE_ENTITY.get(zone)
                        start_temp = None
                        if climate_eid:
                            start_temp = (climate_state.get(climate_eid) or {}).get("current_temp")
                        floor_no_response_rule.on_floor_call_started(
                            zone, ts or datetime.now(UTC), start_temp
                        )
                    if derived["schema"] == "homeops.consumer.floor_call_ended.v1":
                        d = derived["data"]
                        floor_no_response_rule.on_floor_call_ended(d["floor"])
                        eid = d["entity_id"]
                        if d.get("duration_s") is not None:
                            daily_state["floor_runtime_s"][eid] = (
                                daily_state["floor_runtime_s"].get(eid, 0) + d["duration_s"]
                            )
                            prev_max = daily_state.get("per_floor_max_call_s", {}).get(eid)
                            if prev_max is None or d["duration_s"] > prev_max:
                                daily_state.setdefault("per_floor_max_call_s", {})[eid] = d[
                                    "duration_s"
                                ]
                        daily_state["per_floor_session_count"][eid] = (
                            daily_state["per_floor_session_count"].get(eid, 0) + 1
                        )
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                )
                state_saved = True

            # Outdoor temperature
            if entity_id == "sensor.outdoor_temperature":
                for derived in process_outdoor_temp_event(
                    entity_id, new_state, ts_str, processing_ts=ts_str
                ):
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    daily_state["outdoor_temps"].append(derived["data"]["temperature_f"])
                    daily_state["last_outdoor_temp_f"] = derived["data"]["temperature_f"]
                    daily_state["last_outdoor_temp_recorded_at"] = utc_ts()
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                )
                state_saved = True

            # Climate entities
            if entity_id in CLIMATE_ENTITIES:
                derived_events, climate_state = process_climate_event(
                    entity_id,
                    attributes,
                    ts_str,
                    climate_state,
                    new_state,
                    floor_on_since=floor_on_since,
                    daily_state=daily_state,
                    processing_ts=ts_str,
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.zone_slow_to_heat_warning.v1":
                        daily_state["warnings_triggered"]["zone_slow_to_heat"] += 1
                        d = derived["data"]
                        zone_label = d["zone"].replace("_", " ").title()
                        elapsed_min = d["elapsed_s"] // 60
                        start_t = d["start_temp"]
                        curr_t = d["current_temp"]
                        sp = d["setpoint"]
                        away = (
                            round(sp - curr_t, 1) if sp is not None and curr_t is not None else None
                        )
                        away_str = f"{away}°" if away is not None else "?"
                        msg = (
                            f"⚠️ {zone_label} slow to heat!\n"
                            f"Calling for {elapsed_min} min — setpoint not reached yet.\n"
                            f"Start: {start_t}°F → Now: {curr_t}°F → Target: {sp}°F"
                            f" ({away_str} away)" + _event_ts_suffix(ts_str, datetime.now(UTC))
                        )
                        outdoor_t = d.get("outdoor_temp_f")
                        if outdoor_t is not None:
                            msg += f"\nOutdoor temp: {round(outdoor_t)}°F"
                        _send_telegram(telegram_bot_token, telegram_chat_id, msg)
                    if derived["schema"] == "homeops.consumer.zone_setpoint_miss.v1":
                        daily_state["warnings_triggered"]["setpoint_miss"] += 1
                zone = CLIMATE_ENTITIES.get(entity_id)
                current_temp = (attributes or {}).get("current_temperature")
                if zone and current_temp is not None:
                    floor_no_response_rule.on_temp_updated(zone, current_temp)
                sp = (climate_state.get(entity_id) or {}).get("setpoint")
                if sp is not None:
                    samples = daily_state.setdefault("per_floor_setpoint_samples", {})
                    samples.setdefault(entity_id, []).append(sp)
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                )
                state_saved = True

            # Furnace events
            if entity_id == "binary_sensor.furnace_heating":
                derived_events, furnace_on_since = process_furnace_event(
                    entity_id,
                    old_state,
                    new_state,
                    ts,
                    ts_str,
                    furnace_on_since,
                    processing_ts=ts_str,
                    last_outdoor_temp_f=daily_state.get("last_outdoor_temp_f"),
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.heating_session_ended.v1":
                        d = derived["data"]
                        if d.get("duration_s") is not None:
                            daily_state["furnace_runtime_s"] += d["duration_s"]
                        daily_state["session_count"] += 1
                        _session_floor = d.get("floor")
                        _session_dur = d.get("duration_s")
                        _session_ts = d.get("ended_at") or derived["ts"]
                        for _anom in furnace_session_anomaly_rule.check_session(
                            _session_floor, _session_dur, _session_ts
                        ):
                            # Override rule-generated ts to use the original observer event ts.
                            _anom["ts"] = ts_str or _anom["ts"]
                            fresh_restart = _emit_derived(_anom, derived_log, fresh_restart)
                            _anom_data = _anom["data"]
                            if (
                                _anom["schema"]
                                == "homeops.consumer.heating_short_session_warning.v1"
                            ):
                                _msg = (
                                    f"⚡ Short furnace session on "
                                    f"{_anom_data['floor'] or 'unknown'}:"
                                    f" {_anom_data['duration_s']}s"
                                    f" (threshold: {_anom_data['threshold_s']}s)"
                                    " — possible short-cycling"
                                    + _event_ts_suffix(ts_str, datetime.now(UTC))
                                )
                                _send_telegram(telegram_bot_token, telegram_chat_id, _msg)
                            elif _anom[
                                "schema"
                            ] == "homeops.consumer.heating_long_session_warning.v1" and _anom_data[
                                "floor"
                            ] in ("floor_2", None):
                                _msg = (
                                    f"🔥 Long furnace session on "
                                    f"{_anom_data['floor'] or 'unknown'}:"
                                    f" {_anom_data['duration_s']}s"
                                    f" (threshold: {_anom_data['threshold_s']}s)"
                                    " — overheating risk"
                                    + _event_ts_suffix(ts_str, datetime.now(UTC))
                                )
                                _send_telegram(telegram_bot_token, telegram_chat_id, _msg)
                        # Short-call warning: rapid cycling detection
                        if (
                            _session_dur is not None
                            and _session_dur < furnace_short_call_threshold_s
                            and _session_dur > 0
                        ):
                            _sc_evt = _make_furnace_short_call_event(
                                _session_dur,
                                furnace_short_call_threshold_s,
                                _session_ts,
                                processing_ts=ts_str,
                            )
                            fresh_restart = _emit_derived(_sc_evt, derived_log, fresh_restart)
                            daily_state.setdefault("warnings_triggered", {})
                            daily_state["warnings_triggered"]["furnace_short_call"] = (
                                daily_state["warnings_triggered"].get("furnace_short_call", 0) + 1
                            )
                            if telegram_bot_token and telegram_chat_id:
                                _sc_msg = _format_furnace_short_call_message(
                                    _sc_evt["data"]
                                ) + _event_ts_suffix(ts_str, datetime.now(UTC))
                                _send_telegram(telegram_bot_token, telegram_chat_id, _sc_msg)
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                )
                state_saved = True

            # Catch-all save for events that don't match any entity block.
            if not state_saved and ts_str:
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                )

            event_count += 1

    _logger.info("%s Playback complete: replayed %s observer events", _LOG, event_count)

    return {
        "floor_on_since": floor_on_since,
        "furnace_on_since": furnace_on_since,
        "climate_state": climate_state,
        "daily_state": daily_state,
        "floor_2_warn_sent": floor_2_warn_sent,
        "fresh_restart": fresh_restart,
        "current_date": current_date,
        "last_consumed_observer_ts": last_consumed_observer_ts,
    }


def _register_sigterm_handler(*, state_file: Path | None = None) -> None:
    """Register a SIGTERM handler that stamps shutdown_ts into the state file."""
    sf = state_file or STATE_FILE

    def _handler(signum, frame):
        state: dict = {}
        if sf.exists():
            try:
                state = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                pass
        state["shutdown_ts"] = utc_ts()
        try:
            sf.write_text(json.dumps(state), encoding="utf-8")
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    """Tail observer events and emit derived floor/furnace session events."""
    path = os.environ.get("EVENT_LOG", "state/observer/events.jsonl")
    derived_log = os.environ.get("DERIVED_EVENT_LOG", "state/consumer/events.jsonl")
    _logger.info("Derived log: %s", derived_log)
    version = _get_version()
    _logger.info("Consumer version: %s", version)
    os.makedirs("state/consumer", exist_ok=True)
    with open("state/consumer/version.txt", "w", encoding="utf-8") as _vf:
        _vf.write(version + "\n")
    _logger.info("Consumer following: %s", path)

    global _metrics
    metrics_port = int(os.environ.get("METRICS_PORT", "8001"))
    _metrics = HvacMetrics(port=metrics_port)
    _metrics.start()

    furnace_short_call_threshold_s = int(
        os.environ.get("FURNACE_SHORT_CALL_THRESHOLD_S", "120")
    )  # 2 min
    _logger.info("Furnace short-call threshold: %ss", furnace_short_call_threshold_s)
    floor_2_warn_threshold_s = int(os.environ.get("FLOOR_2_WARN_THRESHOLD_S", "2700"))  # 45 min
    floor_2_telegram_rate_limit_s = int(
        os.environ.get("FLOOR_2_TELEGRAM_RATE_LIMIT_S", "3600")
    )  # 1 hour
    _logger.info("Floor-2 warning threshold: %ss", floor_2_warn_threshold_s)
    _logger.info("Floor-2 Telegram rate limit: %ss", floor_2_telegram_rate_limit_s)
    _logger.info(
        "Slow-to-heat thresholds: %s",
        ", ".join(f"{z}={t}s" for z, t in SLOW_TO_HEAT_THRESHOLDS_S.items()),
    )
    observer_silence_threshold_s = int(
        os.environ.get("OBSERVER_SILENCE_THRESHOLD_S", "4200")  # 70 min
    )  # 10 min
    _logger.info("Observer silence threshold: %ss", observer_silence_threshold_s)
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    telegram_command_interval_s = int(os.environ.get("TELEGRAM_COMMAND_CHECK_INTERVAL_S", "30"))
    _logger.info("Telegram command check interval: %ss", telegram_command_interval_s)

    _register_sigterm_handler()

    floor_entities = _FLOOR_ENTITIES
    floor_2_warn_sent = False  # reset each time floor 2 starts a new call
    last_observer_event_ts: datetime | None = None
    observer_silence_sent = False  # reset when a new event arrives after silence
    last_consumed_observer_ts: str | None = None  # updated after each processed event

    # Floor-not-responding rule (temp-based: zone calling > threshold with no temp rise).
    from rules.floor_no_response import FloorNoResponseRule  # noqa: PLC0415
    from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule  # noqa: PLC0415

    floor_no_response_rule = FloorNoResponseRule()

    # Furnace session anomaly rule — load baseline once at startup if available.
    _baseline_path = Path("state/consumer/baseline_constants.json")
    _baseline: dict = {}
    if _baseline_path.exists():
        try:
            _baseline = json.loads(_baseline_path.read_text(encoding="utf-8"))
            _logger.info("Loaded baseline from %s", _baseline_path)
        except Exception as _e:
            _logger.warning("WARN: Could not load baseline: %s", _e)
    furnace_session_anomaly_rule = FurnaceSessionAnomalyRule(_baseline)

    # Attempt to resume from a recent state file; otherwise cold-start.
    saved = _load_state()
    fresh_restart = True  # always set on any restart (cold or resume)

    if saved is not None:
        fos_raw = saved.get("floor_on_since") or {}
        floor_on_since = {k: _parse_dt(v) for k, v in fos_raw.items()}
        for k in floor_entities:
            floor_on_since.setdefault(k, None)
        furnace_on_since = _parse_dt(saved.get("furnace_on_since"))
        raw_cs = saved.get("climate_state") or {}
        climate_state: dict = {}
        for eid, es in raw_cs.items():
            s = dict(es)
            s["heating_start_ts"] = _parse_dt(s.get("heating_start_ts"))
            s["setpoint_reached_ts"] = _parse_dt(s.get("setpoint_reached_ts"))
            climate_state[eid] = s
        daily_state = saved.get("daily_state") or _empty_daily_state()
        _logger.info("Resumed from state file (saved_at=%s)", saved.get("saved_at"))
        # Warm up Prometheus metrics from persisted state so gauges show real
        # values immediately on restart — before any new events arrive.
        # Without this, floor_temperature_fahrenheit stays 0 until a temp change
        # triggers a thermostat_current_temp_updated event.
        if _metrics is not None:
            for eid, es in climate_state.items():
                zone = CLIMATE_ENTITIES.get(eid)
                temp = es.get("current_temp")
                if zone and temp is not None:
                    _metrics.set_floor_temperature(zone, float(temp))
                    _logger.info("Warmed metric floor_temp %s=%s", zone, temp)
                setpoint = es.get("setpoint")
                if zone and setpoint is not None:
                    _metrics.set_floor_setpoint(zone, float(setpoint))
                    _logger.info("Warmed metric floor_setpoint %s=%s", zone, setpoint)
            if furnace_on_since is not None:
                _metrics.set_furnace_active(True)
            if daily_state.get("last_outdoor_temp_f") is not None:
                _metrics.set_outdoor_temperature(float(daily_state["last_outdoor_temp_f"]))
    else:
        # Cold-start: bootstrap furnace state from the observer log.
        furnace_on_since = last_furnace_on_since(path)
        if furnace_on_since:
            _logger.info("Bootstrapped furnace_on_since=%s", furnace_on_since.isoformat())
        floor_on_since = {key: None for key in floor_entities.keys()}
        climate_state = {}
        daily_state = _empty_daily_state()
        # Seed outdoor temp from saved state if the reading is fresh enough (≤3 h).
        # This prevents the first post-restart heating session from having a null
        # outdoor_temp_f when a recent reading exists on disk.
        seeded_temp = _load_last_outdoor_temp()
        if seeded_temp is not None:
            daily_state["last_outdoor_temp_f"] = seeded_temp
            _logger.info("Seeded last_outdoor_temp_f=%s from saved state", seeded_temp)

    current_date = datetime.now(UTC).strftime("%Y-%m-%d")
    last_snapshot_ts: datetime | None = None
    last_command_check_ts: datetime | None = None
    # Restore last processed Telegram update_id so we don't re-process old commands.
    telegram_last_update_id: int | None = (
        saved.get("telegram_last_update_id") if saved is not None else None
    )
    # Restore floor-2 Telegram rate-limit timestamp (survives restarts).
    _f2_ts_raw: str | None = (
        saved.get("floor_2_telegram_last_sent_ts") if saved is not None else None
    )
    floor_2_telegram_last_sent_ts: datetime | None = _parse_dt(_f2_ts_raw) if _f2_ts_raw else None

    # Playback phase: catch up on missed observer events before entering live mode.
    playback_from_ts = _load_last_consumed_ts()
    if playback_from_ts:
        _logger.info("Found last_consumed_observer_ts=%s", playback_from_ts)
        _pb_result = _playback_phase(
            path,
            playback_from_ts,
            derived_log=derived_log,
            floor_on_since=floor_on_since,
            furnace_on_since=furnace_on_since,
            climate_state=climate_state,
            daily_state=daily_state,
            floor_2_warn_sent=floor_2_warn_sent,
            fresh_restart=fresh_restart,
            current_date=current_date,
            floor_entities=floor_entities,
            floor_no_response_rule=floor_no_response_rule,
            furnace_session_anomaly_rule=furnace_session_anomaly_rule,
            furnace_short_call_threshold_s=furnace_short_call_threshold_s,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
        )
        floor_on_since = _pb_result["floor_on_since"]
        furnace_on_since = _pb_result["furnace_on_since"]
        climate_state = _pb_result["climate_state"]
        daily_state = _pb_result["daily_state"]
        floor_2_warn_sent = _pb_result["floor_2_warn_sent"]
        fresh_restart = _pb_result["fresh_restart"]
        current_date = _pb_result["current_date"] or current_date
        last_consumed_observer_ts = _pb_result["last_consumed_observer_ts"]
        # Restore gauge from saved daily_state so the metric reflects the full
        # day's runtime, not just sessions replayed since last_consumed_ts.
        # daily_state["floor_runtime_s"] is keyed by entity_id; translate to floor name.
        _raw_runtimes = daily_state.get("floor_runtime_s", {})
        _per_floor = {
            _FLOOR_ENTITIES[eid]: secs
            for eid, secs in _raw_runtimes.items()
            if eid in _FLOOR_ENTITIES
        }
        _metrics.restore_daily_runtimes(_per_floor)
        _logger.info("[LIVE] Entering live tail mode")
    else:
        _logger.info("[LIVE] Cold-start — no playback state found")

    # Main stream loop: consume observer events and emit higher-level derived events.
    for line in follow(path):
        if line is None:
            # Timeout — no new events. Just run the in-flight check below.
            pass
        else:
            try:
                evt = json.loads(line)
            except json.JSONDecodeError as e:
                _logger.warning("WARN: bad json line: %s", e)
                continue

            schema = evt.get("schema")
            # Ignore non-observer events if this file is shared with other producers.
            if schema != "homeops.observer.state_changed.v1":
                continue

            # Track last observer event time for silence watchdog.
            last_observer_event_ts = datetime.now(UTC)
            if observer_silence_sent:
                # New event arrived after a silence period — reset dedup flag.
                observer_silence_sent = False

            ts_str = evt.get("ts")
            # Update last-consumed pointer for every observer event we handle.
            if ts_str:
                last_consumed_observer_ts = ts_str
            data = evt.get("data", {})
            entity_id = data.get("entity_id")
            old_state = data.get("old_state")
            new_state = data.get("new_state")
            attributes = data.get("attributes") or {}

            # Always keep the simple print
            print(f"{ts_str} {schema} {entity_id}: {old_state} -> {new_state}", flush=True)

            try:
                from dateutil.parser import isoparse

                ts = isoparse(ts_str) if ts_str else None
            except Exception:
                # Preserve processing even if one event has a malformed timestamp.
                ts = None

            # Date rollover: emit daily summary when the event date changes.
            if ts is not None:
                evt_date = ts.strftime("%Y-%m-%d")
                if current_date is None:
                    current_date = evt_date
                elif evt_date != current_date:
                    summary = emit_daily_summary(daily_state, current_date)
                    print(json.dumps(summary), flush=True)
                    append_jsonl(derived_log, summary)
                    for _floor_evt in emit_floor_daily_summaries(daily_state, current_date):
                        print(json.dumps(_floor_evt), flush=True)
                        append_jsonl(derived_log, _floor_evt)
                    if telegram_bot_token and telegram_chat_id:
                        import urllib.parse as _parse
                        import urllib.request as _urllib

                        tg_msg = format_daily_summary_message(summary["data"])
                        tg_url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                        tg_data = _parse.urlencode(
                            {"chat_id": telegram_chat_id, "text": tg_msg}
                        ).encode()
                        try:
                            _urllib.urlopen(tg_url, tg_data, timeout=10)
                        except Exception as e:
                            _logger.warning("WARN: Telegram daily summary failed: %s", e)
                    else:
                        _logger.warning(
                            "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping daily summary alert"  # noqa: E501
                        )
                    daily_state = _empty_daily_state()
                    _metrics.reset_daily_runtimes()
                    current_date = evt_date

                    # --- Floor runtime anomaly detection ---
                    # Load prior daily summaries (exclude today to avoid circular reference).
                    from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule  # noqa: PLC0415

                    _prior_summaries: list[dict] = []
                    _summary_date = summary["data"]["date"]
                    if Path(derived_log).exists():
                        try:
                            with open(derived_log, encoding="utf-8") as _dlog:
                                for _line in _dlog:
                                    _line = _line.strip()
                                    if not _line:
                                        continue
                                    try:
                                        _evt = json.loads(_line)
                                    except json.JSONDecodeError:
                                        continue
                                    if (
                                        _evt.get("schema")
                                        == "homeops.consumer.furnace_daily_summary.v1"
                                        and _evt.get("data", {}).get("date") != _summary_date
                                    ):
                                        _prior_summaries.append(_evt)
                        except Exception as _e:
                            _logger.warning(
                                "WARN: Could not read derived log for anomaly check: %s", _e
                            )

                    _runtime_anomaly_rule = FloorRuntimeAnomalyRule(history=_prior_summaries)
                    _per_floor = summary["data"].get("per_floor_runtime_s", {})
                    for _floor, _floor_runtime_s in _per_floor.items():
                        for _anom_evt in _runtime_anomaly_rule.check_daily_runtime(
                            _floor, _floor_runtime_s, summary["data"]["date"]
                        ):
                            print(json.dumps(_anom_evt), flush=True)
                            append_jsonl(derived_log, _anom_evt)
                            if telegram_bot_token and telegram_chat_id:
                                _anom_msg = _format_floor_anomaly_message(_anom_evt["data"])
                                _send_telegram(telegram_bot_token, telegram_chat_id, _anom_msg)

            # Per-floor call sessions are derived from floor_* heating_call sensors.
            if entity_id in floor_entities:
                derived_events, floor_on_since, floor_2_warn_sent = process_floor_event(
                    entity_id,
                    old_state,
                    new_state,
                    ts,
                    ts_str,
                    floor_on_since,
                    floor_2_warn_sent,
                    processing_ts=ts_str,
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.floor_call_started.v1":
                        zone = derived["data"]["floor"]
                        climate_eid = _ZONE_TO_CLIMATE_ENTITY.get(zone)
                        start_temp = None
                        if climate_eid:
                            start_temp = (climate_state.get(climate_eid) or {}).get("current_temp")
                        floor_no_response_rule.on_floor_call_started(
                            zone, ts or datetime.now(UTC), start_temp
                        )
                    if derived["schema"] == "homeops.consumer.floor_call_ended.v1":
                        d = derived["data"]
                        floor_no_response_rule.on_floor_call_ended(d["floor"])
                        eid = d["entity_id"]
                        if d.get("duration_s") is not None:
                            daily_state["floor_runtime_s"][eid] = (
                                daily_state["floor_runtime_s"].get(eid, 0) + d["duration_s"]
                            )
                            prev_max = daily_state.get("per_floor_max_call_s", {}).get(eid)
                            if prev_max is None or d["duration_s"] > prev_max:
                                daily_state.setdefault("per_floor_max_call_s", {})[eid] = d[
                                    "duration_s"
                                ]
                        daily_state["per_floor_session_count"][eid] = (
                            daily_state["per_floor_session_count"].get(eid, 0) + 1
                        )
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                    floor_2_telegram_last_sent_ts=floor_2_telegram_last_sent_ts,
                )

            # Outdoor temperature readings are passed through as-is from the sensor.
            if entity_id == "sensor.outdoor_temperature":
                for derived in process_outdoor_temp_event(
                    entity_id, new_state, ts_str, processing_ts=ts_str
                ):
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    daily_state["outdoor_temps"].append(derived["data"]["temperature_f"])
                    daily_state["last_outdoor_temp_f"] = derived["data"]["temperature_f"]
                    daily_state["last_outdoor_temp_recorded_at"] = utc_ts()
                if new_state in (None, "unavailable", "unknown", ""):
                    _logger.warning("WARN: outdoor_temperature state unavailable, skipping")
                else:
                    try:
                        float(new_state)
                    except (ValueError, TypeError):
                        _logger.warning(
                            "WARN: outdoor_temperature non-numeric value %r, skipping", new_state
                        )
                # Always save on outdoor_temp event — this is the 62-min heartbeat write.
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                    floor_2_telegram_last_sent_ts=floor_2_telegram_last_sent_ts,
                )

            # Thermostat climate entities: setpoint, current temp, and mode changes.
            if entity_id in CLIMATE_ENTITIES:
                derived_events, climate_state = process_climate_event(
                    entity_id,
                    attributes,
                    ts_str,
                    climate_state,
                    new_state,
                    floor_on_since=floor_on_since,
                    daily_state=daily_state,
                    processing_ts=ts_str,
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.zone_slow_to_heat_warning.v1":
                        daily_state["warnings_triggered"]["zone_slow_to_heat"] += 1
                        d = derived["data"]
                        zone_label = d["zone"].replace("_", " ").title()
                        elapsed_min = d["elapsed_s"] // 60
                        start_t = d["start_temp"]
                        curr_t = d["current_temp"]
                        sp = d["setpoint"]
                        away = (
                            round(sp - curr_t, 1) if sp is not None and curr_t is not None else None
                        )
                        away_str = f"{away}°" if away is not None else "?"
                        msg = (
                            f"⚠️ {zone_label} slow to heat!\n"
                            f"Calling for {elapsed_min} min — setpoint not reached yet.\n"
                            f"Start: {start_t}°F → Now: {curr_t}°F → Target: {sp}°F"
                            f" ({away_str} away)"
                        )
                        outdoor_t = d.get("outdoor_temp_f")
                        if outdoor_t is not None:
                            msg += f"\nOutdoor temp: {round(outdoor_t)}°F"
                        msg += _event_ts_suffix(ts_str, datetime.now(UTC))
                        if telegram_bot_token and telegram_chat_id:
                            import urllib.parse as _parse
                            import urllib.request as _urllib

                            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                            data = _parse.urlencode(
                                {"chat_id": telegram_chat_id, "text": msg}
                            ).encode()
                            try:
                                _urllib.urlopen(url, data=data, timeout=10)
                            except Exception as e:
                                _logger.warning("WARN: Telegram slow-to-heat alert failed: %s", e)
                        else:
                            _logger.warning(
                                "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping slow-to-heat alert"  # noqa: E501
                            )
                    if derived["schema"] == "homeops.consumer.zone_setpoint_miss.v1":
                        daily_state["warnings_triggered"]["setpoint_miss"] += 1
                # Feed temperature updates to the floor-not-responding rule.
                zone = CLIMATE_ENTITIES.get(entity_id)
                current_temp = (attributes or {}).get("current_temperature")
                if zone and current_temp is not None:
                    floor_no_response_rule.on_temp_updated(zone, current_temp)
                # Accumulate setpoint sample for daily summary avg
                sp = (climate_state.get(entity_id) or {}).get("setpoint")
                if sp is not None:
                    samples = daily_state.setdefault("per_floor_setpoint_samples", {})
                    samples.setdefault(entity_id, []).append(sp)
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                    floor_2_telegram_last_sent_ts=floor_2_telegram_last_sent_ts,
                )

            # Whole-home heating sessions are derived from furnace on/off transitions.
            if entity_id == "binary_sensor.furnace_heating":
                derived_events, furnace_on_since = process_furnace_event(
                    entity_id,
                    old_state,
                    new_state,
                    ts,
                    ts_str,
                    furnace_on_since,
                    processing_ts=ts_str,
                    last_outdoor_temp_f=daily_state.get("last_outdoor_temp_f"),
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.heating_session_ended.v1":
                        d = derived["data"]
                        if d.get("duration_s") is not None:
                            daily_state["furnace_runtime_s"] += d["duration_s"]
                        daily_state["session_count"] += 1
                        # Check for session duration anomalies.
                        _session_floor = d.get("floor")
                        _session_dur = d.get("duration_s")
                        _session_ts = d.get("ended_at") or derived["ts"]
                        for _anom in furnace_session_anomaly_rule.check_session(
                            _session_floor, _session_dur, _session_ts
                        ):
                            fresh_restart = _emit_derived(_anom, derived_log, fresh_restart)
                            _anom_data = _anom["data"]
                            if (
                                _anom["schema"]
                                == "homeops.consumer.heating_short_session_warning.v1"
                            ):
                                _anom_floor = _anom_data["floor"] or "unknown"
                                _anom_dur = _anom_data["duration_s"]
                                _anom_thr = _anom_data["threshold_s"]
                                _anom_msg = (
                                    f"⚡ Short furnace session on {_anom_floor}:"
                                    f" {_anom_dur}s (threshold: {_anom_thr}s)"
                                    " — possible short-cycling"
                                    + _event_ts_suffix(ts_str, datetime.now(UTC))
                                )
                                if telegram_bot_token and telegram_chat_id:
                                    import urllib.parse as _parse
                                    import urllib.request as _urllib

                                    _url = (
                                        f"https://api.telegram.org/bot{telegram_bot_token}"
                                        "/sendMessage"
                                    )
                                    _tdata = _parse.urlencode(
                                        {"chat_id": telegram_chat_id, "text": _anom_msg}
                                    ).encode()
                                    try:
                                        _urllib.urlopen(_url, _tdata, timeout=10)
                                    except Exception as _te:
                                        _logger.warning(
                                            "WARN: Telegram short-session alert failed: %s", _te
                                        )  # noqa: E501
                                else:
                                    _logger.warning(
                                        "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping short-session alert"  # noqa: E501
                                    )  # noqa: E501
                            elif _anom[
                                "schema"
                            ] == "homeops.consumer.heating_long_session_warning.v1" and _anom_data[
                                "floor"
                            ] in ("floor_2", None):
                                _anom_floor = _anom_data["floor"] or "unknown"
                                _anom_dur = _anom_data["duration_s"]
                                _anom_thr = _anom_data["threshold_s"]
                                _anom_msg = (
                                    f"🔥 Long furnace session on {_anom_floor}:"
                                    f" {_anom_dur}s (threshold: {_anom_thr}s)"
                                    " — overheating risk"
                                    + _event_ts_suffix(ts_str, datetime.now(UTC))
                                )
                                if telegram_bot_token and telegram_chat_id:
                                    import urllib.parse as _parse
                                    import urllib.request as _urllib

                                    _url = (
                                        f"https://api.telegram.org/bot{telegram_bot_token}"
                                        "/sendMessage"
                                    )
                                    _tdata = _parse.urlencode(
                                        {"chat_id": telegram_chat_id, "text": _anom_msg}
                                    ).encode()
                                    try:
                                        _urllib.urlopen(_url, _tdata, timeout=10)
                                    except Exception as _te:
                                        _logger.warning(
                                            "WARN: Telegram long-session alert failed: %s", _te
                                        )  # noqa: E501
                                else:
                                    _logger.warning(
                                        "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping long-session alert"  # noqa: E501
                                    )  # noqa: E501
                        # Short-call warning: rapid cycling detection
                        if (
                            _session_dur is not None
                            and _session_dur < furnace_short_call_threshold_s
                            and _session_dur > 0
                        ):
                            _sc_evt = _make_furnace_short_call_event(
                                _session_dur,
                                furnace_short_call_threshold_s,
                                _session_ts,
                                processing_ts=ts_str,
                            )
                            fresh_restart = _emit_derived(_sc_evt, derived_log, fresh_restart)
                            daily_state.setdefault("warnings_triggered", {})
                            daily_state["warnings_triggered"]["furnace_short_call"] = (
                                daily_state["warnings_triggered"].get("furnace_short_call", 0) + 1
                            )
                            if telegram_bot_token and telegram_chat_id:
                                _sc_msg = _format_furnace_short_call_message(
                                    _sc_evt["data"]
                                ) + _event_ts_suffix(ts_str, datetime.now(UTC))
                                _send_telegram(telegram_bot_token, telegram_chat_id, _sc_msg)
                            else:
                                _logger.warning(
                                    "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping short-call alert"  # noqa: E501
                                )
                _save_state(
                    floor_on_since,
                    furnace_on_since,
                    climate_state,
                    daily_state,
                    last_consumed_observer_ts=last_consumed_observer_ts,
                    floor_2_telegram_last_sent_ts=floor_2_telegram_last_sent_ts,
                )

        # In-flight floor-2 long-call check (runs on every event and on timeouts)
        warn_event, floor_2_warn_sent = check_floor_2_warning(
            floor_on_since,
            floor_2_warn_sent,
            floor_2_warn_threshold_s,
            datetime.now(UTC),
            climate_state,
        )
        if warn_event:
            daily_state["warnings_triggered"]["floor_2_long_call"] += 1
            fresh_restart = _emit_derived(warn_event, derived_log, fresh_restart)
            _now_for_rl = datetime.now(UTC)
            if is_floor_2_telegram_rate_limited(
                floor_2_telegram_last_sent_ts, floor_2_telegram_rate_limit_s, _now_for_rl
            ):
                daily_state["floor_2_telegram_suppressed_count"] = (
                    daily_state.get("floor_2_telegram_suppressed_count", 0) + 1
                )
                _logger.info(
                    "Floor-2 Telegram suppressed (rate limit %ss); suppressed_count=%s",
                    floor_2_telegram_rate_limit_s,
                    daily_state["floor_2_telegram_suppressed_count"],
                )  # noqa: E501
            elif telegram_bot_token and telegram_chat_id:
                import urllib.parse as _parse
                import urllib.request as _urllib

                elapsed_s = warn_event["data"]["elapsed_s"]
                current_temp = warn_event["data"].get("current_temp")
                setpoint = warn_event["data"].get("setpoint")
                temp_line = ""
                if current_temp is not None and setpoint is not None:
                    delta = abs(round(setpoint - current_temp))
                    temp_line = (
                        f"Current temp: {current_temp}°F → Setpoint: {setpoint}°F ({delta}° away)\n"
                    )
                suppressed = daily_state.get("floor_2_telegram_suppressed_count", 0)
                suppressed_line = (
                    f"({suppressed} previous alert(s) suppressed in the last"
                    f" {floor_2_telegram_rate_limit_s // 60} min)\n"
                    if suppressed > 0
                    else ""
                )
                # Use event generation time (≈ now), not started_at.
                # started_at is always 45+ min old → would always say "replayed from downtime".
                _warn_ts = warn_event.get("ts")
                msg = (
                    f"⚠️ Floor 2 has been calling for {elapsed_s // 60} min!\n"
                    f"{temp_line}"
                    f"{suppressed_line}"
                    f"Risk of furnace overheating (Code 4/7 limit trip).\n"
                    f"Consider lowering floor 2 thermostat manually."
                    + _event_ts_suffix(_warn_ts, _now_for_rl)
                )
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                data = _parse.urlencode({"chat_id": telegram_chat_id, "text": msg}).encode()
                try:
                    _urllib.urlopen(url, data=data, timeout=10)
                    floor_2_telegram_last_sent_ts = _now_for_rl
                    daily_state["floor_2_telegram_suppressed_count"] = 0
                except Exception as e:
                    _logger.warning("WARN: Telegram alert failed: %s", e)
            else:
                _logger.warning(
                    "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping alert"
                )  # noqa: E501
            # Escalation: fire on 2nd, 3rd, etc. long-call warning in the same day
            long_call_count = daily_state["warnings_triggered"]["floor_2_long_call"]
            escalation_event = check_floor_2_escalation(
                long_call_count, floor_2_warn_threshold_s, climate_state
            )
            if escalation_event:
                daily_state["warnings_triggered"]["floor_2_escalation"] += 1
                fresh_restart = _emit_derived(escalation_event, derived_log, fresh_restart)
                _esc_now = datetime.now(UTC)
                if is_floor_2_telegram_rate_limited(
                    floor_2_telegram_last_sent_ts, floor_2_telegram_rate_limit_s, _esc_now
                ):
                    daily_state["floor_2_telegram_suppressed_count"] = (
                        daily_state.get("floor_2_telegram_suppressed_count", 0) + 1
                    )
                    _logger.info(
                        "Floor-2 escalation Telegram suppressed (rate limit); suppressed_count=%s",
                        daily_state["floor_2_telegram_suppressed_count"],
                    )
                elif telegram_bot_token and telegram_chat_id:
                    import urllib.parse as _parse
                    import urllib.request as _urllib

                    suppressed = daily_state.get("floor_2_telegram_suppressed_count", 0)
                    suppressed_line = (
                        f"({suppressed} previous alert(s) suppressed in the last"
                        f" {floor_2_telegram_rate_limit_s // 60} min)\n"
                        if suppressed > 0
                        else ""
                    )
                    esc_msg = (
                        f"🚨 Floor 2 long-call escalation: {long_call_count} long calls today"
                        f"\n{suppressed_line}"
                        " — furnace may be struggling. Check HVAC."
                    )
                    _url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                    _tdata = _parse.urlencode(
                        {"chat_id": telegram_chat_id, "text": esc_msg}
                    ).encode()
                    try:
                        _urllib.urlopen(_url, _tdata, timeout=10)
                        floor_2_telegram_last_sent_ts = _esc_now
                        daily_state["floor_2_telegram_suppressed_count"] = 0
                    except Exception as _esc_e:
                        _logger.warning("WARN: Telegram escalation alert failed: %s", _esc_e)
                else:
                    _logger.warning(
                        "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping escalation alert"  # noqa: E501
                    )

        # Observer silence watchdog (runs on every event and on timeouts)
        silence_event, observer_silence_sent = check_observer_silence(
            last_observer_event_ts,
            observer_silence_sent,
            observer_silence_threshold_s,
            datetime.now(UTC),
        )
        if silence_event:
            daily_state["warnings_triggered"]["observer_silence"] += 1
            fresh_restart = _emit_derived(silence_event, derived_log, fresh_restart)
            if telegram_bot_token and telegram_chat_id:
                import urllib.parse as _parse
                import urllib.request as _urllib

                silence_s = silence_event["data"]["silence_s"]
                last_ts = silence_event["data"]["last_event_ts"]
                silence_min = silence_s // 60
                msg = (
                    f"⚠️ Observer silence detected!\n"
                    f"No events received for {silence_min} min.\n"
                    f"Last event: {last_ts}\n"
                    f"Check homeops.now to confirm data is still flowing.\n"
                    f"If not, check observer service on Pi."
                    # no _event_ts_suffix: last_ts is intentionally old (it's the
                    # last-seen event before silence) — would always show "replayed"
                )
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                data = _parse.urlencode({"chat_id": telegram_chat_id, "text": msg}).encode()
                try:
                    _urllib.urlopen(url, data=data, timeout=10)
                except Exception as e:
                    _logger.warning("WARN: Telegram alert failed: %s", e)
            else:
                _logger.warning(
                    "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping observer silence alert"  # noqa: E501
                )

        # In-flight floor-not-responding check (runs on every event and on timeouts)
        for finding in floor_no_response_rule.check(datetime.now(UTC)):
            no_resp_event = {
                "schema": "homeops.consumer.floor_no_response_warning.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": finding,
            }
            daily_state["warnings_triggered"]["floor_no_response"] += 1
            fresh_restart = _emit_derived(no_resp_event, derived_log, fresh_restart)
            zone_label = finding["zone"].replace("_", " ").title()
            if telegram_bot_token and telegram_chat_id:
                import urllib.parse as _parse
                import urllib.request as _urllib

                start_t = finding["start_temp"]
                curr_t = finding["current_temp"]
                elapsed_m = finding["minutes_elapsed"]
                msg = (
                    f"⚠️ {zone_label} not responding!\n"
                    f"Calling for {elapsed_m:.0f} min with no temperature increase.\n"
                    f"Start temp: {start_t}°F, Current: {curr_t}°F\n"
                    f"Check thermostat or vents."
                    + _event_ts_suffix(no_resp_event.get("ts"), datetime.now(UTC))
                )
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                data = _parse.urlencode({"chat_id": telegram_chat_id, "text": msg}).encode()
                try:
                    _urllib.urlopen(url, data=data, timeout=10)
                except Exception as e:
                    _logger.warning("WARN: Telegram alert failed: %s", e)
            else:
                _logger.warning(
                    "WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping floor-not-responding alert"  # noqa: E501
                )

        # Zone temperature snapshot — write every 5 minutes if we have data.
        now = datetime.now(UTC)
        if (
            last_snapshot_ts is None
            or (now - last_snapshot_ts).total_seconds() >= ZONE_TEMP_SNAPSHOT_INTERVAL_S
        ):
            if write_zone_temp_snapshot(climate_state, daily_state):
                _logger.info("Zone temp snapshot written")
            last_snapshot_ts = now

        # Telegram command polling — check for /summary every ~30 s.
        if telegram_bot_token and telegram_chat_id:
            if (
                last_command_check_ts is None
                or (now - last_command_check_ts).total_seconds() >= telegram_command_interval_s
            ):
                new_update_id = handle_telegram_commands(
                    bot_token=telegram_bot_token,
                    chat_id=telegram_chat_id,
                    last_update_id=telegram_last_update_id,
                    furnace_on_since=furnace_on_since,
                    floor_on_since=floor_on_since,
                    climate_state=climate_state,
                    daily_state=daily_state,
                    now=now,
                )
                if new_update_id != telegram_last_update_id:
                    telegram_last_update_id = new_update_id
                    _save_state(
                        floor_on_since,
                        furnace_on_since,
                        climate_state,
                        daily_state,
                        last_consumed_observer_ts=last_consumed_observer_ts,
                        telegram_last_update_id=telegram_last_update_id,
                        floor_2_telegram_last_sent_ts=floor_2_telegram_last_sent_ts,
                    )
                last_command_check_ts = now


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Backward-compat re-exports — tests import these names from `consumer`
# ---------------------------------------------------------------------------
# (all are already imported at the top of this module)
__all__ = [
    # constants
    "SLOW_TO_HEAT_THRESHOLDS_S",
    "ZONE_TEMP_SNAPSHOT_INTERVAL_S",
    # entry-point functions (defined here)
    "_emit_derived",
    "_format_furnace_short_call_message",
    "_make_furnace_short_call_event",
    "_format_floor_anomaly_message",
    "_playback_phase",
    "_register_sigterm_handler",
    "_send_telegram",
    # state
    "_empty_daily_state",
    "_load_last_consumed_ts",
    "_load_state",
    "_parse_dt",
    "_save_state",
    "last_furnace_on_since",
    # processors
    "process_climate_event",
    "process_floor_event",
    "process_furnace_event",
    "process_outdoor_temp_event",
    # alerts
    "check_floor_2_escalation",
    "check_floor_2_warning",
    "is_floor_2_telegram_rate_limited",
    "check_observer_silence",
    "write_zone_temp_snapshot",
    # reporting
    "emit_daily_summary",
    "emit_floor_daily_summaries",
    "format_daily_summary_message",
    # telegram_commands
    "handle_telegram_commands",
]
