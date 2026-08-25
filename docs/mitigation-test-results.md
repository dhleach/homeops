# Mitigation end-to-end test results

**Run date:** 2026-08-25
**Status:** Offline deterministic replay passed; live Home Assistant test
environment remains a required pre-enable gate.

## Purpose

This test verifies the complete staged path for the mitigation slices without
writing to the live Home Assistant instance or sending a real Telegram
message:

```text
simulated HA state/actions
        │
        ▼
HA mitigation event bus
        │
        ▼
real observer event wrapper + schema validator
        │
        ▼
real consumer processor + durable derived log + alert sink
```

Home Assistant's event model starts an automation, evaluates its conditions,
and then executes its actions. The replay models that state/action boundary
in memory, while using the checked-in automation file as the contract and the
real observer/consumer code for the downstream path.

## Command

Run from the repository root:

```bash
python3 scripts/mitigation_e2e.py
```

The same scenario is collected by the normal Python test command:

```bash
PYTHONPATH=services/consumer:services/observer:services/insights:dashboard/backend:scripts \
  pytest --import-mode=importlib scripts/tests/test_mitigation_e2e.py
```

## Scenario and results

The checked-in production configuration keeps `rules.mitigation.enabled: false`
and both automations `initial_state: false`. The harness enables them only in
its isolated in-memory state, then replays three fresh furnace sessions for
the same mitigation incident. Each session has a secondary floor-2 call while
floor 1 is active and the furnace is inside its warm-up window.

| Step | Simulated input | Expected result | Result |
|---|---|---|---|
| 1 | Furnace session + secondary floor-2 call | Attempt 1, stagger resumes, applied event | PASS |
| 2 | Second short furnace session + secondary floor-2 call | Attempt 2, stagger resumes, applied event; no rollback yet | PASS |
| 3 | Third short furnace session + secondary floor-2 call | Attempt 3, stagger resumes, applied event; no rollback until failure evidence | PASS |
| 4 | Continued short-cycle event matching the incident | Guard turns off; `homeops.mitigation.rollback.v1` emitted | PASS |
| 5 | Replay the same rollback observer record | No second derived record and no second alert | PASS |

The replay produced four observed output events: three
`homeops.mitigation.zone_stagger_applied.v1` records and one
`homeops.mitigation.rollback.v1` record. The real consumer appended all four,
sent exactly one urgent alert through the injected test sink, and rejected the
rollback replay as a duplicate. The final mitigation guard was off. The
short-cycle input event is deliberately not subscribed to by the observer; it
is the explicit trigger/adapter contract for the staged rollback automation.

The command prints:

```text
PASS: staged mitigation end-to-end replay
  cycle 1: attempt 1, stagger applied, rollback=no
  cycle 2: attempt 2, stagger applied, rollback=no
  cycle 3: attempt 3, stagger applied, rollback=yes
  final: mitigation_enabled=False, derived_events=4, telegram_alerts=1, replay_emitted=False
```

## Validation boundary

This is an offline HA-compatible replay, not a claim that Home Assistant was
run. There is no isolated HA core/test instance in this repository or current
workspace, so the harness does not exercise HA's Jinja renderer, service
registry, automation trace UI, or real entity state transitions. It also makes
no network calls and cannot prove the live thermostat integration accepts
`climate.set_hvac_mode`.

Before enabling the overlay on any real instance, repeat the scenario in a
separate Home Assistant test environment using Developer Tools → Events and
the automation trace. Confirm configuration validation, helper restoration,
the three state-triggered stagger runs, the matching short-cycle event, the
rollback event, and the Telegram alert. Keep the live Pi overlay disabled
until that test and a human safety review pass.
