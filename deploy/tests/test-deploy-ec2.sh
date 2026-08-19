#!/usr/bin/env bash
# Focused tests for the EC2 deployment readiness gate.
#
# Revision history:
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
