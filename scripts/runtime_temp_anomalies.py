#!/usr/bin/env python3
"""Find floor-day runtime outliers after adjusting for outdoor temperature.

The command reads ``floor_daily_summary.v1`` events from the derived JSONL log,
fits one ordinary-least-squares model per floor (runtime seconds as a function
of the day's average outdoor temperature), and reports unusually large positive
residuals.  A robust median-absolute-deviation scale is preferred so one
extreme day does not define its own alert threshold; sample standard deviation
is used only when the MAD is zero.  Lower-than-expected runtime is retained in
the model but is not treated as an operational candidate because a missing
heating call can be a normal setpoint or schedule decision.

This is an evidence report, not a fault diagnosis.  A candidate means that a
floor's recorded runtime was unusual for the temperature history in the
selected window.  It does not establish a mechanical cause, and the report
explicitly marks floors with insufficient history or no residual variation.

Usage (last 30 UTC days):
    python3 scripts/runtime_temp_anomalies.py --log state/consumer/events.jsonl

Usage with an explicit inclusive date range and Markdown output:
    python3 scripts/runtime_temp_anomalies.py \
        --start 2026-03-20 --end 2026-08-21 \
        --log state/consumer/events.jsonl --out reports/runtime-anomalies.md

Usage with JSON output:
    python3 scripts/runtime_temp_anomalies.py --days 90 --format json

Revision history:
  2026-08-22  Added deterministic temperature-adjusted floor-runtime anomaly
              analysis so cold- or warm-weather demand is separated from
              unusual floor behavior without changing the live consumer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

SCHEMA = "homeops.consumer.floor_daily_summary.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_DAYS = 30
DEFAULT_MIN_POINTS = 14
DEFAULT_THRESHOLD = 2.5
MAD_SCALE = 1.4826


@dataclass(frozen=True)
class FloorDay:
    """One valid floor daily summary used by the temperature model."""

    day: date
    floor: str
    outdoor_temp_f: float
    runtime_s: float


@dataclass(frozen=True)
class LinearModel:
    """Runtime-versus-temperature least-squares model for one floor."""

    intercept_s: float
    slope_s_per_f: float
    r_squared: float | None

    def predict(self, outdoor_temp_f: float) -> float:
        """Return the model's expected runtime for an outdoor temperature."""
        return self.intercept_s + self.slope_s_per_f * outdoor_temp_f


@dataclass(frozen=True)
class RuntimeAnomaly:
    """A floor-day whose model residual crossed the configured threshold."""

    day: str
    floor: str
    outdoor_temp_f: float
    actual_runtime_s: float
    expected_runtime_s: float
    residual_s: float
    score: float
    direction: str


def _finite_number(value: Any) -> float | None:
    """Return finite numeric JSON values, excluding booleans and strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_date(value: str) -> date:
    """Parse an ISO calendar date for argparse."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _resolve_range(
    days: int = DEFAULT_DAYS,
    start: date | None = None,
    end: date | None = None,
) -> tuple[date, date]:
    """Resolve an inclusive UTC date range from either dates or a day count."""
    if (start is None) != (end is None):
        raise ValueError("--start and --end must be provided together")
    if start is not None and end is not None:
        if start > end:
            raise ValueError("start date must be on or before end date")
        return start, end
    if days < 1:
        raise ValueError("days must be at least 1")
    end = datetime.now(UTC).date()
    return end - timedelta(days=days - 1), end


def load_floor_days(
    log_path: str | Path,
    start: date,
    end: date,
) -> list[FloorDay]:
    """Load valid, temperature-bearing floor summaries in an inclusive range.

    The summary's explicit ``data.date`` is the source of the UTC calendar day.
    If multiple records describe the same floor and date, the last valid record
    in JSONL order wins, matching the consumer's append-only event semantics
    and making replay behavior deterministic.
    """
    if start > end:
        raise ValueError("start date must be on or before end date")

    by_key: dict[tuple[str, date], FloorDay] = {}
    try:
        with open(log_path, encoding="utf-8") as events_file:
            for line in events_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("schema") != SCHEMA:
                    continue

                data = event.get("data")
                if not isinstance(data, dict):
                    continue
                floor = data.get("floor")
                day_value = data.get("date")
                if not isinstance(floor, str) or not floor or not isinstance(day_value, str):
                    continue
                try:
                    day = date.fromisoformat(day_value)
                except ValueError:
                    continue
                if day < start or day > end:
                    continue

                outdoor_temp_f = _finite_number(data.get("outdoor_temp_avg_f"))
                runtime_s = _finite_number(data.get("total_runtime_s"))
                if outdoor_temp_f is None or runtime_s is None or runtime_s < 0:
                    continue

                by_key[(floor, day)] = FloorDay(day, floor, outdoor_temp_f, runtime_s)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"log file not found: {log_path}") from exc
    except OSError as exc:
        raise OSError(f"error reading log {log_path}: {exc}") from exc

    return sorted(by_key.values(), key=lambda row: (row.floor, row.day))


def fit_linear_model(rows: Iterable[FloorDay]) -> LinearModel | None:
    """Fit runtime seconds against outdoor temperature, or return ``None``."""
    points = list(rows)
    if len(points) < 2:
        return None

    temperatures = [row.outdoor_temp_f for row in points]
    runtimes = [row.runtime_s for row in points]
    mean_temp = statistics.fmean(temperatures)
    mean_runtime = statistics.fmean(runtimes)
    denominator = sum((temperature - mean_temp) ** 2 for temperature in temperatures)
    if denominator == 0:
        return None

    slope = (
        sum(
            (temperature - mean_temp) * (runtime - mean_runtime)
            for temperature, runtime in zip(temperatures, runtimes)
        )
        / denominator
    )
    intercept = mean_runtime - slope * mean_temp
    predicted = [intercept + slope * temperature for temperature in temperatures]
    residual_sum_squares = sum(
        (runtime - expected) ** 2 for runtime, expected in zip(runtimes, predicted)
    )
    total_sum_squares = sum((runtime - mean_runtime) ** 2 for runtime in runtimes)
    r_squared = 1.0 - residual_sum_squares / total_sum_squares if total_sum_squares > 0 else None
    return LinearModel(intercept, slope, r_squared)


def _residual_scale(residuals: list[float]) -> tuple[float, float, str]:
    """Return ``(center, scale, method)`` for robust anomaly scoring."""
    center = float(statistics.median(residuals))
    deviations = [abs(residual - center) for residual in residuals]
    mad = float(statistics.median(deviations))
    if mad > 0:
        return center, MAD_SCALE * mad, "mad"

    if len(residuals) > 1:
        mean = statistics.fmean(residuals)
        sample_stddev = math.sqrt(
            sum((residual - mean) ** 2 for residual in residuals) / (len(residuals) - 1)
        )
        if sample_stddev > 0:
            return center, sample_stddev, "sample_stddev_fallback"

    return center, 0.0, "none"


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _model_dict(model: LinearModel | None) -> dict[str, float | None] | None:
    if model is None:
        return None
    return {
        "intercept_s": _round(model.intercept_s),
        "slope_s_per_f": _round(model.slope_s_per_f),
        "r_squared": _round(model.r_squared, 3),
    }


def _anomaly_dict(anomaly: RuntimeAnomaly) -> dict[str, Any]:
    return asdict(anomaly)


def build_report(
    rows: Iterable[FloorDay],
    start: date,
    end: date,
    *,
    min_points: int = DEFAULT_MIN_POINTS,
    threshold: float = DEFAULT_THRESHOLD,
    source: str = "events.jsonl",
) -> dict[str, Any]:
    """Build a JSON-serializable temperature-adjusted anomaly report."""
    if start > end:
        raise ValueError("start date must be on or before end date")
    if min_points < 2:
        raise ValueError("min_points must be at least 2")
    if threshold <= 0:
        raise ValueError("threshold must be greater than 0")

    grouped: defaultdict[str, list[FloorDay]] = defaultdict(list)
    for row in rows:
        if start <= row.day <= end:
            grouped[row.floor].append(row)

    floor_reports: list[dict[str, Any]] = []
    all_anomalies: list[dict[str, Any]] = []
    for floor in sorted(grouped):
        floor_rows = sorted(grouped[floor], key=lambda row: row.day)
        model = fit_linear_model(floor_rows) if len(floor_rows) >= min_points else None
        if model is None:
            floor_reports.append(
                {
                    "floor": floor,
                    "sample_count": len(floor_rows),
                    "date_range": {
                        "first": floor_rows[0].day.isoformat() if floor_rows else None,
                        "last": floor_rows[-1].day.isoformat() if floor_rows else None,
                    },
                    "model": None,
                    "residual_center_s": None,
                    "residual_scale_s": None,
                    "scale_method": "none",
                    "status": "insufficient_data",
                    "anomalies": [],
                }
            )
            continue

        residuals = [row.runtime_s - model.predict(row.outdoor_temp_f) for row in floor_rows]
        center, scale, scale_method = _residual_scale(residuals)
        anomalies: list[RuntimeAnomaly] = []
        if scale > 0:
            for row, residual in zip(floor_rows, residuals):
                score = (residual - center) / scale
                if residual <= center or score < threshold:
                    continue
                expected = model.predict(row.outdoor_temp_f)
                anomalies.append(
                    RuntimeAnomaly(
                        day=row.day.isoformat(),
                        floor=floor,
                        outdoor_temp_f=round(row.outdoor_temp_f, 1),
                        actual_runtime_s=round(row.runtime_s, 1),
                        expected_runtime_s=round(expected, 1),
                        residual_s=round(residual, 1),
                        score=round(score, 2),
                        direction=(
                            "higher_than_expected" if residual > center else "lower_than_expected"
                        ),
                    )
                )

        status = "ok" if scale > 0 else "no_residual_variation"
        floor_report = {
            "floor": floor,
            "sample_count": len(floor_rows),
            "date_range": {
                "first": floor_rows[0].day.isoformat(),
                "last": floor_rows[-1].day.isoformat(),
            },
            "model": _model_dict(model),
            "residual_center_s": round(center, 1),
            "residual_scale_s": round(scale, 1),
            "scale_method": scale_method,
            "status": status,
            "anomalies": [_anomaly_dict(anomaly) for anomaly in anomalies],
        }
        floor_reports.append(floor_report)
        all_anomalies.extend(floor_report["anomalies"])

    return {
        "schema": "homeops.runtime-temperature-anomalies.v1",
        "source": source,
        "method": (
            "Per-floor ordinary least squares of daily runtime seconds versus average outdoor "
            "temperature; anomaly scores use median-centered residuals and MAD when available."
        ),
        "coverage": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "valid_floor_days": sum(len(values) for values in grouped.values()),
            "floors": len(grouped),
        },
        "configuration": {
            "min_points": min_points,
            "threshold_score": threshold,
            "mad_scale_factor": MAD_SCALE,
            "runtime_units": "seconds per UTC calendar day",
        },
        "candidate_anomaly_count": len(all_anomalies),
        "floors": floor_reports,
        "anomalies": sorted(all_anomalies, key=lambda item: (item["day"], item["floor"])),
        "interpretation_guard": (
            "A candidate is unusual recorded runtime for this temperature history, not proof of "
            "an equipment fault. Confirm against thermostat state, sensor quality, and maintenance "
            "history before acting."
        ),
    }


def _fmt_seconds(value: float | int | None) -> str:
    """Format seconds compactly for Markdown output."""
    if value is None:
        return "—"
    seconds = int(round(float(value)))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{sign}{hours}h {minutes:02d}m"
    return f"{sign}{minutes}m {seconds:02d}s"


def _fmt_number(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def render_markdown(report: dict[str, Any], file: TextIO | None = None) -> str:
    """Render a compact human-readable report."""
    lines = [
        "# Temperature-adjusted floor runtime anomalies",
        "",
        f"Source: `{report['source']}`",
        (
            f"Coverage: `{report['coverage']['start']}` → `{report['coverage']['end']}`; "
            f"{report['coverage']['valid_floor_days']} valid floor-days"
        ),
        "",
        "The model estimates expected daily floor runtime from average outdoor temperature.",
        "Candidates are unusually high positive residuals, not confirmed equipment faults.",
        "",
        "## Floor models",
        "",
        "| Floor | Samples | Slope (s/°F) | Residual scale | Candidates | Status |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for floor in report["floors"]:
        model = floor["model"] or {}
        lines.append(
            f"| {floor['floor']} | {floor['sample_count']} | "
            f"{_fmt_number(model.get('slope_s_per_f'))} | "
            f"{_fmt_seconds(floor['residual_scale_s'])} | "
            f"{len(floor['anomalies'])} | {floor['status']} |"
        )

    lines.extend(["", "## Candidate anomalies", ""])
    if report["anomalies"]:
        lines.extend(
            [
                "| Date | Floor | Outdoor | Actual runtime | Expected runtime | Residual | "
                "Score | Direction |",
                "|---|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for anomaly in report["anomalies"]:
            lines.append(
                f"| {anomaly['day']} | {anomaly['floor']} | {anomaly['outdoor_temp_f']:.1f}°F | "
                f"{_fmt_seconds(anomaly['actual_runtime_s'])} | "
                f"{_fmt_seconds(anomaly['expected_runtime_s'])} | "
                f"{_fmt_seconds(anomaly['residual_s'])} | {anomaly['score']:.2f} | "
                f"{anomaly['direction']} |"
            )
    else:
        lines.append("No temperature-adjusted anomalies crossed the configured threshold.")

    lines.extend(
        [
            "",
            "## Interpretation guard",
            "",
            report["interpretation_guard"],
            "",
        ]
    )
    output = "\n".join(lines).rstrip("\n")
    if file is not None:
        print(output, file=file)
    return output


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive number: {value}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _minimum_points(value: str) -> int:
    """Parse a model sample-size minimum, which must support a line fit."""
    parsed = _positive_int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("min-points must be at least 2")
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=_positive_int,
        default=DEFAULT_DAYS,
        help=f"Number of trailing UTC days to include (default: {DEFAULT_DAYS})",
    )
    parser.add_argument("--start", type=_parse_date, help="Inclusive UTC start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="Inclusive UTC end date (YYYY-MM-DD)")
    parser.add_argument(
        "--min-points",
        type=_minimum_points,
        default=DEFAULT_MIN_POINTS,
        help=f"Minimum floor-days required for a model (default: {DEFAULT_MIN_POINTS})",
    )
    parser.add_argument(
        "--threshold",
        type=_positive_float,
        default=DEFAULT_THRESHOLD,
        help=f"Absolute residual score required for a candidate (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Path to derived event JSONL (overrides DERIVED_EVENT_LOG)",
    )
    parser.add_argument("--out", help="Optional output path; defaults to stdout")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the report CLI."""
    args = _parse_args(argv)
    try:
        start, end = _resolve_range(args.days, args.start, args.end)
        log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
        rows = load_floor_days(log_path, start, end)
        report = build_report(
            rows,
            start,
            end,
            min_points=args.min_points,
            threshold=args.threshold,
            source=str(log_path),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(
            f"Report written → {output_path} "
            f"({report['candidate_anomaly_count']} candidate anomalies)"
        )
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
