#!/usr/bin/env bash
# Validate the tracked canonical Pi sudoers policy.
#
# Revision history:
#   2026-08-19  Added policy-content and optional visudo validation so the
#               duplicate live sudoers files have one reviewable source of truth.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
POLICY="$SCRIPT_DIR/../sudoers/homeops-github-deploy"

[[ -f "$POLICY" ]]
! grep -Eq 'NOPASSWD:[[:space:]]*ALL([[:space:]]|$)' "$POLICY"

required_lines=(
  'Cmnd_Alias HOMEOPS_GIT = /usr/bin/git -C /home/leachd/repos/homeops *'
  'Cmnd_Alias HOMEOPS_SERVICES = /usr/bin/systemctl restart homeops-observer, /usr/bin/systemctl restart homeops-consumer, /usr/bin/systemctl is-active --quiet homeops-observer, /usr/bin/systemctl is-active --quiet homeops-consumer'
  'github-deploy ALL=(leachd) NOPASSWD: HOMEOPS_GIT'
  'github-deploy ALL=(root) NOPASSWD: HOMEOPS_SERVICES'
)

for line in "${required_lines[@]}"; do
  grep -Fqx -- "$line" "$POLICY"
done

if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$POLICY" >/dev/null
else
  printf '%s\n' 'SKIP: visudo is not installed; content checks passed'
fi

printf '%s\n' 'PASS: canonical Pi sudoers policy'
