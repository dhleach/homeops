#!/usr/bin/env bash
# Deploy the HomeOps backend and validate the EC2 host-local interfaces.
#
# This script is streamed to the EC2 host by GitHub Actions and runs as ubuntu.
# The repository and Docker socket are owned by/available to ubuntu; sudo is
# used only for the existing dashboard ownership repair and Nginx validation.
#
# Revision history:
#   2026-08-21  Refresh the ignored Compose environment from EC2 IAM/SSM before
#               recreating the backend so OIDC and shared Valkey settings survive
#               deploys without putting credentials in the repository.
#   2026-08-19  Added bounded backend readiness polling and compose diagnostics
#               so cold Uvicorn starts do not falsely fail an otherwise healthy
#               deployment, while genuine startup failures still fail closed.
#   2026-08-18  Added owner-scoped fast-forward deployment, backend container
#               validation, and Nginx checks for the EC2 release gate.

set -euo pipefail

REPO_DIR="${HOMEOPS_REPO_DIR:-/home/ubuntu/homeops}"
BRANCH="${HOMEOPS_BRANCH:-master}"
BACKEND_HEALTH_URL="${HOMEOPS_BACKEND_HEALTH_URL:-http://127.0.0.1:8000/health}"
AWS_BIN="${HOMEOPS_AWS_BIN:-/usr/local/bin/aws}"
ENVIRONMENT="${HOMEOPS_ENVIRONMENT:-production}"
AWS_REGION="${AWS_REGION:-us-east-1}"

ssm_value() {
  local name="$1"
  [[ -x "$AWS_BIN" ]] || return 1
  "$AWS_BIN" ssm get-parameter \
    --name "$name" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text \
    --region "$AWS_REGION" 2>/dev/null
}

existing_env_value() {
  local name="$1"
  local env_file="$2"
  [[ -f "$env_file" ]] || return 0
  sed -n "s/^${name}=//p" "$env_file" | head -n 1
}

write_runtime_env() {
  local env_file="${REPO_DIR}/dashboard/.env"
  local gemini_key oidc_issuer oidc_audience audience_claim jwks_url diagnostic_scope
  local limiter_backend redis_url

  gemini_key="$(ssm_value "/homeops/${ENVIRONMENT}/gemini-api-key" || true)"
  oidc_issuer="$(ssm_value "/homeops/${ENVIRONMENT}/ask-homeops-oidc-issuer" || true)"
  oidc_audience="$(ssm_value "/homeops/${ENVIRONMENT}/ask-homeops-oidc-audience" || true)"
  audience_claim="$(ssm_value "/homeops/${ENVIRONMENT}/ask-homeops-oidc-audience-claim" || true)"
  jwks_url="$(ssm_value "/homeops/${ENVIRONMENT}/ask-homeops-oidc-jwks-url" || true)"
  diagnostic_scope="$(ssm_value "/homeops/${ENVIRONMENT}/ask-homeops-diagnostic-scope" || true)"
  limiter_backend="$(ssm_value "/homeops/${ENVIRONMENT}/ask-homeops-limiter-backend" || true)"
  redis_url="$(ssm_value "/homeops/${ENVIRONMENT}/ask-homeops-redis-url" || true)"

  # Preserve known-good values if an otherwise unrelated SSM read is
  # temporarily unavailable. Do not print the values or include them in logs.
  [[ -n "$gemini_key" ]] || gemini_key="$(existing_env_value GEMINI_API_KEY "$env_file")"
  [[ -n "$oidc_issuer" ]] || oidc_issuer="$(existing_env_value ASK_HOMEOPS_OIDC_ISSUER "$env_file")"
  [[ -n "$oidc_audience" ]] || oidc_audience="$(existing_env_value ASK_HOMEOPS_OIDC_AUDIENCE "$env_file")"
  [[ -n "$audience_claim" ]] || audience_claim="$(existing_env_value ASK_HOMEOPS_OIDC_AUDIENCE_CLAIM "$env_file")"
  [[ -n "$jwks_url" ]] || jwks_url="$(existing_env_value ASK_HOMEOPS_OIDC_JWKS_URL "$env_file")"
  [[ -n "$diagnostic_scope" ]] || diagnostic_scope="$(existing_env_value ASK_HOMEOPS_DIAGNOSTIC_SCOPE "$env_file")"
  [[ -n "$limiter_backend" ]] || limiter_backend="$(existing_env_value ASK_HOMEOPS_LIMITER_BACKEND "$env_file")"
  [[ -n "$redis_url" ]] || redis_url="$(existing_env_value ASK_HOMEOPS_REDIS_URL "$env_file")"

  if [[ -z "$oidc_issuer" || -z "$oidc_audience" || -z "$diagnostic_scope" ]]; then
    echo "Ask HomeOps OIDC settings are incomplete; backend will remain fail-closed" >&2
  fi
  if [[ -z "$limiter_backend" || -z "$redis_url" ]]; then
    echo "Ask HomeOps shared limiter settings are incomplete; backend will remain fail-closed" >&2
  fi

  umask 077
  {
    printf 'GEMINI_API_KEY=%s\n' "$gemini_key"
    printf 'ASK_HOMEOPS_OIDC_ISSUER=%s\n' "$oidc_issuer"
    printf 'ASK_HOMEOPS_OIDC_AUDIENCE=%s\n' "$oidc_audience"
    printf 'ASK_HOMEOPS_OIDC_AUDIENCE_CLAIM=%s\n' "$audience_claim"
    printf 'ASK_HOMEOPS_OIDC_JWKS_URL=%s\n' "$jwks_url"
    printf 'ASK_HOMEOPS_DIAGNOSTIC_SCOPE=%s\n' "$diagnostic_scope"
    printf 'ASK_HOMEOPS_LIMITER_BACKEND=%s\n' "$limiter_backend"
    printf 'ASK_HOMEOPS_REDIS_URL=%s\n' "$redis_url"
  } > "$env_file"
  chmod 600 "$env_file"
}

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

  write_runtime_env

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
