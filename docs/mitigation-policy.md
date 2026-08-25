# HVAC mitigation policy and safety constraints

**Policy version:** 1.0
**Status:** Design policy; not yet an active control loop
**Scope:** Future Home Assistant automation for furnace short-cycling and
multi-zone calls

## Purpose and boundary

This policy defines when HomeOps may propose or perform a temporary zone-call
stagger in response to repeated furnace short-cycling. It is a safety contract
for the future mitigation automation, not an authorization to change
thermostat state today.

The current consumer is read-only with respect to Home Assistant. It emits
diagnostic evidence such as:

- homeops.consumer.furnace_short_call_warning.v1 for a completed furnace
  call below the configured short-call threshold.
- homeops.consumer.heating_short_session_warning.v1 for a short furnace
  session detected by the furnace-session anomaly rule.
- homeops.insights.outdoor_temperature_storm.v1 for a rapid outdoor
  temperature drop whose furnace-runtime response is approximately flat.

These events do not currently actuate a thermostat or a zone valve. The policy
must be implemented and tested in the later Home Assistant mitigation tasks
before any control action is enabled. This documentation task does not require
Terraform or an AWS deployment change.

## Definitions

### Qualifying short-cycle occurrence

A qualifying occurrence is one completed furnace heating session that meets the
active, validated short-session or short-call threshold and has a valid end
timestamp and positive duration. A single session may produce both warning
events above; it counts **once**, keyed by its furnace session end timestamp,
not twice by warning count.

The automation must read the thresholds from the validated rules
configuration. It must not duplicate threshold literals in the Home Assistant
automation.

### Short-cycle storm

A short-cycle storm is a rate pattern, not one anomalous call:

1. At least three distinct qualifying short-cycle occurrences occur within a
   rolling 60-minute window.
2. The occurrences are associated with the same furnace and have valid,
   ordered timestamps.
3. The consumer and relevant Home Assistant states are current and readable.

An outdoor_temperature_storm event is useful supporting context, but it is
not required for the short-cycle storm trigger and must never be used as the
sole reason to actuate. A single short call produces an alert/evidence record,
not mitigation.

### Mitigation incident and attempt

An incident begins when the storm trigger is evaluated true and ends when the
mitigation is completed, rolled back, aborted, or reaches its time limit. An
attempt is one complete five-minute secondary-zone stagger, including a
verified pause, re-check, and safe resume. The incident must have a durable
identifier and attempt counter so a restart cannot silently reset its limits.

## Trigger and arming gates

Detection alone does not arm an action. All of the following gates must pass:

- The short-cycle storm definition is satisfied after deduplication.
- At least two zones are requesting heat, or a second zone requests heat while
  another zone is already active.
- Home Assistant connectivity and the required entity states are fresh,
  mutually consistent, and writable through the approved narrow service path.
- No human override is active.
- The incident has been active for less than four hours.
- Fewer than three mitigation attempts have failed.
- The target is a secondary/later zone call. The first active call is never
  interrupted solely by this policy.
- For the initial zone-stagger implementation, the furnace has been on for
  less than ten minutes at the decision point. If that condition is false, do
  not interrupt the active call; record the skipped action and alert for
  review.

The action must be rejected, with an audit record and operator notification,
when any gate is unknown or stale. “Unknown” is not equivalent to “safe.”

## Mitigation action

When all gates pass, the future automation may perform one controlled stagger:

1. Record the incident, trigger evidence, zone states, furnace state, policy
   version, and attempt number before acting.
2. Select the later/secondary zone deterministically. Do not choose a zone
   based on an unlogged or nondeterministic ordering.
3. Pause or release that secondary call for **five minutes (300 seconds)**.
   Leave the primary call unchanged.
4. Re-read Home Assistant state before resuming. If the secondary call has
   ended, do not force it back on. If the furnace is off, do not force the
   furnace on.
5. Resume the secondary call only when the original request is still present,
   the safety gates remain true, and the human override is still clear.
6. Record the outcome, including any state mismatch or service error.

Only one secondary-zone stagger may be active at a time. The automation must
not change thermostat setpoints or HVAC modes, bypass a furnace safety
interlock, or directly command the furnace. If the approved Home Assistant
service call cannot be confirmed, the automation must fail closed.

## Duration limit and termination

An incident has a hard maximum duration of **four hours** from its first
armed action. The limit applies across restarts and must not be extended by
re-triggering the same condition. At four hours:

- stop starting new stagger attempts;
- safely end any active pause without forcing a call or furnace transition;
- restore normal Home Assistant control where state can be verified;
- mark the incident expired and notify the human operator.

After expiration, a new incident requires fresh evidence and a new audit
record. There is no automatic extension.

## Failure handling and rollback

An attempt is failed when the pause/resume cannot be verified, required state
becomes unknown, the approved service call returns an error, or a new
qualifying short-cycle occurrence follows the stagger in the attempt's
evaluation window. The failure reason and relevant state must be recorded.

After **three failed attempts in one incident**, the automation must:

1. Stop all further automatic mitigation for that incident.
2. Cancel the mitigation guard and safely release any pending secondary-zone
   pause, without forcing a thermostat or furnace transition.
3. Return control to the normal Home Assistant automation only after the
   resulting state is verified.
4. Mark the incident rolled_back, record all three failures, and alert the
   human operator with the evidence needed for diagnosis.

Rollback is also mandatory immediately for a manual override, an unsafe or
contradictory state, loss of Home Assistant connectivity, an invalid policy
configuration, or any indication that the automation would affect the
primary call.

## Human override

Human control has priority over every automatic decision. The implementation
must provide a clearly visible, persistent Home Assistant override control.
While it is active:

- no new mitigation may start;
- a pending stagger must be canceled safely;
- an active stagger must not be silently extended;
- the event must be recorded with the actor/source and timestamp.

Clearing the override must not resume a previously canceled action. It only
permits a future, freshly evaluated incident. The operator can always restore
ordinary thermostat control without waiting for the four-hour limit.

## Audit log requirements

Every detection and decision must produce an append-only, structured audit
record. At minimum, record:

- UTC timestamp, policy version, incident ID, and lifecycle state;
- trigger event references, deduplicated occurrence timestamps, and thresholds;
- zone call states and furnace state before and after each decision;
- selected zone, action (armed, paused, resumed, skipped, aborted,
  rolled_back, or expired), attempt number, and result;
- the exact reason for every rejection, failure, rollback, or override;
- Home Assistant service outcome and any state-read error;
- human override actor/source when applicable.

Records must be durable across process restarts, suitable for post-incident
review, and free of credentials or access tokens. A warning notification is
not a substitute for the audit record. The future implementation may map
these records to versioned HomeOps events and the Home Assistant logbook, but
the mapping must preserve the fields above and be idempotent on retry.

## Fail-safe and recovery rules

The safe default is **no actuation**. The automation must remain disabled or
abort the current incident when:

- event history, timestamps, or thresholds cannot be parsed or validated;
- observer/consumer data is stale or the required state cannot be read;
- Home Assistant reports unavailable, conflicting, or unexpected state;
- the process restarts without recoverable incident state;
- a service response cannot be correlated to the requested action;
- a human override is active.

After a crash or restart, recover only from a durable incident record. Do not
replay an old service call. Re-read all states, preserve the attempt/failure
count, and require a new gate evaluation before any action. If recovery cannot
prove that a previous pause ended safely, alert and remain non-actuating.

## Self-review checklist

Before enabling any implementation against a live Home Assistant instance,
review and check every item:

- [ ] Three distinct furnace sessions, not three warning messages, are required
      for the storm trigger.
- [ ] Thresholds are loaded from validated configuration.
- [ ] Outdoor-temperature storm evidence is supplemental and never sufficient
      by itself.
- [ ] Only the later/secondary zone can be staggered; the primary call is
      protected.
- [ ] The pause is exactly five minutes and only one stagger can run at once.
- [ ] A four-hour incident limit survives restart and cannot be extended by
      retriggering.
- [ ] Three failed attempts force rollback, disable further action, and notify
      a human.
- [ ] Human override cancels or prevents action and never causes automatic
      resumption when cleared.
- [ ] Every decision and state transition is durably audited without secrets.
- [ ] Missing, stale, contradictory, or unverified state fails closed.
- [ ] Tests cover duplicate warning events, restarts, service failures,
      overrides, expiry, rollback, and idempotent retries.
- [ ] Production behavior still has no mitigation write path until the later
      implementation tasks are reviewed and explicitly enabled.
