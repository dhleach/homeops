"""Tests for the reviewed public Bob evaluation snapshot.

Revision history:
  2026-08-27  Added a publication allowlist and redaction-boundary checks so a
              future snapshot refresh cannot silently add raw traces, prompts,
              model outputs, or external page dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "dashboard" / "frontend" / "public" / "bob" / "evals"
EXPECTED_CASES = {
    "openclaw-regression-builtin-hidden-location.json",
    "openclaw-regression-employer-alias-cross-parent.json",
    "openclaw-regression-event-cost-reset-attribution.json",
    "openclaw-regression-help-probe-side-effect.json",
    "openclaw-regression-merge-state-verification.json",
    "openclaw-regression-no-network-replay.json",
    "openclaw-regression-notion-url-key-drift.json",
    "openclaw-regression-partial-notion-batch-retry.json",
    "openclaw-regression-prompt-injection-hard-block.json",
    "openclaw-regression-proofpoint-adjacent-roles.json",
    "openclaw-regression-session-text-field-recovery.json",
    "openclaw-regression-source-jd-alignment.json",
    "openclaw-regression-trace-redaction-boundary.json",
    "openclaw-regression-whitelist-claim-race.json",
}


def test_public_bundle_contains_only_the_reviewed_artifact_allowlist():
    files = {path.relative_to(BUNDLE).as_posix() for path in BUNDLE.rglob("*") if path.is_file()}

    assert files == {
        "index.html",
        "evaluation-report.v1.json",
        "evaluation-live-trials.v1.json",
        *(f"cases/{name}" for name in EXPECTED_CASES),
    }


def test_public_bundle_proves_offline_non_production_execution():
    report = json.loads((BUNDLE / "evaluation-report.v1.json").read_text())
    live = json.loads((BUNDLE / "evaluation-live-trials.v1.json").read_text())

    assert report["schema_version"] == "evaluation-report.v1"
    assert report["mode"] == "deterministic"
    assert report["status"] == "passed"
    assert report["execution"]["network_enabled"] is False
    assert report["execution"]["external_mutations_enabled"] is False
    assert report["execution"]["credentials_loaded"] is False
    assert report["execution"]["model_calls"] == 0
    assert report["artifacts"]["seeded_traces_published"] is False
    assert report["release_gate"]["status"] == "passed"

    assert live["schema_version"] == "evaluation-live-trials.v1"
    assert live["mode"] == "scripted"
    for field in (
        "model_network_enabled",
        "tool_network_enabled",
        "external_mutations_enabled",
        "production_path_enabled",
    ):
        assert live["execution"][field] is False
    assert live["artifacts"]["raw_model_outputs_published"] is False
    assert live["artifacts"]["raw_prompts_published"] is False


def test_public_dashboard_is_self_contained():
    html = (BUNDLE / "index.html").read_text().lower()

    assert 'src="' not in html
    assert "http://" not in html
    assert "https://" not in html


def test_cloudfront_preserves_relative_artifact_urls_at_the_short_route():
    cloudfront = (REPO / "infra" / "cloudfront.tf").read_text()

    assert "if (uri === '/bob/evals')" in cloudfront
    assert "value: '/bob/evals/'" in cloudfront
    assert "if (uri === '/bob/evals/')" in cloudfront
    assert "request.uri = '/bob/evals/index.html'" in cloudfront
