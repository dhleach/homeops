#!/usr/bin/env python3
"""Stream Home Assistant state and mitigation events to JSONL.

Revision history:
  2026-08-25  Subscribe to and preserve the staged mitigation rollback event so
              the consumer can durably alert on automatic fail-safe shutdown.
  2026-08-25  Subscribe to the staged mitigation event and preserve it in a
              generic observer envelope so the consumer can durably record the
              applied/skipped decision and replay it after a restart.
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import websockets
from dotenv import load_dotenv
from log_config import get_logger

logger = get_logger("observer")

_STATE_CHANGED_EVENT_TYPE = "state_changed"
_MITIGATION_EVENT_TYPE = "homeops.mitigation.zone_stagger_applied.v1"
_MITIGATION_ROLLBACK_EVENT_TYPE = "homeops.mitigation.rollback.v1"
_MITIGATION_EVENT_TYPES = (_MITIGATION_EVENT_TYPE, _MITIGATION_ROLLBACK_EVENT_TYPE)
_STATE_CHANGED_SCHEMA = "homeops.observer.state_changed.v1"
_EVENT_SCHEMA = "homeops.observer.event.v1"


def utc_ts():
    return datetime.now(UTC).isoformat()


def _build_observer_record(
    event: dict[str, Any],
    watch: set[str],
    *,
    timestamp: str | None = None,
) -> dict[str, Any] | None:
    """Convert one Home Assistant event into an observer JSONL record."""
    event_type = event.get("event_type")
    record_ts = timestamp or utc_ts()

    if event_type == _STATE_CHANGED_EVENT_TYPE:
        data = event.get("data") or {}
        if not isinstance(data, dict):
            return None
        entity_id = data.get("entity_id")
        if not entity_id or (watch and entity_id not in watch):
            return None

        new_state = data.get("new_state") or {}
        old_state = data.get("old_state") or {}
        if not isinstance(new_state, dict) or not isinstance(old_state, dict):
            return None

        event_data: dict[str, Any] = {
            "entity_id": entity_id,
            "old_state": old_state.get("state"),
            "new_state": new_state.get("state"),
        }
        attributes = new_state.get("attributes") or {}
        if attributes:
            event_data["attributes"] = attributes

        return {
            "schema": _STATE_CHANGED_SCHEMA,
            "source": "ha.websocket",
            "ts": record_ts,
            "data": event_data,
        }

    if event_type in _MITIGATION_EVENT_TYPES:
        context = event.get("context") or {}
        wrapper_data: dict[str, Any] = {
            "event_type": event_type,
            "event_data": event.get("data"),
        }
        if isinstance(context, dict) and context.get("id"):
            wrapper_data["context_id"] = context["id"]
        return {
            "schema": _EVENT_SCHEMA,
            "source": "ha.websocket",
            "ts": record_ts,
            "data": wrapper_data,
        }

    return None


def _get_version() -> str:
    """Return the current git version as <short_hash>-<YYYY-MM-DD>, or "unknown" if unavailable."""
    try:
        import subprocess as _subprocess

        return (
            _subprocess.check_output(
                ["git", "-C", str(Path(__file__).parent), "log", "-1", "--format=%h-%as"],
                stderr=_subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


async def main():
    """Stream Home Assistant state and selected event-bus records to JSONL."""
    # Load dotenv values first so explicit process env vars can still override them.
    # Note: default path is relative to the current working directory.
    env_path = os.environ.get("HA_ENV_FILE", "secrets/ha.env")
    load_dotenv(env_path)

    version = _get_version()
    logger.info(f"Observer version: {version}")
    os.makedirs("state/observer", exist_ok=True)
    with open("state/observer/version.txt", "w", encoding="utf-8") as _vf:
        _vf.write(version + "\n")

    ws_url = os.environ.get("HA_WS_URL")
    token = os.environ.get("HA_TOKEN")
    watch_raw = os.environ.get("WATCH_ENTITIES", "")
    event_log = os.environ.get("OBSERVER_EVENT_LOG")

    if not ws_url or not token:
        logger.error(f"Missing HA_WS_URL or HA_TOKEN in {env_path}")
        sys.exit(2)

    watch = set(e.strip() for e in watch_raw.split(",") if e.strip())

    backoff_s = 1
    max_backoff_s = 30

    # Keep the process alive forever; any disconnect/error falls back to reconnect.
    while True:
        try:
            logger.info(f"Connecting to {ws_url}")
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                backoff_s = 1  # reset backoff once connected

                # 1) HA sends auth_required
                hello = json.loads(await ws.recv())
                if hello.get("type") != "auth_required":
                    raise RuntimeError(f"Unexpected hello: {hello}")

                # 2) Send auth
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                auth_resp = json.loads(await ws.recv())
                if auth_resp.get("type") != "auth_ok":
                    raise RuntimeError(f"Auth failed: {auth_resp}")

                logger.info("Auth OK")

                # 3) Subscribe to state_changed
                sub_id = 1
                await ws.send(
                    json.dumps(
                        {
                            "id": sub_id,
                            "type": "subscribe_events",
                            "event_type": _STATE_CHANGED_EVENT_TYPE,
                        }
                    )
                )
                sub_resp = json.loads(await ws.recv())
                if not sub_resp.get("success", False):
                    raise RuntimeError(f"Subscribe failed: {sub_resp}")

                # 4) Subscribe to explicit mitigation events.  They are
                # intentionally separate from state_changed so the observer
                # never has to infer a mitigation decision from thermostat state.
                for sub_id, mitigation_event_type in enumerate(_MITIGATION_EVENT_TYPES, start=2):
                    await ws.send(
                        json.dumps(
                            {
                                "id": sub_id,
                                "type": "subscribe_events",
                                "event_type": mitigation_event_type,
                            }
                        )
                    )
                    mitigation_sub_resp = json.loads(await ws.recv())
                    if not mitigation_sub_resp.get("success", False):
                        raise RuntimeError(f"Subscribe failed: {mitigation_sub_resp}")

                logger.info(
                    "Subscribed to state_changed and "
                    f"{', '.join(sorted(_MITIGATION_EVENT_TYPES))}. "
                    f"Watching: {', '.join(sorted(watch)) if watch else '(ALL)'}"
                )

                # 5) Print matching events
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)

                    # The HA websocket sends other message types (pong/result/etc).
                    if msg.get("type") != "event":
                        continue

                    event = msg.get("event", {})
                    if not isinstance(event, dict):
                        continue
                    out = _build_observer_record(event, watch)
                    if out is None:
                        continue
                    line = json.dumps(out)
                    # Stdout is the primary event stream for pipes/consumers.
                    print(line, flush=True)
                    if event_log:
                        try:
                            # Best-effort local append copy; failures should not stop streaming.
                            Path(event_log).parent.mkdir(parents=True, exist_ok=True)
                            with open(event_log, "a", encoding="utf-8") as f:
                                f.write(line + "\n")
                        except OSError as e:
                            logger.warning(f"WARN: failed to append to {event_log}: {e}")

        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            logger.warning(f"Disconnected: {e.__class__.__name__}: {e}")
        except Exception as e:
            logger.error(f"Error: {e.__class__.__name__}: {e}")

        logger.info(f"Reconnecting in {backoff_s}s...")
        await asyncio.sleep(backoff_s)
        backoff_s = min(max_backoff_s, backoff_s * 2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped")
