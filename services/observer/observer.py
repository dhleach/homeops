#!/usr/bin/env python3
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv


def utc_ts():
    return datetime.now(timezone.utc).isoformat()


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


async def main():
    # Load env vars from secrets file (relative to repo root)
    env_path = os.environ.get("HA_ENV_FILE", "secrets/ha.env")
    load_dotenv(env_path)

    ws_url = os.environ.get("HA_WS_URL")
    token = os.environ.get("HA_TOKEN")
    watch_raw = os.environ.get("WATCH_ENTITIES", "")

    if not ws_url or not token:
        eprint(f"[{utc_ts()}] Missing HA_WS_URL or HA_TOKEN in {env_path}")
        sys.exit(2)

    watch = set(e.strip() for e in watch_raw.split(",") if e.strip())

    backoff_s = 1
    max_backoff_s = 30

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
                        {"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}
                    )
                )
                sub_resp = json.loads(await ws.recv())
                if not sub_resp.get("success", False):
                    raise RuntimeError(f"Subscribe failed: {sub_resp}")

                eprint(
                    f"[{utc_ts()}] Subscribed. Watching: "
                    + (", ".join(sorted(watch)) if watch else "(ALL)")
                )

                # 4) Print matching state changes
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") != "event":
                        continue

                    event = msg.get("event", {})
                    data = event.get("data", {})
                    entity_id = data.get("entity_id")

                    if not entity_id:
                        continue
                    if watch and entity_id not in watch:
                        continue

                    new_state = data.get("new_state") or {}
                    old_state = data.get("old_state") or {}

                    out = {
                        "ts": utc_ts(),
                        "entity_id": entity_id,
                        "old_state": old_state.get("state"),
                        "new_state": new_state.get("state"),
                    }
                    print(json.dumps(out), flush=True)

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