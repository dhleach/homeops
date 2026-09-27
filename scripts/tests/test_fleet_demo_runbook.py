"""Contract checks for the recruiter-facing Fleet Deploy Lab runbook."""

from pathlib import Path

ROOT = Path(__file__).parents[2]
RUNBOOK = ROOT / "docs" / "fleet-deploy-demo-runbook.md"


def test_runbook_covers_the_verified_demo_paths_and_code_owners():
    text = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "Normal success walkthrough",
        "Verification-failure and rollback walkthrough",
        "Reset and recovery walkthrough",
        "Known limitations and honest answers",
        "anonymous browser",
        "FLEET_DEPLOY_GITHUB_TOKEN",
        "FLEET_DEPLOY_API_KEY",
        "one request per UTC day",
        "not hardware-in-the-loop",
    ):
        assert required in text

    for relative_path in (
        "dashboard/frontend/src/components/FleetDeployView.jsx",
        "dashboard/backend/fleet_api.py",
        "deploy_demo/deployment_spec.py",
        "deploy_demo/fleet_state.py",
        "deploy_demo/deployer.py",
        "ansible/deploy.yml",
        ".github/workflows/fleet-deploy.yml",
        "scripts/deploy_smoke_check.py",
    ):
        assert (ROOT / relative_path).exists(), relative_path


def test_runbook_does_not_present_simulated_targets_as_real_vehicle_deployment():
    text = RUNBOOK.read_text(encoding="utf-8").lower()

    assert "simulated vehicles" in text
    assert "not hardware-in-the-loop" in text
    assert "deployment to a real vehicle" in text
