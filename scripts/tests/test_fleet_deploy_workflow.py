"""Readiness checks for the trusted Fleet Deploy workflow boundary."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "fleet-deploy.yml"
).read_text(encoding="utf-8")


def test_workflow_is_dispatchable_only_with_bounded_identity_inputs() -> None:
    """The manual entry point must carry both identities explicitly."""
    assert "workflow_dispatch:" in WORKFLOW
    assert "GITHUB_REF_PROTECTED:-false" in WORKFLOW
    assert "refs/heads/master" in WORKFLOW
    assert "actions/setup-python@v5" in WORKFLOW
    assert "deployment_id:" in WORKFLOW
    assert "event_commit_sha:" in WORKFLOW
    assert "required: true" in WORKFLOW
    assert "type: string" in WORKFLOW
    assert "MANIFEST_BRANCH: fleet-deployments" in WORKFLOW
    assert "MANIFEST_PATH: manifests/${{ inputs.deployment_id }}.json" in WORKFLOW
    assert "run-name: Fleet deployment ${{ inputs.deployment_id }}" in WORKFLOW


def test_workflow_checks_out_master_and_never_checks_out_manifest_branch() -> None:
    """Writable manifest data is fetched as Git objects and read with git show."""
    assert "ref: ${{ github.sha }}" in WORKFLOW
    assert "GITHUB_REF" in WORKFLOW
    assert "persist-credentials: false" in WORKFLOW
    assert "refs/heads/${MANIFEST_BRANCH}:refs/remotes/origin/${MANIFEST_BRANCH}" in WORKFLOW
    assert 'git show "${EVENT_COMMIT_SHA}:${MANIFEST_PATH}"' in WORKFLOW
    assert "git merge-base --is-ancestor" in WORKFLOW
    assert "printf 'x-access-token:%s'" in WORKFLOW
    assert "base64 --wrap=0" in WORKFLOW
    assert "http.extraHeader=Authorization: Basic ${AUTH_HEADER}" in WORKFLOW
    assert "http.extraHeader=Authorization: Bearer ${GITHUB_TOKEN}" not in WORKFLOW
    assert "ref: ${{ env.MANIFEST_BRANCH }}" not in WORKFLOW
    assert "git checkout" not in WORKFLOW


def test_manifest_validation_precedes_lint_tests_and_artifact_generation() -> None:
    """Invalid untrusted data must stop before any later build gate."""
    validation = WORKFLOW.index("- name: Validate exact manifest before build gates")
    tool_install = WORKFLOW.index("- name: Install trusted workflow test tools")
    lint = WORKFLOW.index("- name: Lint trusted workflow support")
    tests = WORKFLOW.index("- name: Run trusted workflow tests")
    artifact = WORKFLOW.index("- name: Build immutable profile artifact")

    assert validation < tool_install < lint < tests < artifact
    assert "deploy_demo.trusted_artifact validate" in WORKFLOW
    assert "deploy_demo.trusted_artifact build" in WORKFLOW


def test_workflow_separates_read_only_validation_from_simulator_write_job() -> None:
    """Only the post-gate job receives the separate simulator write boundary."""
    assert "permissions:\n  contents: read" in WORKFLOW
    assert "permissions:\n      contents: read" in WORKFLOW
    assert "actions/upload-artifact@v4" in WORKFLOW
    trusted_job, deploy_job = WORKFLOW.split("  deploy-simulator:", maxsplit=1)
    assert "FLEET_DEPLOY_API_KEY" not in trusted_job
    assert "needs: trusted-artifact" in deploy_job
    assert "concurrency:" in deploy_job
    assert "group: fleet-deploy-write" in deploy_job
    assert "actions/download-artifact@v4" in deploy_job
    assert "FLEET_DEPLOY_API_KEY: ${{ secrets.FLEET_DEPLOY_API_KEY }}" in deploy_job
    assert "deploy_demo.trusted_artifact verify" in deploy_job
    assert "deploy_demo.deployer" in deploy_job
    assert "FleetApiClient" not in deploy_job


def test_deployment_job_writes_only_after_artifact_verification() -> None:
    """No simulator write can occur before the downloaded artifact is rechecked."""
    deploy_job = WORKFLOW.split("  deploy-simulator:", maxsplit=1)[1]
    verification = deploy_job.index("- name: Verify downloaded artifact provenance")
    credential = deploy_job.index("- name: Require simulator write credential")
    deployer = deploy_job.index("- name: Deploy artifact and verify fresh API readback")

    assert verification < credential < deployer
    assert '--artifact "${RUNNER_TEMP}/fleet-artifact/profile.json"' in deploy_job
    assert '--provenance "${RUNNER_TEMP}/fleet-artifact/profile-provenance.json"' in deploy_job


def test_workflow_displays_event_sha_separately_from_profile_digest() -> None:
    """The provenance boundary is visible in logs and uploaded metadata."""
    assert 'echo "Event commit SHA: ${EVENT_COMMIT_SHA}"' in WORKFLOW
    assert 'echo "Profile artifact SHA-256:' in WORKFLOW
    assert "profile-provenance.json" in WORKFLOW
