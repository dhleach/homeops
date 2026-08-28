# Bob evaluation publication

The public evaluator is published at
[`https://homeops.now/bob/evals/`](https://homeops.now/bob/evals/).

## Publication boundary

`dashboard/frontend/public/bob/evals/` is a reviewed snapshot of the canonical
`openclaw-config/dashboard/evaluation/` bundle. The snapshot currently comes
from merge commit `a63a3440a9844e6a7b91124eadfecf0915e3717f` (PR #66). It
contains:

- the deterministic `evaluation-report.v1.json` release-gate report;
- 14 redacted case-evidence JSON files;
- the optional `evaluation-live-trials.v1.json` scripted fixture; and
- the self-contained static dashboard.

The deterministic report is authoritative. The live-trial fixture is labeled
optional and non-gating, uses no provider/network calls, and publishes neither
raw prompts nor raw model outputs. No production EMAIL-13 data is included.

## Refreshing the snapshot

Refresh only from a reviewed commit of the canonical evaluator repository:

```bash
SOURCE=/path/to/openclaw-config/dashboard/evaluation
DEST=dashboard/frontend/public/bob/evals
mkdir -p "$DEST"
cp "$SOURCE/index.html" "$DEST/index.html"
cp "$SOURCE/evaluation-report.v1.json" "$DEST/evaluation-report.v1.json"
cp "$SOURCE/evaluation-live-trials.v1.json" "$DEST/evaluation-live-trials.v1.json"
cp -R "$SOURCE/cases" "$DEST/"
```

Before opening a PR, verify that the evaluator repository's public-safety and
report tests pass, compare the copied file hashes, inspect the diff for raw
traces/prompts/outputs, and update this commit reference. This is deliberately
a reviewed snapshot for the first publication; an automated cross-repository
sync can be added later once its review and redaction boundary is explicit.

## Route and rollout

The CloudFront viewer-request function redirects `/bob/evals` to the canonical
trailing-slash URL and maps `/bob/evals/` to `/bob/evals/index.html`. Files
below that directory retain their extensions and pass through to S3, allowing
the dashboard's relative artifact fetches to work.
After the frontend snapshot is deployed and the Terraform route change has
propagated, run the public route check:

```bash
python3 scripts/deploy_smoke_check.py --check-bob-evaluation
```

Local preview:

```bash
python3 -m http.server 8000 --directory dashboard/frontend/public
```

Then open `http://localhost:8000/bob/evals/`.
