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

## Ask HomeOps authentication and quota runtime

Terraform provisions the Cognito user pool, managed-login domain, public
authorization-code + PKCE app client, and the
`https://api.homeops.now/diagnostic:read` custom scope. It also writes the
non-secret OIDC and Valkey settings to
`/homeops/production/ask-homeops-*` SSM parameters. The EC2 deploy script reads
those values with the instance role and refreshes the ignored, mode-0600
`dashboard/.env` before recreating the backend. The Gemini key remains in its
existing encrypted SSM parameter.

After applying the Cognito resources:

1. Copy `terraform output -raw cognito_managed_login_authority`,
   `cognito_frontend_client_id`, and `cognito_frontend_scope` into the GitHub
   repository variables `HOMEOPS_OIDC_AUTHORITY`, `HOMEOPS_OIDC_CLIENT_ID`, and
   `HOMEOPS_OIDC_SCOPE`.
2. Invite the intended demo user using the pool ID output:

   ```bash
   POOL_ID="$(terraform output -raw cognito_user_pool_id)"
   aws cognito-idp admin-create-user \
     --user-pool-id "$POOL_ID" \
     --username "you@example.com" \
     --user-attributes Name=email,Value=you@example.com \
     --desired-delivery-mediums EMAIL
   ```

   The user pool is admin-create-only, so an invite is deliberate rather than
   allowing arbitrary public registrations.
3. Push/merge the application change so the backend deploy refreshes SSM config
   and the frontend deploy embeds only public OIDC metadata.
4. Confirm the browser redirects to Cognito managed login, returns to
   `/auth/callback`, and sends an access token on `POST /api/diagnostic`.

The backend accepts Cognito's `client_id` access-token claim and verifies the
signature, issuer, expiry, subject, and configured scope against the pool JWKS.
Valkey listens only on EC2 loopback; the backend performs all per-user/IP
window reservations in one atomic Lua script. Missing OIDC or Valkey settings
remain a safe `401`/`503` failure rather than enabling anonymous access.

## Pi permission model

The original deploy failure occurred because `github-deploy` ran `git pull` in a
checkout owned by `leachd`; Git could not write `.git/objects`. The fix is to
run Git as the owner, not to recursively make the repository writable by every
deployment process.

The Pi must have one narrowly scoped sudo policy for the CI account. The
reviewed source of truth is
[`deploy/sudoers/homeops-github-deploy`](../deploy/sudoers/homeops-github-deploy).
The canonical installed path is `/etc/sudoers.d/homeops-github-deploy`.
Install and validate it as an administrator on the Pi, adjusting executable
paths with `command -v` if the distribution differs:

```sudoers
# See deploy/sudoers/homeops-github-deploy in the repository.
```

```bash
sudo install -o root -g root -m 0440 \
  deploy/sudoers/homeops-github-deploy \
  /etc/sudoers.d/homeops-github-deploy
sudo visudo -cf /etc/sudoers.d/homeops-github-deploy
sudo -u leachd -H git -C /home/leachd/repos/homeops status --porcelain
sudo -n -u leachd -H git -C /home/leachd/repos/homeops branch --show-current
```

After the canonical file validates, remove the stale duplicate if it exists,
then validate the complete sudoers configuration again:

```bash
sudo rm -f /etc/sudoers.d/github-deploy
sudo visudo -c
sudo -u github-deploy -H sudo -n -u leachd -H \
  git -C /home/leachd/repos/homeops branch --show-current
sudo -u github-deploy -H sudo -n systemctl is-active --quiet homeops-observer
sudo -u github-deploy -H sudo -n systemctl is-active --quiet homeops-consumer
```

The duplicate removal is intentionally administrator-only: `github-deploy` and
the Bob agent do not have permission to alter `/etc/sudoers.d`.

Do not grant `github-deploy` `NOPASSWD: ALL`, make `.git` world-writable, or
store a private key in the repository.

## EC2 CI SSH identity

The dedicated `homeops-ec2-deploy` public key is baked into the EC2
`authorized_keys` bootstrap in [`infra/ec2.tf`](../infra/ec2.tf). Its private
half remains only in the GitHub Actions `EC2_DEPLOY_SSH_KEY` secret. Keep that
identity separate from the personal `homeops-production`/Pi key; when rotating
the CI credential, replace both halves together and compare the public-key
fingerprint before rerunning the release workflow.

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
