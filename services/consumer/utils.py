"""Shared utility functions for the HomeOps consumer service."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dateutil.parser import isoparse

# Add insights rules to path for floor_no_response rule
sys.path.insert(0, str(Path(__file__).parent.parent / "insights"))


def utc_ts() -> str:
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


def follow(path: str, timeout_s: float = 60.0) -> Generator[str | None, None, None]:
    """Yield new lines as they are appended to a file, or yield None on timeout.

    Uses readline() polling with a short sleep to avoid busy-waiting.
    select.select() is NOT used because on Linux it always returns immediately
    for regular files (they are always "ready"), causing a 100% CPU spin when
    the consumer is caught up to EOF with no new events.
    """
    import time as _time

    _POLL_INTERVAL_S = 0.1  # check for new lines every 100 ms

    with open(path, encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        last_yield_ts = _time.monotonic()
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
                last_yield_ts = _time.monotonic()
            else:
                now = _time.monotonic()
                if now - last_yield_ts >= timeout_s:
                    # Timeout — no new events. Yield None so caller can run periodic checks.
                    yield None
                    last_yield_ts = _time.monotonic()
                else:
                    _time.sleep(_POLL_INTERVAL_S)


def append_jsonl(path: str, obj: dict[str, Any]) -> None:
    """Shared helper so all derived events are emitted in consistent JSONL format."""
    line = json.dumps(obj)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    try:
        return isoparse(s)
    except Exception:
        return None
