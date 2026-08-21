"""Tests for the CI-generated canonical HomeOps test count tooling.

Revision history:
  2026-08-20  Added parser, manifest, and README validation coverage so test
              count drift fails in CI rather than reaching public documentation.
"""

import json

import pytest
from test_counts import (
    build_counts,
    count_junit_tests,
    count_vitest_tests,
    validate_counts,
    validate_readme,
)


def test_count_junit_tests_sums_direct_suites(tmp_path):
    report = tmp_path / "python.xml"
    report.write_text('<testsuites><testsuite tests="2"/><testsuite tests="3"/></testsuites>')
    assert count_junit_tests(report) == 5


def test_count_junit_tests_falls_back_to_testcases(tmp_path):
    report = tmp_path / "python.xml"
    report.write_text("<testsuite><testcase/><testcase/><testcase/></testsuite>")
    assert count_junit_tests(report) == 3


def test_count_vitest_tests_reads_total(tmp_path):
    report = tmp_path / "react.json"
    report.write_text(json.dumps({"numTotalTests": 30}))
    assert count_vitest_tests(report) == 30


def test_validate_counts_accepts_matching_manifest():
    counts = build_counts(841, 30)
    validate_counts(counts, {"python": 841, "react": 30, "total": 871})


def test_validate_readme_requires_both_canonical_claims(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("841 Python tests and 30 React component tests.")
    validate_readme(readme, {"python": 841, "react": 30, "total": 871})

    readme.write_text("836 Python tests and 30 React component tests.")
    with pytest.raises(ValueError, match="missing canonical"):
        validate_readme(readme, {"python": 841, "react": 30, "total": 871})
