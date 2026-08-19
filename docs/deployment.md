# HomeOps deployment and release gate

## Deployment sequence

Production deploys are serialized with the `homeops-production-deploy`
concurrency group. A push to `master` runs:

1. GitHub Actions joins the Tailnet and verifies that the Pi peer responds.
2. [`deploy/deploy-pi.sh`](../deploy/deploy-pi.sh) is streamed to
   `github-deploy@100.115.21.72`.
   - It refuses a detached branch or dirty worktree.
   - It fetches and fast-forwards `/home/leachd/repos/homeops` as `leachd`,
     the repository owner.
   - It restarts and checks `homeops-observer` and `homeops-consumer`.
3. [`deploy/deploy-ec2.sh`](../deploy/deploy-ec2.sh) is streamed to `ubuntu`
   over the `homeops-ec2` Tailnet hostname when that peer is reachable. The
   workflow falls back to the current public EIP `32.194.69.77` only when the
   private interface cannot be resolved.
   - It fast-forwards `/home/ubuntu/homeops`.
   - It rebuilds/recreates the FastAPI backend container.
   - It checks the container, waits with a bounded retry loop for
     `http://127.0.0.1:8000/health`, and checks Nginx syntax. If the backend
     never becomes ready, it prints the container state and recent backend logs.
4. `scripts/deploy_smoke_check.py` verifies the public frontend, API,
   telemetry, Grafana, and Prometheus interfaces.

The separate frontend workflow runs `npm ci`, builds with
`VITE_API_URL=https://api.homeops.now`, syncs the private S3 bucket, invalidates
CloudFront, and runs the same public smoke checks.

## Pi permission model

The original deploy failure occurred because `github-deploy` ran `git pull` in a
checkout owned by `leachd`; Git could not write `.git/objects`. The fix is to
run Git as the owner, not to recursively make the repository writable by every
deployment process.

The Pi must have a narrowly scoped sudo policy for the CI account. Install and
validate this as an administrator on the Pi, adjusting executable paths with
`command -v` if the distribution differs:

```sudoers
# /etc/sudoers.d/homeops-github-deploy
Cmnd_Alias HOMEOPS_GIT = /usr/bin/git -C /home/leachd/repos/homeops *
Cmnd_Alias HOMEOPS_SERVICES = /usr/bin/systemctl restart homeops-observer, /usr/bin/systemctl restart homeops-consumer, /usr/bin/systemctl is-active --quiet homeops-observer, /usr/bin/systemctl is-active --quiet homeops-consumer
github-deploy ALL=(leachd) NOPASSWD: HOMEOPS_GIT
github-deploy ALL=(root) NOPASSWD: HOMEOPS_SERVICES
```

```bash
sudo visudo -cf /etc/sudoers.d/homeops-github-deploy
sudo -u leachd -H git -C /home/leachd/repos/homeops status --porcelain
sudo -n -u leachd -H git -C /home/leachd/repos/homeops branch --show-current
```

Do not grant `github-deploy` `NOPASSWD: ALL`, make `.git` world-writable, or
store a private key in the repository.

## Release-gate failure handling

- A Pi failure stops the sequence before EC2 is changed.
- An EC2 host-local health failure stops before the public smoke check. The
  backend readiness check tolerates a cold Uvicorn start but fails closed after
  its bounded retry window, including recent container diagnostics.
- A public smoke failure fails the GitHub Actions run even if the SSH commands
  completed. Inspect the failing URL, then check the deployed SHA and service
  status on the affected host.
- The smoke checker intentionally treats a non-null telemetry `error` as a
  release failure. A process that is alive but cannot reach Prometheus is not a
  healthy public release.

## Rollback

Prefer a normal revert commit on `master`, then let the same release gate
deploy and verify the reverted revision. For an emergency operator rollback,
record the known-good SHA first and run the equivalent owner-scoped Git command
on the affected host; do not reset a dirty worktree without explicit approval.
After restoring the revision, restart the affected services and run:

```bash
python3 scripts/deploy_smoke_check.py
```

The workflows use fast-forward-only updates, so a host that has diverged or
contains uncommitted changes fails visibly instead of silently creating a merge
commit or overwriting local state.

## Manual verification commands

```bash
# Public release gate from any machine with Python 3.11+
python3 scripts/deploy_smoke_check.py

# API contract and observability probes
curl -fsS https://api.homeops.now/health
curl -fsS https://api.homeops.now/api/current-temps
curl -fsS https://api.homeops.now/grafana/api/health
curl -fsS https://api.homeops.now/prometheus/-/healthy
```

For the full address and port map, see [`architecture.md`](architecture.md).
