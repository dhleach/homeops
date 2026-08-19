#!/usr/bin/env bash
# Validate the dedicated EC2 CI public key in Terraform user-data.
#
# Revision history:
#   2026-08-19  Added fingerprint coverage so the durable bootstrap key cannot
#               silently drift from the public key authorized on EC2.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_FILE="$SCRIPT_DIR/../ec2.tf"
EXPECTED_FINGERPRINT='SHA256:/zvKlLlCjUO8OhjYzBkjGvmAYLEowUpnq7XbeWLOTFM'

mapfile -t ci_keys < <(grep -E '^ssh-ed25519 [^[:space:]]+ homeops-ec2-deploy$' "$TERRAFORM_FILE")
[[ "${#ci_keys[@]}" == 1 ]]

fingerprint="$(printf '%s\n' "${ci_keys[0]}" | ssh-keygen -lf - -E sha256 | awk '{print $2}')"
[[ "$fingerprint" == "$EXPECTED_FINGERPRINT" ]]

printf 'PASS: durable EC2 CI key (%s)\n' "$fingerprint"
