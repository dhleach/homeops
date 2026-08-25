# homeops

**Live dashboard → [homeops.now](https://homeops.now) · API → [api.homeops.now/api/current-temps](https://api.homeops.now/api/current-temps)**

A full-stack observability platform for a 3-zone home HVAC system — event-driven Python pipeline on a Raspberry Pi 5, live metrics in Prometheus + Grafana on AWS EC2, React dashboard on S3 + CloudFront, FastAPI backend, all provisioned with Terraform. 26 derived event types, 1048 Python tests, and 34 React component tests.

## The Problem

Floor 2 has only 3 vents. When it calls for heat for an extended period, the furnace blasts through too few open vents, overheats, and trips the high-limit switch (Code 4/7). The result: a multi-hour furnace lockout and a cold house.

Home Assistant alone can't prevent this. It sees state changes; it doesn't reason about them. **homeops** does.

## What It Does

- **Real-time overheating prevention** — tracks how long floor 2 has been calling for heat; fires a Telegram alert before the limit switch trips (configurable threshold, default 45 min)
- **26 derived event types** from raw HA state changes and explicit HA mitigation decisions — floor call sessions, floor heating performance, furnace sessions, furnace diagnostics, thermostat setpoint/mode/temp changes, zone temperature snapshots, outdoor temperature, daily summaries, system watchdog, and staged zone-stagger outcomes
- **Heating cycle analytics** — `zone_time_to_temp` (how fast each zone heats), `zone_overshoot` (how far past setpoint the zone runs), `zone_setpoint_miss` (calls that fail to reach setpoint), `zone_slow_to_heat_warning` (zones heating below expected rate), and furnace duty cycle via `furnace_duty_cycle.py`
- **Thermostat entity tracking** — setpoint changes, mode changes, current temp updates, and setpoint-reached events per zone
- **Event-driven pipeline** — observer writes raw `state_changed` and explicit mitigation-decision events to JSONL; consumer tails that file and emits semantically rich derived events downstream
- **Schema-versioned events** — every event carries a `schema` field (e.g. `homeops.consumer.floor_2_long_call_warning.v1`) for safe downstream evolution
- **Production-grade operations** — runs as `systemd` services on the Pi, log rotation via `logrotate`, exponential-backoff reconnects on the WebSocket
- **1048 Python tests + 34 React component tests**, GitHub Actions CI, Ruff lint/format enforcement on every PR, and post-deploy public smoke checks
- **Opt-in mitigation overlay** — staged Home Assistant zone-call staggering with a disabled-by-default guard and validated timing projections; it is not deployed by the normal application release

## Architecture

```
┌──────────────────── Raspberry Pi 5 ─────────────────────┐
│                                                          │
│  Home Assistant                                          │
│    WebSocket API                                         │
│         │                                                │
│         ▼                                                │
│    observer.py ──► state/observer/events.jsonl           │
│                               │                          │
│                               ▼                          │
│                         consumer.py ──► /metrics         │
│                               │         (Prometheus      │
│                 ┌─────────────┤          exposition)     │
│                 ▼             ▼                          │
│        state/consumer/   Telegram alert                  │
│        events.jsonl      (floor-2 long call)             │
│       (derived events)                                   │
└──────────────────────────────────────────────────────────┘
                              │ scrape every 15s (Tailscale)
                              ▼
┌──────────────────── AWS EC2 ─────────────────────────────┐
│                                                          │
│  Prometheus  ◄──────────────────────────────────────     │
│       │                                                  │
│       ▼                                                  │
│   Grafana  (4 provisioned dashboards)                    │
│       │                                                  │
│   FastAPI  /api/current-temps  ◄── Prometheus query      │
│       │                                                  │
│   Nginx  (api.homeops.now → FastAPI + Grafana)           │
└──────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┤
          ▼                   ▼
    S3 + CloudFront      api.homeops.now
    homeops.now          (FastAPI JSON)
    (React frontend)
```
**Full architecture diagrams:** [`docs/architecture/phase1.svg`](docs/architecture/phase1.svg) (Pi + event pipeline) · [`docs/architecture/phase2.svg`](docs/architecture/phase2.svg) (AWS stack + full system)

For the verified address/port map, active-versus-migration boundaries, and
deployment ownership model, see [`docs/architecture.md`](docs/architecture.md).
For the release sequence, Pi permission model, rollback guidance, and smoke
checks, see [`docs/deployment.md`](docs/deployment.md).
For the proposed HVAC mitigation guardrails and fail-safe policy, see
[`docs/mitigation-policy.md`](docs/mitigation-policy.md).


**Observer** connects to the Home Assistant WebSocket API, subscribes to `state_changed` events for configured entities, and writes one JSON line per event to a JSONL log. It reconnects automatically with exponential backoff.

**Consumer** tails the observer log in real time. It routes each event by entity ID, maintains per-zone heating session state, emits higher-level derived events, and exports live Prometheus metrics via `/metrics`.

**EC2 dashboard stack** — Prometheus scrapes the Pi's `/metrics` endpoint every 15 seconds over Tailscale and the backend's internal diagnostic metrics over EC2 loopback. Grafana reads from Prometheus and serves 4 provisioned dashboards. FastAPI queries Prometheus and exposes structured JSON at `/api/current-temps`; Ask HomeOps requires a Cognito-verified bearer principal, independent per-user/IP quota state in loopback-only Valkey, a process-wide provider-call backstop, low-cardinality abuse/cost metrics, and a deterministic read-only prompt-safety boundary. The browser uses authorization code + PKCE and never receives a client secret. Nginx proxies the public API surfaces behind a TLS subdomain but does not expose backend `/metrics`. The active production stack uses Docker Compose with host networking; the Kubernetes manifests are a separate migration surface.

**Frontend** — React + Tailwind, built by GitHub Actions and deployed to S3/CloudFront when frontend files or its deploy workflow change on `master`.

All infrastructure (EC2, S3, CloudFront, Route53, ACM, IAM) is managed with Terraform.

## Event Types

The consumer emits **25 derived event types**:

| Category | Events |
|---|---|
| Floor heating calls | `floor_call_started.v1`, `floor_call_ended.v1` |
| Floor diagnostics | `floor_no_response_warning.v1`, `floor_not_responding.v1`, `floor_runtime_anomaly.v1` |
| Floor-2 overheating | `floor_2_long_call_warning.v1`, `floor_2_long_call_escalation.v1` |
| Furnace sessions | `heating_session_started.v1`, `heating_session_ended.v1`, `heating_long_session_warning.v1`, `heating_short_session_warning.v1` |
| Furnace diagnostics | `furnace_short_call_warning.v1` |
| Thermostat state | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1` |
| Heating performance | `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_setpoint_miss.v1`, `zone_slow_to_heat_warning.v1`, `zone_temp_snapshot.v1` |
| Environmental | `outdoor_temp_updated.v1` |
| System | `observer_silence_warning.v1` |
| Summaries | `furnace_daily_summary.v1`, `floor_daily_summary.v1` |

All events share a common envelope (`schema`, `source`, `ts`, `data`) and are written as newline-delimited JSON.

Full schema reference: [`docs/event-schemas/consumer-events.md`](docs/event-schemas/consumer-events.md)
Consumer service detail: [`services/consumer/README.md`](services/consumer/README.md)

**Example — floor-2 overheating warning:**

```json
{
  "schema": "homeops.consumer.floor_2_long_call_warning.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T09:32:18.005600+00:00",
  "data": {
    "floor": "floor_2",
    "elapsed_s": 2714,
    "threshold_s": 2700,
    "entity_id": "binary_sensor.floor_2_heating_call"
  }
}
```

**Example — zone heating performance:**

```json
{
  "schema": "homeops.consumer.zone_time_to_temp.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T07:43:12.004821+00:00",
  "data": {
    "zone": "floor_1",
    "start_temp": 64.5,
    "setpoint": 68.0,
    "setpoint_delta": 3.5,
    "duration_s": 1140,
    "degrees_per_min": 0.189,
    "outdoor_temp_f": 28.4,
    "other_zones_calling": ["binary_sensor.floor_3_heating_call"]
  }
}
```

## Repository Layout

```text
homeops/
├── infra/                        # Terraform — AWS infrastructure for homeops.now
│   ├── main.tf                   # provider config
│   ├── variables.tf              # input variables
│   ├── networking.tf             # VPC, security groups
│   ├── ec2.tf                    # EC2 instance, EBS, Elastic IP, SSH key
│   ├── s3.tf                     # S3 frontend bucket, OAC
│   ├── cloudfront.tf             # CloudFront distribution, SPA router function
│   ├── dns_cert.tf               # Route53 hosted zone, ACM cert, DNS records
│   ├── iam.tf                    # EC2 IAM role + instance profile
│   └── outputs.tf                # EC2 IP, CF distribution ID, S3 bucket name
├── dashboard/
│   ├── backend/                  # FastAPI — telemetry and diagnostic APIs
│   └── frontend/                 # React + Tailwind — homeops.now public site
├── compose/
│   └── docker-compose.yml        # Home Assistant container
├── homeassistant/
│   ├── automations.yaml          # staged, opt-in zone-call mitigation automation
│   ├── helpers.yaml              # guard and validated timing projections
│   └── README.md                  # isolated HA test-install instructions
├── services/
│   ├── insights/
│   │   ├── rules.yaml           # validated rule thresholds and enablement
│   │   └── rules/               # pure insight-rule implementations
│   ├── observer/
│   │   ├── observer.py
│   │   ├── requirements.txt
│   │   └── README.md
│   └── consumer/
│       ├── consumer.py           # lean entry point / main loop
│       ├── constants.py          # entity maps, env config, shared constants
│       ├── utils.py              # utc_ts, follow, append_jsonl, _parse_dt
│       ├── state.py              # state persistence and bootstrap logic
│       ├── processors.py         # floor, furnace, climate, outdoor-temp handlers
│       ├── alerts.py             # floor-2 warning, escalation, silence, temp snapshot
│       ├── reporting.py          # daily summary generation and formatting
│       ├── requirements.txt
│       └── README.md             # full consumer reference
├── scripts/
│   ├── query_floor_runtime.py         # CLI: per-floor runtime summary for a date range
│   ├── floor_runtime_trend.py         # CLI: day-by-day runtime trend table (last N days)
│   ├── floor_hourly_heatmap.py        # CLI: hourly zone-call frequency table for a date range
│   ├── furnace_duty_cycle.py          # CLI: furnace duty cycle % for any time window
│   ├── furnace_session_analysis.py    # CLI: correlate furnace session length vs outdoor temp (CSV output)
│   ├── furnace_temp_scatter.py        # CLI: daily outdoor temp + furnace runtime CSV data
│   ├── generate_report.py              # CLI: self-contained HTML runtime/trend charts
│   ├── runtime_temp_anomalies.py       # CLI: temperature-adjusted floor runtime anomalies
│   ├── zone_heat_loss.py              # CLI: per-zone cooling-curve heat-loss rates
│   ├── runtime_per_degree.py           # CLI: per-zone furnace runtime per degree gained
│   ├── time_to_temp.py                 # CLI: per-zone time-to-temperature model/prediction
│   ├── temp_correlation.py            # CLI: Pearson correlation — outdoor temp vs floor runtime
│   ├── validate_floor_aggregation.py # dev: validate floor_daily_summary totals vs raw events
│   ├── validate_anomalies.py         # read-only replay/report for anomaly detectors
│   ├── analyze_multi_zone_impact.py  # read-only contention analysis/report
│   ├── deploy_smoke_check.py          # release gate for public frontend/API/observability
│   └── test_counts.py                 # CI-generated canonical test-count validation
├── docs/
│   ├── architecture.md               # verified topology, addresses, ports, and boundaries
│   ├── deployment.md                 # CI/CD sequence, permissions, rollback, smoke checks
│   ├── data-model.md                 # normalized floor-call session/statistics contracts
│   ├── test-counts.json              # CI-verified Python and React test counts
│   └── event-schemas/
│       └── consumer-events.md    # authoritative event schema reference
├── deploy/
│   ├── deploy-pi.sh               # owner-scoped Pi deploy streamed by CI
│   ├── deploy-ec2.sh              # EC2 backend deploy + host-local checks
│   ├── sudoers/homeops-github-deploy # canonical least-privilege Pi policy
│   ├── tests/test-pi-sudoers.sh   # static/visudo coverage for the Pi policy
│   ├── tests/test-deploy-ec2.sh   # shell coverage for the EC2 readiness gate
│   └── logrotate/                 # logrotate config for JSONL files
├── state/
│   ├── observer/events.jsonl     # runtime output, gitignored
│   └── consumer/events.jsonl    # derived events, gitignored
├── .githooks/
│   └── pre-commit
├── pyproject.toml                # Ruff lint/format config
└── secrets/
    └── ha.env                    # local only, gitignored
```

## Prerequisites

- Docker + Docker Compose
- Python 3.11+
- A Home Assistant long-lived access token

## Setup

1. Start Home Assistant:

```bash
cd compose
docker compose up -d
```

2. Create a Python virtualenv and install dependencies:

```bash
cd services/observer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r ../consumer/requirements.txt
pip install ruff
```

Both services share one virtualenv for local development. Runtime dependencies are split across service-level `requirements.txt` files for clearer deployment ownership.

3. Create `secrets/ha.env` (already gitignored):

```dotenv
HA_BASE_URL=http://127.0.0.1:8123
HA_WS_URL=ws://127.0.0.1:8123/api/websocket
HA_TOKEN=<your_long_lived_token>
WATCH_ENTITIES=binary_sensor.furnace_heating,binary_sensor.floor_1_heating_call,binary_sensor.floor_2_heating_call,binary_sensor.floor_3_heating_call,climate.floor_1_thermostat,climate.floor_2_thermostat,climate.floor_3_thermostat,sensor.outdoor_temperature
OBSERVER_EVENT_LOG=state/observer/events.jsonl
```

4. Enable the pre-commit hook:

```bash
git config core.hooksPath .githooks
```

## Running

**Observer** (from repo root):

```bash
HA_ENV_FILE=secrets/ha.env services/observer/.venv/bin/python services/observer/observer.py
```

**Consumer** (from repo root):

```bash
EVENT_LOG=state/observer/events.jsonl \
DERIVED_EVENT_LOG=state/consumer/events.jsonl \
TELEGRAM_BOT_TOKEN=<bot-token> \
TELEGRAM_CHAT_ID=<chat-id> \
services/observer/.venv/bin/python services/consumer/consumer.py
```

Omit the `TELEGRAM_*` variables to run without alerts.

Run both services in separate terminals or deploy as `systemd` units.

## Environment Variables

### Observer

| Variable | Description |
|---|---|
| `HA_WS_URL` | Home Assistant WebSocket endpoint |
| `HA_TOKEN` | Long-lived Home Assistant access token |
| `WATCH_ENTITIES` | Comma-separated entity IDs to filter (empty = all entities) |
| `HA_ENV_FILE` | Path to dotenv file (default: `secrets/ha.env`) |
| `OBSERVER_EVENT_LOG` | JSONL append path for observer output |

### Consumer

| Variable | Default | Description |
|---|---|---|
| `EVENT_LOG` | `state/observer/events.jsonl` | Observer output file to tail |
| `DERIVED_EVENT_LOG` | `state/consumer/events.jsonl` | Derived event output path |
| `HOMEOPS_RULES_CONFIG` | `services/insights/rules.yaml` | Validated rule thresholds and enabled flags |
| `TELEGRAM_BOT_TOKEN` | _(unset)_ | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | _(unset)_ | Telegram chat ID for alerts |

The consumer loads `services/insights/rules.yaml` once at startup. It controls
the thresholds and `enabled` flag for floor-runtime anomalies, no-response and
slow-to-heat warnings, furnace/session warnings, floor-2 escalation, observer
silence, efficiency insights, time-of-day insights, and the outdoor-temperature
storm detector. Set `HOMEOPS_RULES_CONFIG` to a separately validated file for a
controlled test or rollback; malformed or missing configuration stops startup
with an actionable error.

## Development

### Linting and Formatting

Ruff is configured via `pyproject.toml`.

Check:

```bash
services/observer/.venv/bin/ruff check services/
services/observer/.venv/bin/ruff format --check services/
```

Apply fixes:

```bash
services/observer/.venv/bin/ruff format services/
services/observer/.venv/bin/ruff check --fix services/
```

### Tests

```bash
pip install pytest pytest-asyncio respx PyYAML
pip install -r services/consumer/requirements.txt \
  -r services/observer/requirements.txt \
  -r dashboard/backend/requirements.txt
PYTHONPATH=services/consumer:services/observer:services/insights:dashboard/backend:scripts \
  python3 -m pytest --import-mode=importlib \
  services/observer/tests services/consumer/tests services/insights/rules/tests \
  dashboard/backend/tests scripts/tests
NODE_ENV=test npm --prefix dashboard/frontend test
```

1048 Python tests cover observer reconnect logic, consumer event derivation, floor-2 long-call warning and escalation, thermostat tracking, heating cycle analytics, consumer state persistence, Prometheus metrics gauge updates, the FastAPI backend, Ask HomeOps authentication/quota/budget/observability/prompt-safety guards, deployment smoke checks, insights engine rules, historical anomaly replay/reporting, multi-zone impact analysis, hourly zone-call frequency reporting, the floor-call data-model contract, daily furnace temperature/runtime scatter export, the self-contained HTML trend report, temperature-adjusted runtime anomaly analysis, zone cooling-curve heat-loss analysis, per-zone furnace-runtime-per-degree efficiency analysis, per-zone time-to-temperature modeling, shared rule configuration validation, enabled-rule gates, outdoor-temperature storm detection, direct consumer import-path validation, staged Home Assistant mitigation configuration and event logging, and test-count validation. The frontend has 34 React component tests. The canonical counts live in [`docs/test-counts.json`](docs/test-counts.json) and are verified against CI runner output.

### CI

GitHub Actions runs Ruff lint and format checks on every PR and push to `master`. The CI test job runs the complete Python and React suites, generates JUnit/Vitest reports, and fails if the canonical count or README claims drift. Production deploys run host-local checks and then verify the public frontend, API, Grafana, and Prometheus endpoints before succeeding.

### Branching

- `master` — stable, production-ready. All PRs merge here.
- `feature/<short-description>` — new features
- `fix/<short-description>` — bug fixes
- `docs/<short-description>` — documentation

All commits must pass Ruff checks (enforced by `.githooks/pre-commit` and CI).

## Query Scripts

### `scripts/query_floor_runtime.py`

Query per-floor heating runtime from `floor_daily_summary.v1` events:

```bash
# Summary for January 2026
python3 scripts/query_floor_runtime.py --start 2026-01-01 --end 2026-01-31

# Filter to floor 2 only
python3 scripts/query_floor_runtime.py --start 2026-01-01 --end 2026-01-31 --floor floor_2

# Custom log path
DERIVED_EVENT_LOG=/path/to/events.jsonl python3 scripts/query_floor_runtime.py ...
```

Output:
```
Floor runtime summary: 2026-01-01 → 2026-01-31

Floor       |  Days |  Total Runtime |       Avg Daily |  Max Single Day
------------------------------------------------------------------------
floor_1     |    28 |       45h 12m  |         1h 37m  |         3h 10m
floor_2     |    22 |       38h 44m  |         1h 46m  |         4h 22m
floor_3     |    31 |       28h 05m  |           54m   |         1h 45m
```

### `scripts/floor_runtime_trend.py`

Day-by-day floor runtime trend table from `floor_daily_summary.v1` events:

```bash
# All floors, last 30 days (default)
python3 scripts/floor_runtime_trend.py

# Last 14 days
python3 scripts/floor_runtime_trend.py --days 14

# Detailed view for floor 2 only
python3 scripts/floor_runtime_trend.py --floor floor_2 --days 14

# Custom log path
DERIVED_EVENT_LOG=/path/to/events.jsonl python3 scripts/floor_runtime_trend.py
```

Output (all floors):
```
Date          Floor 1      Floor 2      Floor 3      Outdoor
------------------------------------------------------------
2026-03-31     2h 14m       4h 33m       1h 20m       42°F
2026-03-30     1h 50m       3h 10m          55m       45°F
```

Output (`--floor floor_2`):
```
Floor 2 — 14-day trend
Date           Runtime    Calls   Avg Call   Max Call    Outdoor
---------------------------------------------------------------
2026-03-31      4h 33m        8    34m        1h 22m      42°F
```

### `scripts/furnace_duty_cycle.py`

Compute furnace duty cycle for any time window from `heating_session_ended.v1` events:

```bash
# Single day
python3 scripts/furnace_duty_cycle.py --start 2026-01-15 --end 2026-01-15

# Date range
python3 scripts/furnace_duty_cycle.py --start 2026-01-01 --end 2026-01-31

# Sub-day window
python3 scripts/furnace_duty_cycle.py --start "2026-01-15T06:00" --end "2026-01-15T18:00"
```

Sessions that span window boundaries are clipped to the window. Output: duty cycle percentage, total on-time, and window duration.

### `scripts/furnace_session_analysis.py`

Correlate furnace session length with outdoor temperature from `heating_session_ended.v1` events:

```bash
# All sessions
python3 scripts/furnace_session_analysis.py

# With CSV output
python3 scripts/furnace_session_analysis.py --out state/furnace_session_correlation.csv

# Last N days
python3 scripts/furnace_session_analysis.py --days 30
```

Output: per-session CSV with `started_at`, `ended_at`, `duration_s`, `outdoor_temp_f`, plus Pearson correlation coefficient between session length and outdoor temperature.

### `scripts/furnace_temp_scatter.py`

Export one row per observed UTC calendar day with average outdoor temperature and
total furnace runtime in minutes. The exporter averages raw
`outdoor_temp_updated.v1` readings, prefers `furnace_daily_summary.v1` for
runtime (so zero-runtime days are retained), and falls back to completed raw
furnace sessions when a daily summary is absent. Missing values remain blank,
and the command reports how many rows are complete scatter points.

```bash
python3 scripts/furnace_temp_scatter.py \
  --start 2026-03-20 \
  --end 2026-08-21 \
  --log state/consumer/events.jsonl \
  --out state/furnace_temp_scatter.csv
```

Output columns are `date`, `avg_temp_f`, and `furnace_runtime_min`, sorted by
date. The report is read-only and does not change consumer state or event logs.

### `scripts/generate_report.py`

Generate one self-contained HTML report with an inline-SVG line chart for daily
runtime by floor and an inline-SVG scatter plot of outdoor temperature versus
whole-furnace runtime. The report composes the existing
`floor_daily_summary.v1` and `furnace_temp_scatter.py` parsing contracts,
preserves missing values as gaps/`—`, and includes the underlying daily table
and complete/partial coverage counts.

```bash
# Trailing 30 days (default)
python3 scripts/generate_report.py

# Reproducible explicit range and custom output
python3 scripts/generate_report.py \
  --start 2026-03-20 \
  --end 2026-08-21 \
  --log state/consumer/events.jsonl \
  --out reports/hvac_trend.html
```

The default output is `reports/hvac_trend.html`. It opens directly in a
browser without a HomeOps service, JavaScript bundle, or network connection;
the command only reads the derived event log.

### `scripts/runtime_temp_anomalies.py`

Find unusually high daily runtime for a floor after accounting for average
outdoor temperature. The read-only report fits one per-floor runtime-versus-
temperature model, scores positive residuals with a robust MAD scale, and marks
floors with insufficient history explicitly. Candidates are evidence for review,
not equipment-fault diagnoses; lower runtime is not flagged because a missing
heating call can be a normal setpoint or schedule decision.

```bash
# Last 30 UTC days (default)
python3 scripts/runtime_temp_anomalies.py --log state/consumer/events.jsonl

# Reproducible range and JSON output
python3 scripts/runtime_temp_anomalies.py \
  --start 2026-04-01 \
  --end 2026-08-21 \
  --log state/consumer/events.jsonl \
  --format json \
  --out reports/runtime-anomalies.json
```

The input is `floor_daily_summary.v1`; the command does not start the consumer
or modify state or event logs.

### `scripts/zone_heat_loss.py`

Estimate per-zone heat loss from thermostat cooling curves. The read-only
analysis pairs a floor-call end with the next call, excludes samples while the
furnace or thermostat is active, splits telemetry gaps, and fits a temperature
decay rate in °F/min. It reports per-zone coverage and insufficient-data states;
rates are observations, not insulation or equipment diagnoses.

```bash
python3 scripts/zone_heat_loss.py \
  --log state/consumer/events.jsonl \
  --start 2026-03-20 \
  --end 2026-05-31 \
  --out reports/zone-heat-loss.md
```

The command is historical and read-only. It requires known furnace-off
transitions and does not modify consumer state or event logs.

### `scripts/runtime_per_degree.py`

Measure the furnace on-time required for a zone to gain one degree Fahrenheit.
The report pairs completed floor calls with overlapping completed furnace runs,
uses nearby bracketed thermostat readings only when their ages are within the
configured quality window, and groups valid ratios by zone and half-open
outdoor-temperature bucket. Incomplete calls, stale or missing readings,
non-positive temperature rises, and calls without measured furnace on-time are
counted explicitly rather than becoming misleading zeroes. Furnace on-time is
attributed to each overlapping zone call, so zone values are not additive.

```bash
python3 scripts/runtime_per_degree.py \
  --log state/consumer/events.jsonl \
  --start 2026-03-20 \
  --end 2026-05-31 \
  --format json \
  --out reports/runtime-per-degree.json
```

The JSON artifact is suitable for a future daily-summary consumer. The command
does not emit a production event, change thermostat settings, or modify
consumer state or event logs. See
[`docs/runtime-per-degree-2026-08.md`](docs/runtime-per-degree-2026-08.md) for
the latest Pi-history snapshot and its telemetry limitations.

### `scripts/time_to_temp.py`

Build a deterministic per-zone estimate of the time required to reach a
requested setpoint. The read-only model uses completed `zone_time_to_temp.v1`
events, fits seconds-per-degree against outdoor temperature for each zone, and
scales the result by the requested positive setpoint delta. The report also
retains half-open outdoor-temperature buckets and marks sparse zones or
out-of-range queries explicitly.

```bash
# Render all zone models for the trailing 30 UTC days
python3 scripts/time_to_temp.py --log state/consumer/events.jsonl

# Query a zone over a reproducible historical range
python3 scripts/time_to_temp.py \
  --zone floor_2 \
  --outdoor 30 \
  --delta 3 \
  --start 2026-03-20 \
  --end 2026-08-23 \
  --log state/consumer/events.jsonl \
  --format json \
  --out reports/time-to-temp.json
```

The completed event is emitted alongside `thermostat_setpoint_reached.v1`,
but is the source used here because it carries the measured duration and
setpoint delta. This command does not emit a production event, change
thermostat settings, or modify consumer state or event logs. See
[`docs/time-to-temp-2026-08.md`](docs/time-to-temp-2026-08.md) for the latest
Pi-history snapshot and its telemetry limitations.

### `scripts/validate_anomalies.py`

Replay the production floor-runtime and furnace-session anomaly detectors over a
complete derived-event history. The command is read-only, compares replayed
warnings with warnings already emitted, and reports when the log lacks the
maintenance or operator labels needed to measure fault-level false positives.

```bash
# Local derived log
PYTHONPATH=services/insights \
  python3 scripts/validate_anomalies.py --log state/consumer/events.jsonl

# JSON output for automation
PYTHONPATH=services/insights \
  python3 scripts/validate_anomalies.py --log state/consumer/events.jsonl --format json

# Stream a Pi log without writing to the Pi
ssh bob@raspberrypi 'cat /home/leachd/repos/homeops/state/consumer/events.jsonl' \
  | PYTHONPATH=services/insights python3 scripts/validate_anomalies.py --log -
```

See [`docs/anomaly-validation-2026-08.md`](docs/anomaly-validation-2026-08.md)
for the latest reviewed Pi-history snapshot.

### `scripts/analyze_multi_zone_impact.py`

Group `zone_time_to_temp.v1` events by target zone and the other zones calling
at session start. The report includes exact sample counts, median duration and
heating-rate statistics, and returns `insufficient_data` instead of making a
scheduling claim when the comparison groups are too small.

```bash
PYTHONPATH=services/insights \
  python3 scripts/analyze_multi_zone_impact.py --log state/consumer/events.jsonl
```

See [`docs/multi-zone-impact-analysis-2026-08.md`](docs/multi-zone-impact-analysis-2026-08.md)
for the latest Pi-history result.

### `scripts/floor_hourly_heatmap.py`

Count `floor_call_started.v1` events by local hour for each floor over an
explicit inclusive date range. The default timezone is
`America/New_York`; pass another IANA timezone when comparing UTC or a
different operating location. The report is read-only and shows malformed
input, out-of-range events, per-floor totals, and peak hours.

```bash
# Seven local calendar days from the local derived log
python3 scripts/floor_hourly_heatmap.py \
  --log state/consumer/events.jsonl \
  --start 2026-05-11 --end 2026-05-17

# Stream a Pi log without writing to the Pi
ssh bob@raspberrypi 'cat /home/leachd/repos/homeops/state/consumer/events.jsonl' \
  | python3 scripts/floor_hourly_heatmap.py --log - \
      --start 2026-05-11 --end 2026-05-17

# JSON output for automation
python3 scripts/floor_hourly_heatmap.py \
  --log state/consumer/events.jsonl \
  --start 2026-05-11 --end 2026-05-17 --format json
```

See [`docs/hourly-zone-call-heatmap-2026-08.md`](docs/hourly-zone-call-heatmap-2026-08.md)
for the latest seven-day Pi-history snapshot.

### `scripts/temp_correlation.py`

Pearson correlation between outdoor temperature and per-floor daily heating runtime from `floor_daily_summary.v1` events:

```bash
# All floors, all data
python3 scripts/temp_correlation.py

# Last 60 days
python3 scripts/temp_correlation.py --days 60

# Single floor
python3 scripts/temp_correlation.py --floor floor_2
```

Answers: does floor heating runtime depend on outdoor temperature, and by how much?

### `services/consumer/hvac_context.py`

Generates a compact, structured plain-text summary of current HVAC conditions and recent history for LLM input. Reads `state/consumer/state.json` and `state/consumer/events.jsonl`.

```bash
# Default: 48h lookback, output to stdout
python3 services/consumer/hvac_context.py

# Custom lookback window
python3 services/consumer/hvac_context.py --hours 24

# Write to file
python3 services/consumer/hvac_context.py --output /tmp/hvac_context.txt
```

Output includes current zone temps and setpoints, today's runtime by floor, yesterday's daily summary, recent heating sessions, and active warnings. Designed as the data layer for the agent-assisted recommendations epic.

## Security Notes

- Never commit secrets from `secrets/`.
- Treat `HA_TOKEN` like a password. If one is exposed, revoke it in Home Assistant immediately and generate a new one.
- The operational IPs in the architecture/deployment docs are routing metadata, not credentials. Keep tokens, private keys, and Terraform state out of Git.
- `api.homeops.now/grafana/` is the supported Grafana interface. The separate `grafana.homeops.now` DNS record is not currently a supported endpoint; see [`docs/architecture.md`](docs/architecture.md).
