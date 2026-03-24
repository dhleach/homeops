#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets
from dotenv import load_dotenv

_OUTDOOR_ENTITY = "sensor.outdoor_temperature"
_OUTDOOR_POLL_INTERVAL_S = 3600  # 60 min — guarantees consumer state is never > 62 min stale


def utc_ts():
    return datetime.now(UTC).isoformat()


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


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def _emit_fetched_state(state_obj: dict, event_log: str | None) -> None:
    """Emit a synthetic state_changed.v1 event from a get_states result object."""
    entity_id = state_obj.get("entity_id", "")
    state_val = state_obj.get("state")
    attrs = state_obj.get("attributes") or {}
    event_data: dict = {
        "entity_id": entity_id,
        "old_state": state_val,
        "new_state": state_val,
    }
    if attrs:
        event_data["attributes"] = attrs
    out = {
        "schema": "homeops.observer.state_changed.v1",
        "source": "ha.websocket",
        "ts": utc_ts(),
        "data": event_data,
    }
    line = json.dumps(out)
    print(line, flush=True)
    if event_log:
        try:
            Path(event_log).parent.mkdir(parents=True, exist_ok=True)
            with open(event_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            eprint(f"[{utc_ts()}] WARN: failed to append to {event_log}: {e}")


async def main():
    """Stream Home Assistant state changes to stdout (and optional JSONL file)."""
    # Load dotenv values first so explicit process env vars can still override them.
    # Note: default path is relative to the current working directory.
    env_path = os.environ.get("HA_ENV_FILE", "secrets/ha.env")
    load_dotenv(env_path)

    print(f"[{utc_ts()}] Observer version: {_get_version()}", flush=True)

    ws_url = os.environ.get("HA_WS_URL")
    token = os.environ.get("HA_TOKEN")
    watch_raw = os.environ.get("WATCH_ENTITIES", "")
    event_log = os.environ.get("OBSERVER_EVENT_LOG")

    if not ws_url or not token:
        eprint(f"[{utc_ts()}] Missing HA_WS_URL or HA_TOKEN in {env_path}")
        sys.exit(2)

    watch = set(e.strip() for e in watch_raw.split(",") if e.strip())

    backoff_s = 1
    max_backoff_s = 30

    # Keep the process alive forever; any disconnect/error falls back to reconnect.
    while True:
        try:
            eprint(f"[{utc_ts()}] Connecting to {ws_url}")
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

                eprint(f"[{utc_ts()}] Auth OK")

                # 3) Subscribe to state_changed
                sub_id = 1
                await ws.send(
                    json.dumps(
                        {
                            "id": sub_id,
                            "type": "subscribe_events",
                            "event_type": "state_changed",
                        }
                    )
                )
                sub_resp = json.loads(await ws.recv())
                if not sub_resp.get("success", False):
                    raise RuntimeError(f"Subscribe failed: {sub_resp}")

                eprint(
                    f"[{utc_ts()}] Subscribed. Watching: "
                    + (", ".join(sorted(watch)) if watch else "(ALL)")
                )

                # 4) Print matching state changes; periodically fetch outdoor temp
                req_counter = 1  # 1 is already used for subscribe
                pending_states_id: int | None = None
                # Fetch immediately on connect so consumer gets an up-to-date value.
                next_outdoor_poll: float = time.time()

                while True:
                    now = time.time()
                    # Kick off a periodic outdoor-temp poll (via get_states).
                    if now >= next_outdoor_poll and pending_states_id is None:
                        req_counter += 1
                        pending_states_id = req_counter
                        await ws.send(json.dumps({"id": req_counter, "type": "get_states"}))
                        next_outdoor_poll = now + _OUTDOOR_POLL_INTERVAL_S

                    recv_timeout = max(1.0, next_outdoor_poll - time.time())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                        msg = json.loads(raw)
                    except TimeoutError:
                        continue

                    # Handle get_states result — emit outdoor temp then discard.
                    if msg.get("type") == "result":
                        if msg.get("id") == pending_states_id and msg.get("success"):
                            pending_states_id = None
                            for state_obj in msg.get("result") or []:
                                if state_obj.get("entity_id") == _OUTDOOR_ENTITY:
                                    _emit_fetched_state(state_obj, event_log)
                                    break
                        continue

                    # The HA websocket sends other message types (pong/result/etc).
                    if msg.get("type") != "event":
                        continue

                    event = msg.get("event", {})
                    data = event.get("data", {})
                    entity_id = data.get("entity_id")

                    if not entity_id:
                        continue
                    # Optional allowlist filter; empty WATCH_ENTITIES means "all entities".
                    if watch and entity_id not in watch:
                        continue

                    new_state = data.get("new_state") or {}
                    old_state = data.get("old_state") or {}

                    event_data = {
                        "entity_id": entity_id,
                        "old_state": old_state.get("state"),
                        "new_state": new_state.get("state"),
                    }
                    attributes = new_state.get("attributes") or {}
                    if attributes:
                        event_data["attributes"] = attributes

                    out = {
                        # Stable envelope for downstream consumers.
                        "schema": "homeops.observer.state_changed.v1",
                        "source": "ha.websocket",
                        "ts": utc_ts(),
                        "data": event_data,
                    }
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
                            eprint(f"[{utc_ts()}] WARN: failed to append to {event_log}: {e}")

        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            eprint(f"[{utc_ts()}] Disconnected: {e.__class__.__name__}: {e}")
        except Exception as e:
            eprint(f"[{utc_ts()}] Error: {e.__class__.__name__}: {e}")

        eprint(f"[{utc_ts()}] Reconnecting in {backoff_s}s...")
        await asyncio.sleep(backoff_s)
        backoff_s = min(max_backoff_s, backoff_s * 2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        eprint(f"[{utc_ts()}] Stopped")
