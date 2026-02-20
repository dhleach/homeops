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

    eprint(f"[{utc_ts()}] Connecting to {ws_url}")
    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
        # 1) HA sends auth_required
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            eprint(f"[{utc_ts()}] Unexpected hello: {hello}")
            sys.exit(3)

        # 2) Send auth
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            eprint(f"[{utc_ts()}] Auth failed: {auth_resp}")
            sys.exit(4)

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
            eprint(f"[{utc_ts()}] Subscribe failed: {sub_resp}")
            sys.exit(5)

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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        eprint(f"[{utc_ts()}] Stopped")