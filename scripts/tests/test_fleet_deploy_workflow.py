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


def test_workflow_checks_out_master_and_never_checks_out_manifest_branch() -> None:
    """Writable manifest data is fetched as Git objects and read with git show."""
    assert "ref: master" in WORKFLOW
    assert "persist-credentials: false" in WORKFLOW
    assert "refs/heads/${MANIFEST_BRANCH}:refs/remotes/origin/${MANIFEST_BRANCH}" in WORKFLOW
    assert 'git show "${EVENT_COMMIT_SHA}:${MANIFEST_PATH}"' in WORKFLOW
    assert "git merge-base --is-ancestor" in WORKFLOW
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


def test_workflow_has_read_only_permissions_and_no_deployer_side_effect() -> None:
    """PR09 builds evidence only; the deployer belongs to a later workflow."""
    assert "permissions:\n  contents: read" in WORKFLOW
    assert "permissions:\n      contents: read" in WORKFLOW
    assert "actions/upload-artifact@v4" in WORKFLOW
    assert "FLEET_DEPLOY_API_KEY" not in WORKFLOW
    assert "deploy_demo.deployer" not in WORKFLOW
    assert "FleetApiClient" not in WORKFLOW


def test_workflow_displays_event_sha_separately_from_profile_digest() -> None:
    """The provenance boundary is visible in logs and uploaded metadata."""
    assert 'echo "Event commit SHA: ${EVENT_COMMIT_SHA}"' in WORKFLOW
    assert 'echo "Profile artifact SHA-256:' in WORKFLOW
    assert "profile-provenance.json" in WORKFLOW
