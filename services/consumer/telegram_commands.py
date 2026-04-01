"""
Telegram command handling for the HomeOps consumer service.

Provides `/summary` command support via Telegram's getUpdates long-polling.
The consumer calls `handle_telegram_commands()` periodically (every ~30 s)
to check for incoming messages and respond with a live HVAC status summary.
"""

from __future__ import annotations

import json
import urllib.parse as _parse
import urllib.request as _urllib
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from constants import _FLOOR_ENTITIES

_EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------


def get_updates(bot_token: str, offset: int | None = None, timeout: int = 0) -> list[dict]:
    """
    Call Telegram's getUpdates endpoint and return a list of update dicts.

    Args:
        bot_token: The Telegram bot token.
        offset: If provided, only updates with update_id >= offset are returned.
        timeout: Long-poll timeout in seconds (0 = short poll).

    Returns:
        List of Telegram update dicts.  Returns [] on any error.
    """
    params: dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?" + _parse.urlencode(params)
    try:
        with _urllib.urlopen(url, timeout=timeout + 10) as resp:
            body = json.loads(resp.read().decode())
        if body.get("ok"):
            return body.get("result", [])
    except Exception:
        pass
    return []


def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    """
    Send a Telegram message.

    Returns True on success, False on failure.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = _parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        _urllib.urlopen(url, data=data, timeout=10)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: int) -> str:
    """Format a duration in seconds as a compact human-readable string (e.g. '1h 12m')."""
    if seconds <= 0:
        return "0m"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_status_summary(
    *,
    furnace_on_since: datetime | None,
    floor_on_since: dict[str, datetime | None],
    climate_state: dict[str, Any],
    daily_state: dict[str, Any],
    now: datetime | None = None,
) -> str:
    """
    Build a live HVAC status summary suitable for sending via Telegram.

    Args:
        furnace_on_since: datetime when the furnace turned on (None = off).
        floor_on_since: mapping of floor entity_id → datetime when call started (None = idle).
        climate_state: mapping of climate entity_id → state dict with
                       'current_temp', 'setpoint', 'hvac_action' keys.
        daily_state: accumulated daily counters (furnace_runtime_s, session_count, etc.).
        now: wall-clock datetime to use (defaults to datetime.now(UTC)).

    Returns:
        Multi-line string suitable for Telegram sendMessage.
    """
    if now is None:
        now = datetime.now(UTC)

    # Header — display in Eastern time
    now_eastern = now.astimezone(_EASTERN)
    time_str = now_eastern.strftime("%-I:%M %p") if now else "–"
    lines: list[str] = [f"🏠 HomeOps Status — {time_str}"]
    lines.append("")

    # Outdoor temperature
    outdoor_f: float | None = daily_state.get("last_outdoor_temp_f")
    if outdoor_f is not None:
        lines.append(f"🌡️ Outdoor: {round(outdoor_f)}°F")
        lines.append("")

    # Furnace status
    furnace_line = "🔥 Furnace: ON"
    if furnace_on_since is not None:
        running_s = int((now - furnace_on_since).total_seconds())
        running_m = running_s // 60
        furnace_line += f" (running {running_m} min)"
    else:
        furnace_line = "💤 Furnace: OFF"
    lines.append(furnace_line)

    # Per-floor status
    floor_display_order: list[tuple[str, str, str]] = [
        ("binary_sensor.floor_1_heating_call", "climate.floor_1_thermostat", "Floor 1"),
        ("binary_sensor.floor_2_heating_call", "climate.floor_2_thermostat", "Floor 2"),
        ("binary_sensor.floor_3_heating_call", "climate.floor_3_thermostat", "Floor 3"),
    ]

    floor_runtime_s: dict[str, int] = daily_state.get("floor_runtime_s", {})
    # In-flight runtimes are not yet committed to floor_runtime_s — add them.
    active_floor_runtime: dict[str, int] = {}
    for floor_eid, _, _ in floor_display_order:
        on_since = floor_on_since.get(floor_eid)
        if on_since is not None:
            active_floor_runtime[floor_eid] = int((now - on_since).total_seconds())

    for floor_eid, climate_eid, label in floor_display_order:
        cs = climate_state.get(climate_eid) or {}
        current_temp: float | None = cs.get("current_temp")
        setpoint: float | None = cs.get("setpoint")
        hvac_action: str | None = cs.get("hvac_action")

        # Build temp / setpoint display
        temp_str = f"{round(current_temp)}°F" if current_temp is not None else "?°F"
        sp_str = f"{round(setpoint)}°F" if setpoint is not None else "?°F"

        # Action label
        if hvac_action == "heating":
            action_label = "heating"
        else:
            action_label = "idle"

        # Today's runtime (committed + in-flight if currently active)
        committed_s = floor_runtime_s.get(floor_eid, 0)
        in_flight_s = active_floor_runtime.get(floor_eid, 0)
        today_s = committed_s + in_flight_s
        today_str = _fmt_duration(today_s)

        # Floor 2 overheating flag
        floor_key = _FLOOR_ENTITIES.get(floor_eid, "")
        warn_flag = " ⚠️" if floor_key == "floor_2" and floor_on_since.get(floor_eid) else ""

        line = (
            f"  {label}: {temp_str} → {sp_str} setpoint ({action_label})"
            f" | today: {today_str}{warn_flag}"
        )
        lines.append(line)

    lines.append("")

    # Daily totals — include in-flight furnace session if currently running
    committed_runtime_s: int = daily_state.get("furnace_runtime_s", 0)
    in_flight_furnace_s = 0
    if furnace_on_since is not None:
        in_flight_furnace_s = int((now - furnace_on_since).total_seconds())
    total_runtime_s = committed_runtime_s + in_flight_furnace_s
    session_count: int = daily_state.get("session_count", 0)
    # If furnace is currently on, it's an active session not yet counted
    if furnace_on_since is not None:
        session_count += 1

    lines.append(
        f"📊 Today: {session_count} session{'s' if session_count != 1 else ''}, "
        f"{_fmt_duration(total_runtime_s)} total"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


def handle_telegram_commands(
    *,
    bot_token: str,
    chat_id: str,
    last_update_id: int | None,
    furnace_on_since: datetime | None,
    floor_on_since: dict[str, datetime | None],
    climate_state: dict[str, Any],
    daily_state: dict[str, Any],
    now: datetime | None = None,
) -> int | None:
    """
    Check for incoming Telegram commands and respond.

    Polls getUpdates with the given offset, processes any `/summary` commands,
    and returns the new last_update_id (or the existing one if nothing changed).

    Args:
        bot_token: Telegram bot token.
        chat_id: Telegram chat ID to respond to.
        last_update_id: The last processed update_id (used as offset+1).
        furnace_on_since: See format_status_summary.
        floor_on_since: See format_status_summary.
        climate_state: See format_status_summary.
        daily_state: See format_status_summary.
        now: Wall-clock datetime override (defaults to datetime.now(UTC)).

    Returns:
        Updated last_update_id, or the original value if no new updates were found.
    """
    if not bot_token:
        return last_update_id

    offset = (last_update_id + 1) if last_update_id is not None else None
    updates = get_updates(bot_token, offset=offset, timeout=0)

    new_last_update_id = last_update_id

    for update in updates:
        update_id: int = update.get("update_id", 0)
        if update_id > (new_last_update_id or -1):
            new_last_update_id = update_id

        message = update.get("message") or {}
        text: str = (message.get("text") or "").strip()

        # Only respond to /summary commands from the configured chat
        msg_chat_id = str((message.get("chat") or {}).get("id", ""))
        if msg_chat_id != str(chat_id):
            continue

        if text.startswith("/summary"):
            summary = format_status_summary(
                furnace_on_since=furnace_on_since,
                floor_on_since=floor_on_since,
                climate_state=climate_state,
                daily_state=daily_state,
                now=now,
            )
            send_message(bot_token, chat_id, summary)

    return new_last_update_id
