#!/usr/bin/env bash
# Deploy the HomeOps backend and validate the EC2 host-local interfaces.
#
# This script is streamed to the EC2 host by GitHub Actions and runs as ubuntu.
# The repository and Docker socket are owned by/available to ubuntu; sudo is
# used only for the existing dashboard ownership repair and Nginx validation.
#
# Revision history:
#   2026-08-18  Added owner-scoped fast-forward deployment, backend container
#               validation, and Nginx checks for the EC2 release gate.

set -euo pipefail

REPO_DIR="${HOMEOPS_REPO_DIR:-/home/ubuntu/homeops}"
BRANCH="${HOMEOPS_BRANCH:-master}"

if [[ "$(git -C "$REPO_DIR" branch --show-current)" != "$BRANCH" ]]; then
  echo "Expected $REPO_DIR to be on $BRANCH" >&2
  exit 1
fi

if [[ -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
  echo "Refusing to deploy over a dirty worktree: $REPO_DIR" >&2
  exit 1
fi

git -C "$REPO_DIR" fetch --prune origin "$BRANCH"
git -C "$REPO_DIR" merge --ff-only "origin/$BRANCH"

cd "$REPO_DIR/dashboard"
docker compose up -d --build --force-recreate backend

# Docker can create root-owned bind-mounted directories. Keep the checked-out
# tree writable by ubuntu without changing ownership outside this repository.
sudo -n chown -R ubuntu:ubuntu "$REPO_DIR/dashboard"

BACKEND_CONTAINER="$(docker compose ps -q backend)"
if [[ -z "$BACKEND_CONTAINER" ]]; then
  echo "Backend container was not created" >&2
  exit 1
fi
[[ "$(docker inspect --format '{{.State.Running}}' "$BACKEND_CONTAINER")" == "true" ]]

curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8000/health >/dev/null
sudo -n nginx -t

DEPLOYED_SHA="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
echo "EC2 backend deploy complete: $DEPLOYED_SHA ($BRANCH); backend healthy; Nginx config valid"
