# Home Assistant mitigation overlay

This directory contains a reviewable, opt-in Home Assistant configuration
overlay for the first mitigation slice. It is not part of the normal CI
deployment: the active Pi configuration remains read-only until the later
mitigation tasks and the end-to-end HA test are complete.

## Contents

- `automations.yaml` — one `mode: single` automation with one trigger per
  heating-call sensor. It only considers the newly triggered zone as the
  secondary call, requires another active zone, requires the furnace to be
  within the configured warmup window, and requires
  `input_boolean.mitigation_enabled` to be on.
- `helpers.yaml` — the guard and numeric helper projections used by the
  automation. The timing values mirror `rules.mitigation` in
  `services/insights/rules.yaml`; the guard starts off.

The automation uses `climate.set_hvac_mode` rather than trying to write to the
read-only `binary_sensor.*_heating_call` entities. It captures the target
climate's mode and setpoint, turns the secondary climate off for the configured
delay, and resumes only when the guard, furnace state, target state, setpoint,
and current-temperature checks still pass. A failed or changed state is left
non-actuating.

## Test-only installation

In an isolated Home Assistant test instance, merge the mappings in
`helpers.yaml` into the corresponding `input_boolean:` and `input_number:`
sections of `configuration.yaml`, and copy/merge the list in
`automations.yaml` into the instance's `automations.yaml`. Run Home
Assistant's configuration check and keep `mitigation_enabled` off until the
test setup is ready. Do not copy this overlay into the live Pi as part of a
normal application deploy.

The next mitigation tasks add durable event logging and automatic rollback;
this slice intentionally does not claim those capabilities.
