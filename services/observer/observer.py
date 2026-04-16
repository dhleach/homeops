#!/usr/bin/env python3
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import websockets
from dotenv import load_dotenv
from log_config import get_logger

logger = get_logger("observer")


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


async def main():
    """Stream Home Assistant state changes to stdout (and optional JSONL file)."""
    # Load dotenv values first so explicit process env vars can still override them.
    # Note: default path is relative to the current working directory.
    env_path = os.environ.get("HA_ENV_FILE", "secrets/ha.env")
    load_dotenv(env_path)

    version = _get_version()
    print(f"[{utc_ts()}] Observer version: {version}", flush=True)
    os.makedirs("state/observer", exist_ok=True)
    with open("state/observer/version.txt", "w", encoding="utf-8") as _vf:
        _vf.write(version + "\n")

    ws_url = os.environ.get("HA_WS_URL")
    token = os.environ.get("HA_TOKEN")
    watch_raw = os.environ.get("WATCH_ENTITIES", "")
    event_log = os.environ.get("OBSERVER_EVENT_LOG")

    if not ws_url or not token:
        logger.error("Missing HA_WS_URL or HA_TOKEN in %s", env_path)
        sys.exit(2)

    watch = set(e.strip() for e in watch_raw.split(",") if e.strip())

    backoff_s = 1
    max_backoff_s = 30

    # Keep the process alive forever; any disconnect/error falls back to reconnect.
    while True:
        try:
            logger.info("Connecting to %s", ws_url)
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
                            "event_type": "state_changed",
                        }
                    )
                )
                sub_resp = json.loads(await ws.recv())
                if not sub_resp.get("success", False):
                    raise RuntimeError(f"Subscribe failed: {sub_resp}")

                logger.info(
                    "Subscribed. Watching: %s",
                    (", ".join(sorted(watch)) if watch else "(ALL)"),
                )

                # 4) Print matching state changes
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)

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
                            logger.warning("WARN: failed to append to %s: %s", event_log, e)

        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            logger.warning("Disconnected: %s: %s", e.__class__.__name__, e)
        except Exception as e:
            logger.error("Error: %s: %s", e.__class__.__name__, e)

        logger.info("Reconnecting in %ss...", backoff_s)
        await asyncio.sleep(backoff_s)
        backoff_s = min(max_backoff_s, backoff_s * 2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped")
