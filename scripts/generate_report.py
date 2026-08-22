#!/usr/bin/env python3
"""Generate a self-contained HTML report of historical HVAC trends.

The report composes the existing floor-daily-summary and furnace-scatter
parsers. It renders inline SVG charts, so the output opens from a local file
without a running service, JavaScript bundle, or external network request.
Missing values remain gaps in the charts and ``—`` in the data table.

Usage:
    python3 scripts/generate_report.py --days 30
    python3 scripts/generate_report.py \
        --start 2026-03-20 --end 2026-08-21 \
        --log state/consumer/events.jsonl \
        --out reports/hvac_trend.html

Revision history:
  2026-08-22  Added deterministic offline HTML trend reporting that combines
              per-floor runtime lines, whole-furnace temperature/runtime scatter
              data, explicit coverage counts, and a transparent data table.
"""

from __future__ import annotations

import argparse
import html
import math
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from floor_runtime_trend import KNOWN_FLOORS, load_floor_summaries
from furnace_temp_scatter import DailyScatterPoint, build_scatter_points

DEFAULT_DAYS = 30
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_OUT = "reports/hvac_trend.html"
FLOOR_COLORS = {
    "floor_1": "#38bdf8",
    "floor_2": "#f59e0b",
    "floor_3": "#a78bfa",
}
SVG_WIDTH = 960
SVG_HEIGHT = 360
SVG_MARGIN = (58, 22, 58, 46)  # left, right, top, bottom


@dataclass(frozen=True)
class ReportData:
    """Normalized chart data for one inclusive UTC date range."""

    dates: tuple[str, ...]
    floor_runtime_min: dict[str, tuple[float | None, ...]]
    scatter_points: tuple[DailyScatterPoint, ...]

    @property
    def complete_scatter_points(self) -> tuple[DailyScatterPoint, ...]:
        """Return points with both measurements available for plotting."""
        return tuple(
            point
            for point in self.scatter_points
            if point.avg_temp_f is not None and point.furnace_runtime_min is not None
        )

    @property
    def partial_scatter_rows(self) -> int:
        """Return represented scatter rows missing at least one measurement."""
        return len(self.scatter_points) - len(self.complete_scatter_points)


def _finite_nonnegative(value: object) -> float | None:
    """Convert a non-negative finite number to float; preserve missing data."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _runtime_minutes(data: dict) -> float | None:
    """Convert a summary runtime in seconds into chart minutes."""
    seconds = _finite_nonnegative(data.get("total_runtime_s"))
    return None if seconds is None else round(seconds / 60, 1)


def build_report_data(log_path: str | Path, start: date, end: date) -> ReportData:
    """Load and normalize all data required by the report."""
    if start > end:
        raise ValueError("start date must be on or before end date")

    floor_rows = load_floor_summaries(str(log_path), start, end)
    scatter_points = tuple(build_scatter_points(log_path, start=start, end=end))
    dates = tuple(sorted(set(floor_rows) | {point.date for point in scatter_points}))

    floor_runtime_min = {
        floor: tuple(
            _runtime_minutes(floor_rows.get(date_str, {}).get(floor, {})) for date_str in dates
        )
        for floor in KNOWN_FLOORS
    }
    return ReportData(dates, floor_runtime_min, scatter_points)


def _esc(value: object) -> str:
    """Escape text for HTML and SVG markup."""
    return html.escape(str(value), quote=True)


def _fmt_number(value: float | None, suffix: str = "") -> str:
    """Format optional chart/table numbers consistently."""
    return "—" if value is None else f"{value:.1f}{suffix}"


def _scale(value: float, low: float, high: float, start: float, size: float) -> float:
    """Map a data value into a chart coordinate."""
    if high == low:
        return start + size / 2
    return start + (value - low) / (high - low) * size


def _svg_text(x: float, y: float, text: str, *, anchor: str = "start", cls: str = "") -> str:
    """Return a safely escaped SVG text node."""
    class_attr = f' class="{cls}"' if cls else ""
    return f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"{class_attr}>{_esc(text)}</text>'


def _axis_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Return evenly spaced axis ticks, including both bounds."""
    if count < 2 or high == low:
        return [low]
    return [low + (high - low) * index / (count - 1) for index in range(count)]


def _svg_shell(content: str, title: str) -> str:
    """Wrap chart elements in an accessible responsive SVG."""
    return (
        f'<svg class="chart" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
        f'role="img" aria-label="{_esc(title)}">'
        f"<title>{_esc(title)}</title>{content}</svg>"
    )


def _chart_frame(
    y_low: float,
    y_high: float,
    y_suffix: str,
    *,
    x_labels: Iterable[str] = (),
    x_values: Iterable[float] | None = None,
    x_low: float | None = None,
    x_high: float | None = None,
    x_suffix: str = "",
) -> tuple[list[str], float, float, float, float, float, float]:
    """Build grid/axis markup and return the plotting geometry."""
    left, right, top, bottom = SVG_MARGIN
    plot_width = SVG_WIDTH - left - right
    plot_height = SVG_HEIGHT - top - bottom
    elements = [
        f'<rect class="plot" x="{left}" y="{top}" width="{plot_width}" height="{plot_height}"/>'
    ]

    y_ticks = _axis_ticks(y_low, y_high)
    for tick in y_ticks:
        y = _scale(tick, y_low, y_high, top + plot_height, -plot_height)
        elements.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{SVG_WIDTH - right}" y2="{y:.1f}"/>'
        )
        elements.append(
            _svg_text(left - 8, y + 4, f"{tick:.0f}{y_suffix}", anchor="end", cls="axis")
        )

    if x_values is not None:
        assert x_low is not None and x_high is not None
        x_ticks = _axis_ticks(x_low, x_high)
        for tick in x_ticks:
            x = _scale(tick, x_low, x_high, left, plot_width)
            elements.append(
                f'<line class="grid vertical" x1="{x:.1f}" y1="{top}" '
                f'x2="{x:.1f}" y2="{top + plot_height}"/>'
            )
            elements.append(
                _svg_text(
                    x,
                    top + plot_height + 20,
                    f"{tick:.1f}{x_suffix}",
                    anchor="middle",
                    cls="axis",
                )
            )
    else:
        labels = list(x_labels)
        if labels:
            step = plot_width / max(len(labels) - 1, 1)
            label_indices = sorted(
                set([0, len(labels) - 1] + list(range(0, len(labels), max(1, len(labels) // 6))))
            )
            for index in label_indices:
                x = left + index * step
                elements.append(
                    _svg_text(
                        x,
                        top + plot_height + 32,
                        labels[index],
                        anchor="middle",
                        cls="axis x-label",
                    )
                )

    return elements, left, top, plot_width, plot_height, SVG_WIDTH - right, top + plot_height


def _empty_chart(message: str, title: str) -> str:
    """Render a chart-sized empty state without implying zero measurements."""
    _, _, top, _ = SVG_MARGIN
    content = (
        f'<text class="empty" x="{SVG_WIDTH / 2:.1f}" '
        f'y="{top + 140:.1f}" text-anchor="middle">{_esc(message)}</text>'
    )
    return _svg_shell(content, title)


def _line_segments(values: tuple[float | None, ...]) -> list[list[tuple[int, float]]]:
    """Split a series at missing values so gaps remain visible."""
    segments: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for index, value in enumerate(values):
        if value is None:
            if current:
                segments.append(current)
                current = []
            continue
        current.append((index, value))
    if current:
        segments.append(current)
    return segments


def render_floor_runtime_chart(data: ReportData) -> str:
    """Render the daily per-floor runtime line chart."""
    values = [
        value for series in data.floor_runtime_min.values() for value in series if value is not None
    ]
    if not data.dates or not values:
        return _empty_chart("No floor runtime data in the selected period.", "Daily floor runtime")

    y_high = max(max(values), 1.0)
    elements, left, top, plot_width, plot_height, _, _ = _chart_frame(
        0.0, math.ceil(y_high), "m", x_labels=data.dates
    )
    step = plot_width / max(len(data.dates) - 1, 1)
    for floor in KNOWN_FLOORS:
        color = FLOOR_COLORS[floor]
        points = data.floor_runtime_min[floor]
        for segment in _line_segments(points):

            def coordinate(index: int, value: float) -> str:
                y = _scale(
                    value,
                    0,
                    math.ceil(y_high),
                    top + plot_height,
                    -plot_height,
                )
                return f"{left + index * step:.1f},{y:.1f}"

            coords = " ".join(coordinate(index, value) for index, value in segment)
            if len(segment) > 1:
                elements.append(f'<polyline class="series" stroke="{color}" points="{coords}"/>')
        for index, value in enumerate(points):
            if value is None:
                continue
            x = left + index * step
            y = _scale(value, 0, math.ceil(y_high), top + plot_height, -plot_height)
            point_label = (
                f"{floor.replace('_', ' ').title()} {data.dates[index]}: {value:.1f} minutes"
            )
            elements.append(
                f'<circle class="point" data-floor="{floor}" cx="{x:.1f}" '
                f'cy="{y:.1f}" fill="{color}" r="3.5">'
                f"<title>{_esc(point_label)}</title></circle>"
            )

    legend_x = left
    for floor in KNOWN_FLOORS:
        color = FLOOR_COLORS[floor]
        label = floor.replace("_", " ").title()
        elements.append(
            f'<circle class="legend-dot" cx="{legend_x}" cy="18" fill="{color}" r="4"/>'
        )
        elements.append(_svg_text(legend_x + 9, 22, label, cls="legend"))
        legend_x += 100
    return _svg_shell("".join(elements), "Daily floor runtime")


def render_scatter_chart(data: ReportData) -> str:
    """Render the complete outdoor-temperature/runtime scatter points."""
    points = data.complete_scatter_points
    if not points:
        return _empty_chart(
            "No complete scatter points in the selected period.",
            "Outdoor temperature versus furnace runtime",
        )

    temps = [point.avg_temp_f for point in points]
    runtimes = [point.furnace_runtime_min for point in points]
    assert all(value is not None for value in temps + runtimes)
    x_low, x_high = min(temps), max(temps)
    y_low, y_high = 0.0, max(max(runtimes), 1.0)
    if x_low == x_high:
        x_low -= 1
        x_high += 1
    else:
        padding = max((x_high - x_low) * 0.05, 1.0)
        x_low -= padding
        x_high += padding

    elements, left, top, plot_width, plot_height, _, _ = _chart_frame(
        y_low,
        math.ceil(y_high),
        "m",
        x_values=temps,
        x_low=x_low,
        x_high=x_high,
        x_suffix="°F",
    )
    for point in points:
        assert point.avg_temp_f is not None and point.furnace_runtime_min is not None
        x = _scale(point.avg_temp_f, x_low, x_high, left, plot_width)
        y = _scale(
            point.furnace_runtime_min,
            y_low,
            math.ceil(y_high),
            top + plot_height,
            -plot_height,
        )
        label = f"{point.date}: {point.avg_temp_f:.1f}°F, {point.furnace_runtime_min:.1f} minutes"
        elements.append(
            f'<circle class="scatter-point" cx="{x:.1f}" cy="{y:.1f}" fill="#fb7185" r="4">'
            f"<title>{_esc(label)}</title></circle>"
        )
    return _svg_shell("".join(elements), "Outdoor temperature versus furnace runtime")


def _runtime_table(data: ReportData) -> str:
    """Render a transparent table backing both charts."""
    headers = [
        "Date",
        "Floor 1 runtime",
        "Floor 2 runtime",
        "Floor 3 runtime",
        "Outdoor temp",
        "Furnace runtime",
    ]
    header_html = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    scatter_by_date = {point.date: point for point in data.scatter_points}
    rows = []
    for index, date_str in enumerate(data.dates):
        point = scatter_by_date.get(date_str)
        cells = [f"<td>{_esc(date_str)}</td>"]
        cells.extend(
            f"<td>{_fmt_number(data.floor_runtime_min[floor][index])}</td>"
            for floor in KNOWN_FLOORS
        )
        cells.append(f"<td>{_fmt_number(point.avg_temp_f, '°F') if point else '—'}</td>")
        cells.append(f"<td>{_fmt_number(point.furnace_runtime_min, ' min') if point else '—'}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    body = "".join(rows) or '<tr><td colspan="6" class="empty-cell">No represented dates.</td></tr>'
    return (
        f'<div class="table-wrap"><table><thead><tr>{header_html}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def render_report(data: ReportData, start: date, end: date) -> str:
    """Build deterministic HTML for the supplied report data."""
    complete = len(data.complete_scatter_points)
    partial = data.partial_scatter_rows
    period = f"{start.isoformat()} → {end.isoformat()}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HomeOps HVAC trend report</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, sans-serif;
      background: #0b1120;
      color: #e2e8f0;
    }}
    body {{
      margin: 0;
      background: linear-gradient(145deg, #0b1120, #111827);
    }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 36px 22px 60px; }}
    header {{ margin-bottom: 24px; }}
    .eyebrow {{
      color: #38bdf8;
      font-size: .75rem;
      font-weight: 700;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    h1 {{ margin: 8px 0; font-size: clamp(1.8rem, 4vw, 3rem); color: #f8fafc; }}
    h2 {{ margin: 0 0 8px; color: #f8fafc; font-size: 1.2rem; }}
    p {{ color: #94a3b8; line-height: 1.55; }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin: 24px 0;
    }}
    .metric, .panel {{
      border: 1px solid #263244;
      border-radius: 14px;
      background: rgba(15, 23, 42, .82);
      box-shadow: 0 12px 30px rgba(0, 0, 0, .18);
    }}
    .metric {{ padding: 16px; }}
    .metric span {{
      display: block;
      color: #64748b;
      font-size: .75rem;
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .metric strong {{ display: block; margin-top: 7px; color: #f8fafc; font-size: 1.05rem; }}
    .panel {{ margin-top: 18px; padding: 22px; overflow: hidden; }}
    .panel > p {{ margin-top: 0; font-size: .92rem; }}
    .chart {{
      display: block;
      width: 100%;
      min-height: 260px;
      margin-top: 12px;
      overflow: visible;
    }}
    .plot {{ fill: #0f172a; stroke: #334155; stroke-width: 1; }}
    .grid {{ stroke: #263244; stroke-width: 1; }}
    .series {{ fill: none; stroke-width: 2.5; stroke-linejoin: round; stroke-linecap: round; }}
    .point, .scatter-point {{ stroke: #0f172a; stroke-width: 1.5; }}
    .axis {{ fill: #64748b; font-size: 12px; }}
    .x-label {{ font-size: 11px; }}
    .legend {{ fill: #cbd5e1; font-size: 12px; }}
    .empty {{ fill: #94a3b8; font-size: 15px; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .86rem; white-space: nowrap; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #263244; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ color: #94a3b8; font-weight: 600; }}
    td {{ color: #cbd5e1; }}
    .empty-cell {{ color: #64748b; text-align: center !important; }}
    footer {{ margin-top: 24px; color: #64748b; font-size: .78rem; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="eyebrow">HomeOps · read-only historical report</div>
      <h1>HVAC trend report</h1>
      <p>UTC calendar-day views of per-floor heating runtime and whole-furnace demand.<br>
      Period: <strong>{_esc(period)}</strong>.</p>
    </header>
    <section class="summary-grid" aria-label="Report coverage">
      <div class="metric"><span>Days represented</span><strong>{len(data.dates)}</strong></div>
      <div class="metric"><span>Complete scatter points</span><strong>{complete}</strong></div>
      <div class="metric"><span>Partial scatter rows</span><strong>{partial}</strong></div>
      <div class="metric"><span>Chart dependency</span><strong>None</strong></div>
    </section>
    <section class="panel">
      <h2>Daily floor runtime</h2>
      <p>Runtime is shown in minutes. Missing floor summaries remain gaps; a true
      zero-runtime summary remains zero.</p>
      {render_floor_runtime_chart(data)}
    </section>
    <section class="panel">
      <h2>Outdoor temperature vs furnace runtime</h2>
      <p>Only rows with both measurements are plotted. The coverage counts above
      make partial history visible without implying a correlation.</p>
      {render_scatter_chart(data)}
    </section>
    <section class="panel">
      <h2>Underlying daily data</h2>
      <p>The table is the source-facing view used by both charts. An em dash means
      the measurement was not available.</p>
      {_runtime_table(data)}
    </section>
    <footer>Generated from existing derived HomeOps events. This artifact is
    read-only and contains no live-service connection.</footer>
  </main>
</body>
</html>
"""


def write_report(data: ReportData, start: date, end: date, out_path: str | Path) -> None:
    """Write a deterministic HTML report, creating its parent directory."""
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(data, start, end), encoding="utf-8")


def _parse_date_arg(value: str) -> date:
    """Parse an ISO date CLI argument."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from exc


def _positive_days(value: str) -> int:
    """Parse a positive day count."""
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("days must be a positive integer") from exc
    if days < 1:
        raise argparse.ArgumentTypeError("days must be a positive integer")
    return days


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse report CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=_positive_days,
        default=DEFAULT_DAYS,
        help="Trailing days when no explicit range is given",
    )
    parser.add_argument("--start", type=_parse_date_arg, help="Inclusive UTC start date")
    parser.add_argument("--end", type=_parse_date_arg, help="Inclusive UTC end date")
    parser.add_argument(
        "--log", default=None, help="Path to derived event JSONL (overrides DERIVED_EVENT_LOG)"
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT, help=f"HTML output path (default: {DEFAULT_OUT})"
    )
    return parser.parse_args(argv)


def _resolve_range(args: argparse.Namespace) -> tuple[date, date] | None:
    """Resolve either an explicit range or a trailing-days window."""
    if (args.start is None) != (args.end is None):
        return None
    if args.start is not None and args.end is not None:
        return args.start, args.end
    end = date.today()
    return end - timedelta(days=args.days - 1), end


def main(argv: list[str] | None = None) -> int:
    """Run the HTML report CLI."""
    args = _parse_args(argv)
    report_range = _resolve_range(args)
    if report_range is None:
        print("Error: --start and --end must be supplied together", file=sys.stderr)
        return 2
    start, end = report_range
    if start > end:
        print("Error: --start must be on or before --end", file=sys.stderr)
        return 2

    log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
    try:
        data = build_report_data(log_path, start, end)
        write_report(data, start, end, args.out)
    except (OSError, ValueError) as exc:
        print(f"Error generating report: {exc}", file=sys.stderr)
        return 1

    print(f"Days represented: {len(data.dates)}")
    print(f"Complete scatter points: {len(data.complete_scatter_points)}")
    print(f"Partial scatter rows: {data.partial_scatter_rows}")
    print(f"Report written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
