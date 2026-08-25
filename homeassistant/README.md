# Home Assistant mitigation overlay

This directory contains a reviewable, opt-in Home Assistant configuration
overlay for the mitigation slices. It is not part of the normal CI deployment:
the active Pi configuration remains read-only until the later mitigation tasks
and the end-to-end HA test are complete.

## Contents

- `automations.yaml` — the disabled-by-default zone-stagger automation and the
  disabled-by-default automatic rollback automation. The stagger considers the
  newly triggered zone as secondary, requires another active zone and a recent
  furnace start, and records a durable incident/attempt number. Rollback
  listens for `homeops.mitigation.short_cycle_detected.v1` after attempt three,
  turns the guard off, and emits `homeops.mitigation.rollback.v1`.
- `helpers.yaml` — the guard, timing projections, and stateful incident helpers
  used by the automations. Timing values mirror `rules.mitigation` in
  `services/insights/rules.yaml`; the guard starts off. Attempt, incident, and
  rollback-trigger helpers intentionally omit `initial` so Home Assistant can
  restore them after a restart.

The automation uses `climate.set_hvac_mode` rather than trying to write to the
read-only `binary_sensor.*_heating_call` entities. It captures the target
climate's mode and setpoint, turns the secondary climate off for the configured
delay, and resumes only when the guard, furnace state, target state, setpoint,
and current-temperature checks still pass. A failed or changed state is left
non-actuating.

## Test-only installation

In an isolated Home Assistant test instance, merge the mappings in
`helpers.yaml` into the corresponding `input_boolean:`, `input_number:`,
`input_datetime:`, and `input_text:` sections of `configuration.yaml`, and
copy/merge the list in
`automations.yaml` into the instance's `automations.yaml`. Run Home
Assistant's configuration check and keep `mitigation_enabled` off until the
test setup is ready. Do not copy this overlay into the live Pi as part of a
normal application deploy.

The automation emits `homeops.mitigation.zone_stagger_applied.v1` after a
resume decision, with `outcome: applied` or `outcome: skipped`, plus the
incident and attempt metadata. The rollback automation accepts a
`homeops.mitigation.short_cycle_detected.v1` event containing the matching
`incident_id` and a unique `trigger_event_id`. During the active 60-minute
window, a third recorded attempt followed by that event turns
`input_boolean.mitigation_enabled` off and emits
`homeops.mitigation.rollback.v1`. The observer and consumer preserve the
rollback in the append-only derived event log; the consumer sends the urgent
Telegram alert using its existing Telegram configuration.

The short-cycle event is an explicit test/adapter contract; the current
consumer remains read-only with respect to Home Assistant. This overlay is
still staged and is not deployed by the normal application release.
