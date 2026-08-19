#!/usr/bin/env bash
# Deploy the checked-out HomeOps revision to the Raspberry Pi.
#
# This script is streamed to the Pi by GitHub Actions and runs as the
# github-deploy SSH account. Git operations deliberately run as leachd, the
# owner of /home/leachd/repos/homeops, so CI does not need write permission on
# the repository or a broad recursive chmod/chown workaround.
#
# Revision history:
#   2026-08-18  Added owner-scoped fast-forward deployment and service checks to
#               fix the github-deploy .git/objects permission failure safely.

set -euo pipefail

REPO_DIR="${HOMEOPS_REPO_DIR:-/home/leachd/repos/homeops}"
BRANCH="${HOMEOPS_BRANCH:-master}"
REPO_OWNER="${HOMEOPS_REPO_OWNER:-leachd}"

as_owner() {
  sudo -n -u "$REPO_OWNER" -- "$@"
}

if [[ "$(as_owner git -C "$REPO_DIR" branch --show-current)" != "$BRANCH" ]]; then
  echo "Expected $REPO_DIR to be on $BRANCH" >&2
  exit 1
fi

if [[ -n "$(as_owner git -C "$REPO_DIR" status --porcelain)" ]]; then
  echo "Refusing to deploy over a dirty worktree: $REPO_DIR" >&2
  exit 1
fi

as_owner git -C "$REPO_DIR" fetch --prune origin "$BRANCH"
as_owner git -C "$REPO_DIR" merge --ff-only "origin/$BRANCH"

for service in homeops-observer homeops-consumer; do
  sudo -n systemctl restart "$service"
  sudo -n systemctl is-active --quiet "$service"
done

DEPLOYED_SHA="$(as_owner git -C "$REPO_DIR" rev-parse --short HEAD)"
echo "Pi deploy complete: $DEPLOYED_SHA ($BRANCH); services active: homeops-observer homeops-consumer"
