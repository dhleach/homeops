#!/bin/bash
# Backup JSONL log files with 90-day retention.
#
# This script runs daily (via cron or systemd timer) and:
# 1. Copies rotated (gzipped) JSONL files to a timestamped backup directory
# 2. Maintains a 90-day retention policy
#
# Usage: bash /home/leachd/repos/homeops/deploy/backup/backup-jsonl-logs.sh
#

set -euo pipefail

# Paths
REPO_ROOT="/home/leachd/repos/homeops"
STATE_DIR="${REPO_ROOT}/state"
OBSERVER_LOG="${STATE_DIR}/observer/events.jsonl"
CONSUMER_LOG="${STATE_DIR}/consumer/events.jsonl"
BACKUP_DIR="${STATE_DIR}/backups"

# Retention (days)
RETENTION_DAYS=90

# Create backup directory if missing
mkdir -p "${BACKUP_DIR}"

# Log function
log() {
    local msg="$1"
    local ts
    ts=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
    echo "[${ts}] ${msg}" | tee -a "${BACKUP_DIR}/backup.log"
}

# Backup rotated files (gzipped, dated)
backup_rotated() {
    local log_path="$1"
    local log_name
    log_name=$(basename "${log_path}")
    local log_dir
    log_dir=$(dirname "${log_path}")
    
    # Find all rotated files matching the pattern: events.jsonl-YYYYMMDD.gz
    # Copy to backup dir with full timestamp
    local count=0
    while IFS= read -r rotated_file; do
        if [[ -f "${rotated_file}" ]]; then
            local filename
            filename=$(basename "${rotated_file}")
            cp "${rotated_file}" "${BACKUP_DIR}/${log_name%.jsonl}_${filename}"
            ((count++))
        fi
    done < <(find "${log_dir}" -name "${log_name}-[0-9][0-9][0-9][0-9][0-1][0-9][0-3][0-9].gz" -type f 2>/dev/null || true)
    
    if [[ ${count} -gt 0 ]]; then
        log "Backed up ${count} rotated file(s) from ${log_path}"
    fi
}

# Cleanup old backups (>90 days)
cleanup_old() {
    local cutoff_date
    cutoff_date=$(date -d "${RETENTION_DAYS} days ago" -u +'%Y%m%d' 2>/dev/null || date -v-${RETENTION_DAYS}d -u +'%Y%m%d')
    
    local count=0
    while IFS= read -r old_file; do
        rm -f "${old_file}"
        ((count++))
    done < <(find "${BACKUP_DIR}" -name "events.jsonl-*_*-[0-9][0-9][0-9][0-9][0-1][0-9][0-3][0-9].gz" -type f | while read -r f; do
        # Extract the date part (YYYYMMDD from the filename suffix before .gz)
        local file_date
        file_date=$(echo "${f}" | sed -E 's/.*-([0-9]{8})\.gz$/\1/')
        if [[ "${file_date}" -lt "${cutoff_date}" ]]; then
            echo "${f}"
        fi
    done || true)
    
    if [[ ${count} -gt 0 ]]; then
        log "Cleaned up ${count} old backup file(s) (>${RETENTION_DAYS} days)"
    fi
}

# Main
log "Backup started"

backup_rotated "${OBSERVER_LOG}"
backup_rotated "${CONSUMER_LOG}"
cleanup_old

log "Backup completed"
