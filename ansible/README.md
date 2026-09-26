# Fleet Deploy Ansible implementation

This directory contains the Ansible parity implementation for Fleet Deploy
Lab PR13. It is a controller-side playbook for the twelve logical simulated
vehicle IDs; those inventory names are not SSH hosts, containers, or physical
machines.

The playbook reuses the repository's canonical DeploymentSpec parser and
immutable profile-artifact checks, resolves target IDs through
`inventory.yml`, then performs the same protected queue → apply → fresh public
readback sequence as the Python deployer. It uses only built-in Ansible
modules.

## Local invocation

From the repository root, with Ansible Core installed:

```bash
FLEET_DEPLOY_API_KEY='not-for-browser' \
ansible-playbook \
  -i ansible/inventory.yml \
  ansible/deploy.yml \
  -e spec_path=/path/to/deployment-spec.json \
  -e artifact_path=/path/to/profile.json
```

The API URL defaults to `https://api.homeops.now/deploy/api`; override it with
`FLEET_DEPLOY_API_URL` or `-e api_base_url=...` for a synthetic test server.
The API key is intentionally read only from `FLEET_DEPLOY_API_KEY`. Protected
URI tasks use `no_log`, while the final report contains only the deployment ID,
target IDs, artifact digest, and verified status.

An environment selector is expanded by the shared Python contract and must
resolve entirely inside its matching Ansible inventory group. Explicit target
lists are checked against the same logical IDs and stable order. No Ansible
task performs SSH, package installation, or production host mutation.
