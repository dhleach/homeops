"""Report and validate the canonical HomeOps test counts.

Revision history:
  2026-08-20  Added JUnit/Vitest result parsing and manifest/README validation so
              public test-count claims are generated from the CI test runners.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _test_suites(root: ET.Element) -> list[ET.Element]:
    """Return JUnit suites without double-counting nested suites."""
    if root.tag == "testsuite":
        return [root]
    direct = root.findall("./testsuite")
    return direct or root.findall(".//testsuite")


def count_junit_tests(path: Path) -> int:
    """Count test cases reported by a JUnit XML result file."""
    root = ET.parse(path).getroot()
    suites = _test_suites(root)
    if not suites:
        return len(root.findall(".//testcase"))

    total = 0
    for suite in suites:
        tests = suite.attrib.get("tests")
        total += int(tests) if tests is not None else len(suite.findall("./testcase"))
    return total


def count_vitest_tests(path: Path) -> int:
    """Count tests reported by Vitest's JSON reporter."""
    report: dict[str, Any] = json.loads(path.read_text())
    try:
        total = report["numTotalTests"]
    except KeyError as exc:
        raise ValueError(f"Vitest report is missing {exc.args[0]!r}: {path}") from exc
    if not isinstance(total, int) or total < 0:
        raise ValueError(f"Vitest test count must be a non-negative integer: {path}")
    return total


def build_counts(python_tests: int, react_tests: int) -> dict[str, int]:
    """Build the stable count payload used by the manifest and CI output."""
    if python_tests < 0 or react_tests < 0:
        raise ValueError("test counts must be non-negative")
    return {
        "python": python_tests,
        "react": react_tests,
        "total": python_tests + react_tests,
    }


def load_counts(path: Path) -> dict[str, int]:
    """Load and validate the numeric count fields from a manifest."""
    data: dict[str, Any] = json.loads(path.read_text())
    counts = {key: data.get(key) for key in ("python", "react", "total")}
    if any(not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ValueError(f"manifest must contain non-negative integer counts: {path}")
    return counts  # type: ignore[return-value]


def validate_counts(actual: dict[str, int], expected: dict[str, int]) -> None:
    """Fail when the committed manifest no longer matches CI results."""
    if actual != expected:
        raise ValueError(
            "canonical test counts are stale: "
            f"CI={actual}, manifest={expected}; regenerate docs/test-counts.json"
        )


def validate_readme(path: Path, counts: dict[str, int]) -> None:
    """Ensure the root README publishes the same verified counts."""
    text = path.read_text()
    required = (
        f"{counts['python']} Python tests",
        f"{counts['react']} React component tests",
    )
    missing = [claim for claim in required if claim not in text]
    if missing:
        raise ValueError(f"README is missing canonical test-count claims: {missing}")


def write_manifest(path: Path, counts: dict[str, int]) -> None:
    """Write a deterministic manifest suitable for review and CI validation."""
    manifest = {
        "schema": "homeops.test-counts.v1",
        **counts,
        "sources": {
            "python": "pytest JUnit XML report",
            "react": "Vitest JSON reporter",
        },
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-junit", type=Path, required=True)
    parser.add_argument("--react-json", type=Path, required=True)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--readme", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        counts = build_counts(
            count_junit_tests(args.python_junit),
            count_vitest_tests(args.react_json),
        )
        if args.expected:
            validate_counts(counts, load_counts(args.expected))
        if args.readme:
            validate_readme(args.readme, counts)
        if args.output:
            write_manifest(args.output, counts)
    except (OSError, ET.ParseError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Python tests: {counts['python']}")
    print(f"React tests: {counts['react']}")
    print(f"Total tests: {counts['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
