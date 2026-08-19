#!/usr/bin/env bash
# Deploy the HomeOps backend and validate the EC2 host-local interfaces.
#
# This script is streamed to the EC2 host by GitHub Actions and runs as ubuntu.
# The repository and Docker socket are owned by/available to ubuntu; sudo is
# used only for the existing dashboard ownership repair and Nginx validation.
#
# Revision history:
#   2026-08-19  Added bounded backend readiness polling and compose diagnostics
#               so cold Uvicorn starts do not falsely fail an otherwise healthy
#               deployment, while genuine startup failures still fail closed.
#   2026-08-18  Added owner-scoped fast-forward deployment, backend container
#               validation, and Nginx checks for the EC2 release gate.

set -euo pipefail

REPO_DIR="${HOMEOPS_REPO_DIR:-/home/ubuntu/homeops}"
BRANCH="${HOMEOPS_BRANCH:-master}"
BACKEND_HEALTH_URL="${HOMEOPS_BACKEND_HEALTH_URL:-http://127.0.0.1:8000/health}"

wait_for_backend() {
  local attempts="${HOMEOPS_BACKEND_READINESS_ATTEMPTS:-30}"
  local delay_seconds="${HOMEOPS_BACKEND_READINESS_DELAY_SECONDS:-2}"
  local timeout_seconds="${HOMEOPS_BACKEND_HEALTH_TIMEOUT_SECONDS:-10}"
  local attempt

  if ! [[ "$attempts" =~ ^[1-9][0-9]*$ ]]; then
    echo "HOMEOPS_BACKEND_READINESS_ATTEMPTS must be a positive integer: $attempts" >&2
    return 2
  fi

  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl --fail --silent --show-error --max-time "$timeout_seconds" \
      "$BACKEND_HEALTH_URL" >/dev/null; then
      echo "Backend health check passed on attempt $attempt/$attempts"
      return 0
    fi

    if ((attempt < attempts)); then
      echo "Backend not ready; retrying in ${delay_seconds}s (attempt $attempt/$attempts)" >&2
      sleep "$delay_seconds"
    fi
  done

  echo "Backend did not become healthy after $attempts attempts: $BACKEND_HEALTH_URL" >&2
  echo "--- docker compose ps backend ---" >&2
  docker compose ps backend >&2 || true
  echo "--- docker compose logs --tail=100 backend ---" >&2
  docker compose logs --tail=100 backend >&2 || true
  return 1
}

deploy() {
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

  wait_for_backend
  sudo -n nginx -t

  DEPLOYED_SHA="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
  echo "EC2 backend deploy complete: $DEPLOYED_SHA ($BRANCH); backend healthy; Nginx config valid"
}

if [[ "${HOMEOPS_DEPLOY_EC2_LIB_ONLY:-0}" != "1" ]]; then
  deploy "$@"
fi
