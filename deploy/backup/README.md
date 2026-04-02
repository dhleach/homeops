# JSONL Log Backup

Backs up rotated JSONL log files from observer and consumer services with 90-day retention.

## Files

- `backup-jsonl-logs.sh` — Daily backup script. Copies rotated .gz files to `state/backups/`, maintains 90-day retention.
- `../systemd/homeops-backup-jsonl.service` — systemd service unit (oneshot)
- `../systemd/homeops-backup-jsonl.timer` — systemd timer unit (daily at 3 AM UTC)

## Installation on Pi

```bash
# Copy script to Pi
scp -i ~/.ssh/id_ed25519 deploy/backup/backup-jsonl-logs.sh bob@100.115.21.72:/tmp/
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 sudo cp /tmp/backup-jsonl-logs.sh /home/leachd/repos/homeops/deploy/backup/
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 sudo chmod +x /home/leachd/repos/homeops/deploy/backup/backup-jsonl-logs.sh

# Copy systemd units to Pi
scp -i ~/.ssh/id_ed25519 deploy/systemd/homeops-backup-jsonl.{service,timer} bob@100.115.21.72:/tmp/
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 sudo cp /tmp/homeops-backup-jsonl.* /etc/systemd/system/

# Enable and start the timer
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 sudo systemctl daemon-reload
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 sudo systemctl enable --now homeops-backup-jsonl.timer
```

## Verification

```bash
# Check timer status
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 systemctl status homeops-backup-jsonl.timer

# Check last run
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 journalctl -u homeops-backup-jsonl.service -n 20

# List backups
ssh -i ~/.ssh/id_ed25519 bob@100.115.21.72 ls -lh /home/leachd/repos/homeops/state/backups/
```

## Design

- **When**: Daily at 3 AM UTC (10 PM EDT) — outside prime HVAC hours to avoid I/O contention
- **What**: Copies all rotated (gzipped, dated) JSONL files from observer and consumer
- **Retention**: 90 days (configurable in script)
- **Location**: `state/backups/` with naming: `observer_events.jsonl-YYYYMMDD.gz`, `consumer_events.jsonl-YYYYMMDD.gz`
- **Log**: Backup script writes to `state/backups/backup.log` (timestamps, counts, cleanup actions)

## Testing

Manual test (Pi):
```bash
bash /home/leachd/repos/homeops/deploy/backup/backup-jsonl-logs.sh
```

Check result:
```bash
ls -la /home/leachd/repos/homeops/state/backups/
cat /home/leachd/repos/homeops/state/backups/backup.log
```
