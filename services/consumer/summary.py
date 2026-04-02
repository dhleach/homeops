#!/usr/bin/env python3
"""
HomeOps summary CLI.

Usage examples
--------------
Show week-over-week comparison (reads events from Pi via SSH):
  python3 summary.py --week

Monthly aggregate for January 2026:
  python3 summary.py --month 2026-01

Read from a local JSONL file instead of the default Pi path:
  python3 summary.py --week --events-file /path/to/events.jsonl
  python3 summary.py --month 2026-01 --events-file /path/to/events.jsonl
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from monthly import (
    compute_monthly_summary,
    format_monthly_summary,
)
from monthly import (
    load_daily_summaries as load_monthly_summaries,
)
from weekly import compute_weekly_comparison, format_weekly_comparison, load_daily_summaries

# ---------------------------------------------------------------------------
# Default paths / SSH config
# ---------------------------------------------------------------------------

_DEFAULT_SSH_KEY = "/home/node/.openclaw/home-config/.ssh/id_ed25519"
_DEFAULT_SSH_HOST = "bob@100.115.21.72"
_DEFAULT_REMOTE_EVENTS = "/home/leachd/repos/homeops/state/consumer/events.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_events_via_ssh(
    remote_path: str = _DEFAULT_REMOTE_EVENTS,
    ssh_key: str = _DEFAULT_SSH_KEY,
    ssh_host: str = _DEFAULT_SSH_HOST,
) -> str:
    """SCP the remote events file to a local temp file and return its path.

    Raises RuntimeError on failure.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    cmd = [
        "scp",
        "-i",
        ssh_key,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        f"{ssh_host}:{remote_path}",
        tmp.name,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"scp failed (rc={result.returncode}): {result.stderr.strip()}")
    return tmp.name


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="summary",
        description="HomeOps summary CLI — view furnace statistics.",
    )
    parser.add_argument(
        "--week",
        action="store_true",
        help=(
            "Show week-over-week comparison for furnace runtime, sessions, and per-floor averages."
        ),
    )
    parser.add_argument(
        "--month",
        metavar="YYYY-MM",
        default=None,
        help=(
            "Show monthly aggregate for the given month (e.g. 2026-01). "
            "Sums runtime, sessions, per-floor breakdown, outdoor temp, and warnings."
        ),
    )
    parser.add_argument(
        "--events-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to a local events.jsonl file. "
            f"If omitted, fetches from the Pi via SSH ({_DEFAULT_REMOTE_EVENTS})."
        ),
    )
    parser.add_argument(
        "--ssh-key",
        metavar="PATH",
        default=_DEFAULT_SSH_KEY,
        help=f"SSH private key for Pi access (default: {_DEFAULT_SSH_KEY}).",
    )
    parser.add_argument(
        "--ssh-host",
        metavar="USER@HOST",
        default=_DEFAULT_SSH_HOST,
        help=f"SSH host for Pi access (default: {_DEFAULT_SSH_HOST}).",
    )
    parser.add_argument(
        "--remote-events",
        metavar="PATH",
        default=_DEFAULT_REMOTE_EVENTS,
        help=f"Remote events.jsonl path on the Pi (default: {_DEFAULT_REMOTE_EVENTS}).",
    )
    return parser


def cmd_week(args: argparse.Namespace) -> int:
    """Handle --week flag.  Returns exit code."""
    _tmp_to_delete: str | None = None

    if args.events_file:
        events_file = args.events_file
        if not Path(events_file).exists():
            print(f"Error: events file not found: {events_file}", file=sys.stderr)
            return 1
    else:
        print(f"Fetching events from Pi ({args.ssh_host}:{args.remote_events}) …")
        try:
            events_file = _fetch_events_via_ssh(
                remote_path=args.remote_events,
                ssh_key=args.ssh_key,
                ssh_host=args.ssh_host,
            )
            _tmp_to_delete = events_file
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    try:
        summaries = load_daily_summaries(events_file)
        if not summaries:
            print("No furnace_daily_summary.v1 events found in the events file.")
            return 0

        result = compute_weekly_comparison(summaries)
        if result is None:
            print(
                f"Not enough data: found {len(summaries)} day(s) of summaries."
                " Need at least 7 days for a weekly comparison."
            )
            return 0

        print()
        print(format_weekly_comparison(result))
        print()
        return 0
    finally:
        if _tmp_to_delete:
            try:
                Path(_tmp_to_delete).unlink()
            except OSError:
                pass


def cmd_month(args: argparse.Namespace) -> int:
    """Handle --month flag. Returns exit code."""
    _tmp_to_delete: str | None = None

    # Validate month format
    month = args.month
    try:
        parts = month.split("-")
        if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
            raise ValueError
        int(parts[0])
        int(parts[1])
    except (ValueError, AttributeError):
        print(f"Error: --month must be in YYYY-MM format (got {month!r})", file=sys.stderr)
        return 1

    if args.events_file:
        events_file = args.events_file
        if not Path(events_file).exists():
            print(f"Error: events file not found: {events_file}", file=sys.stderr)
            return 1
    else:
        print(f"Fetching events from Pi ({args.ssh_host}:{args.remote_events}) …")
        try:
            events_file = _fetch_events_via_ssh(
                remote_path=args.remote_events,
                ssh_key=args.ssh_key,
                ssh_host=args.ssh_host,
            )
            _tmp_to_delete = events_file
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    try:
        summaries = load_monthly_summaries(events_file)
        stats = compute_monthly_summary(summaries, month)

        if stats.day_count == 0:
            print(f"No furnace_daily_summary.v1 data found for {month}.")
            return 0

        print()
        print(format_monthly_summary(stats))
        print()
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        if _tmp_to_delete:
            try:
                Path(_tmp_to_delete).unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.week:
        return cmd_week(args)

    if args.month:
        return cmd_month(args)

    # No subcommand — show help
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
