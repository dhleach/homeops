# HomeOps — Resume Narrative

> Source material for cover letters, resume bullets, and interview prep.
> Pull from this file when generating any resume that features HomeOps.
> Keep updated as new capabilities ship.

---

## Impact Statement

HomeOps is a full-stack HVAC observability platform running on a Raspberry Pi 5 that prevents furnace lockouts by detecting floor 2 overheating conditions before the high-limit switch trips. It processes raw Home Assistant state changes through an event-driven Python pipeline into 15 derived event types, exposes live metrics via Prometheus and Grafana on AWS, and serves a React dashboard on S3/CloudFront — all provisioned with Terraform, with 698 passing tests and CI/CD on GitHub Actions.

**One-liner (10-second resume scan):** Built a production HVAC monitoring system that prevents furnace failures in real-time: event-driven Python pipeline on Raspberry Pi, Prometheus/Grafana on AWS, Terraform IaC, 698 tests, GitHub Actions CI/CD.

---

## Key Technical Achievements

1. **Designed and shipped an event-driven IoT pipeline** processing 150+ daily Home Assistant state changes into 15 schema-versioned derived event types via JSONL persistence — zero data loss, full replay-on-restart capability.

2. **Built autonomous overheating prevention**: floor-2 long-call detector fires Telegram alerts before the furnace high-limit switch trips, preventing multi-hour lockouts. Configurable threshold (default 45 min), confirmed prevention of real furnace failures.

3. **Implemented consumer state bootstrap from JSONL replay** so accumulated daily runtimes survive restarts without metric resets — correct gauge seeding from `daily_state["per_floor_runtime_s"]` after playback.

4. **Provisioned full AWS observability stack via Terraform**: EC2 (Prometheus + Grafana + FastAPI + Nginx), S3 + CloudFront (React dashboard), automated deploy via GitHub Actions CI/CD with Tailscale OAuth — push to master, auto-deploys to Pi and EC2.

5. **Developed rolling-baseline anomaly detector** identifying per-floor runtime outliers normalized by outdoor temperature band — flags floors running significantly longer or shorter than their historical baseline for the same outdoor conditions.

6. **Built furnace session correlation analysis** (`scripts/furnace_session_analysis.py`): quantifies the relationship between furnace session length and outdoor temperature across 100+ historical sessions, confirming the floor-2 overheating mechanism.

7. **Shipped schema-versioned event system** (`homeops.consumer.<event_type>.v1`) enabling safe downstream evolution without breaking consumers — explicit versioning on all 15 event types.

8. **Maintained 698-test suite** across observer, consumer, and scripts with 100% CI green across all PRs, Ruff lint/format enforced on every commit.

---

## Skills-to-Components Mapping

Use this table to match HomeOps components to job posting keywords.

| Skill | HomeOps Component | Target Role Keywords |
|---|---|---|
| **Python / async** | `observer.py` (WebSocket), `consumer.py` (JSONL tail), all scripts | Python, asyncio, real-time systems |
| **Event-driven architecture** | observer → consumer JSONL pipeline, schema-versioned events | event streaming, distributed systems, async messaging |
| **Observability / monitoring** | Prometheus metrics exposition, Grafana dashboards (4), outdoor correlation panel | observability, SRE, metrics, dashboards |
| **Infrastructure as Code** | Terraform: EC2, S3, CloudFront, security groups, IAM, Nginx | Terraform, IaC, AWS, infrastructure automation |
| **Containerization** | Docker + docker-compose for Grafana/Prometheus stack | Docker, containerization, container orchestration |
| **CI/CD** | GitHub Actions: lint, test, Tailscale OAuth deploy to Pi and EC2 | CI/CD, GitHub Actions, deployment automation, DevOps |
| **Production operations** | systemd services, logrotate, exponential-backoff reconnects | reliability, SRE, production systems, on-call |
| **Testing / TDD** | 698 pytest tests, parametrized fixtures, synthetic 24h pipeline tests | pytest, TDD, test coverage, code quality |
| **AWS** | EC2, S3, CloudFront, FastAPI/Nginx, Prometheus remote scrape | AWS, cloud infrastructure, managed services |
| **State management / event sourcing** | Consumer `daily_state` persistence, JSONL replay, gauge restore | state management, event sourcing, stateful systems |
| **Agentic AI (OpenClaw)** | Autonomous agent with memory architecture, sub-agents, cron orchestration, self-improvement loop | AI agent, autonomous systems, LLM integration, agent orchestration |
| **ML-adjacent analytics** | Rolling-baseline anomaly detection, outdoor temp correlation, furnace session regression | anomaly detection, time-series analysis, ML systems |
| **Data pipeline** | JSONL event stream, schema validation, daily aggregation, weekly summaries | data pipeline, ETL, data engineering |

### Role-Specific Emphasis

**Duolingo — Platform Engineer II:**
Focus on: CI/CD pipeline, Docker/containerization, Terraform IaC, systemd production operations, Prometheus/Grafana observability, GitHub Actions. HomeOps demonstrates ownership of a full platform stack end-to-end.

**Eigen Labs — Senior Agentic AI Engineer:**
Focus on: OpenClaw autonomous agent (memory architecture, sub-agent orchestration, cron pipelines, self-improvement loop), HomeOps event-driven pipeline (state management, schema versioning, reliable restart semantics). Eigen wants demonstrated agent-building, not just LLM wrappers. Bob IS the portfolio piece; HomeOps demonstrates production Python systems depth.
Crypto angle: Derek has been buying crypto since 2016 and lived next to Brian Armstrong (Coinbase CEO) at Rice 2002-2005. Genuine, not performed.

---

## Interview Prep Talking Points

### "Walk me through HomeOps"
Start with the problem: floor 2 has 3 vents, furnace is binary, long calls overheat and trip the high-limit switch. Home Assistant sees state changes but doesn't reason about them. HomeOps does. Observer tails the HA WebSocket and writes raw events; consumer tails that JSONL and emits 15 derived event types with full context. Downstream: Prometheus metrics, Grafana dashboards, Telegram alerts, AWS dashboard. The hard part wasn't the code — it was figuring out the right abstraction (observer/consumer separation) and making it restart-safe without losing daily state.

### "What's the hardest technical problem you solved?"
Consumer state bootstrap: when the consumer restarts, it replays events from `last_consumed_ts` — but that misses all prior sessions today. Daily runtimes reset to zero. Fix: `restore_daily_runtimes()` seeds the gauge from `daily_state["per_floor_runtime_s"]` before entering live tail mode. The authoritative value is already persisted; playback just fills in the gap since last checkpoint.

### "How did you approach testing?"
Synthetic 24-hour pipeline tests: feed a parameterized sequence of HA state changes through the full observer/consumer stack and assert on derived events. This catches edge cases that unit tests miss — zone transitions at midnight, furnace calls that span floor switches, observer silence recovery. 698 tests total, all green on CI.

---

*Last updated: 2026-04-06*
