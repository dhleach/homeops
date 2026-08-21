#!/usr/bin/env bash
# Focused tests for the EC2 deployment readiness gate.
#
# Revision history:
#   2026-08-21  Added coverage for the SSM-backed runtime environment refresh so
#               auth/Valkey settings persist before a backend recreation.
#   2026-08-19  Added retry-success and retry-exhaustion coverage so the
#               release gate tolerates cold starts but still fails closed.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOMEOPS_DEPLOY_EC2_LIB_ONLY=1 source "$SCRIPT_DIR/../deploy-ec2.sh"

curl_attempts=0
sleep_calls=0
docker_commands=()

curl() {
  curl_attempts=$((curl_attempts + 1))
  if ((curl_attempts < 3)); then
    return 7
  fi
  return 0
}

sleep() {
  sleep_calls=$((sleep_calls + 1))
}

docker() {
  docker_commands+=("$*")
}

HOMEOPS_BACKEND_READINESS_ATTEMPTS=5
HOMEOPS_BACKEND_READINESS_DELAY_SECONDS=0
if ! wait_for_backend; then
  echo "retry-success case failed" >&2
  exit 1
fi
[[ "$curl_attempts" == "3" ]]
[[ "$sleep_calls" == "2" ]]
printf '%s\n' "PASS: backend readiness succeeds after transient failures"

curl_attempts=0
sleep_calls=0
docker_commands=()

curl() {
  curl_attempts=$((curl_attempts + 1))
  return 7
}

if wait_for_backend; then
  echo "retry-exhaustion case unexpectedly passed" >&2
  exit 1
fi
[[ "$curl_attempts" == "5" ]]
[[ "$sleep_calls" == "4" ]]
[[ "${#docker_commands[@]}" == "2" ]]
[[ "${docker_commands[0]}" == "compose ps backend" ]]
[[ "${docker_commands[1]}" == "compose logs --tail=100 backend" ]]
printf '%s\n' "PASS: backend readiness exhaustion fails with diagnostics"

runtime_dir="$(mktemp -d)"
trap 'rm -rf "$runtime_dir"' EXIT
REPO_DIR="$runtime_dir"
mkdir -p "$REPO_DIR/dashboard"
ssm_value() {
  case "$1" in
    */gemini-api-key) printf '%s' 'redacted-test-gemini-key' ;;
    */ask-homeops-oidc-issuer) printf '%s' 'https://issuer.example.test/pool' ;;
    */ask-homeops-oidc-audience) printf '%s' 'client-id' ;;
    */ask-homeops-oidc-audience-claim) printf '%s' 'client_id' ;;
    */ask-homeops-oidc-jwks-url) printf '%s' 'https://issuer.example.test/pool/.well-known/jwks.json' ;;
    */ask-homeops-diagnostic-scope) printf '%s' 'https://api.homeops.now/diagnostic:read' ;;
    */ask-homeops-limiter-backend) printf '%s' 'redis' ;;
    */ask-homeops-redis-url) printf '%s' 'redis://127.0.0.1:6379/0' ;;
    *) return 1 ;;
  esac
}
write_runtime_env
[[ "$(stat -c '%a' "$REPO_DIR/dashboard/.env")" == "600" ]]
grep -q '^ASK_HOMEOPS_LIMITER_BACKEND=redis$' "$REPO_DIR/dashboard/.env"
grep -q '^ASK_HOMEOPS_OIDC_AUDIENCE_CLAIM=client_id$' "$REPO_DIR/dashboard/.env"
printf '%s\n' "PASS: runtime environment refresh writes protected auth/Valkey settings"
