# logrotate configuration

Prevents the observer and consumer JSONL event files from growing unbounded on the Raspberry Pi.

Without rotation, each JSONL file grows ~1–5 MB/day depending on sensor activity. The 30-day retention policy caps total disk usage at roughly 150 MB across both files — well within safe limits for a Pi SD card.

## Install

```bash
sudo cp deploy/logrotate/homeops /etc/logrotate.d/homeops
```

logrotate runs daily via the system cron job (`/etc/cron.daily/logrotate`). No additional configuration is needed after installation.

## Test (dry run)

Verify the config parses correctly and see what logrotate would do, without actually rotating anything:

```bash
sudo logrotate --debug /etc/logrotate.d/homeops
```

## Force a manual rotation

Trigger rotation immediately, regardless of whether the daily interval has elapsed:

```bash
sudo logrotate --force /etc/logrotate.d/homeops
```

## Notes

### `copytruncate` — zero-downtime rotation

The config uses `copytruncate` instead of the default rename-and-recreate approach. This means logrotate:

1. Copies the current log file to a new rotated file (e.g. `events.jsonl-20260320`).
2. Truncates the original file to zero bytes in place.

The observer and consumer systemd services keep their file handles open to the original path throughout, so they continue writing without interruption or restart. There is a brief window between copy and truncate where a small number of events could be duplicated in the rotated file — this is acceptable for this use case.

### Rotated filename format

Rotated files are named with a `YYYY-MM-DD` date suffix and compressed with gzip:

```
events.jsonl-20260320.gz
events.jsonl-20260319.gz
...
```
