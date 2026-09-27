# Fleet Deploy Lab DeploymentSpec

PR 02 defines the single request contract shared by the FastAPI endpoint,
trusted GitHub Actions workflow, deployers, and `/deploy` preview.
The executable source of truth is the dependency-free
[`deploy_demo.deployment_spec`](../deploy_demo/deployment_spec.py) module.

## Canonical shape

An environment selector expands to the complete deterministic group:

```json
{"deployment_id":"demo-20260925-001","environment":"test","failure_mode":"abort","implementation":"python","profile":{"color":"blue","shape":"circle"},"schema_version":1,"strategy":"all_at_once"}
```

An explicit selection uses `target_ids` instead of `environment`:

```json
{"deployment_id":"demo-20260925-002","failure_mode":"abort","implementation":"ansible","profile":{"color":"green","shape":"hexagon"},"schema_version":1,"strategy":"all_at_once","target_ids":["test-vehicle-01","prod-vehicle-02"]}
```

For a bounded interview failure demonstration, the optional
`failure_target_id` selects one of the already selected logical vehicles:

```json
{"deployment_id":"demo-20260927-failure","environment":"test","failure_mode":"abort","failure_target_id":"test-vehicle-01","implementation":"python","profile":{"color":"orange","shape":"triangle"},"schema_version":1,"strategy":"canary"}
```

The validator requires exactly one of `environment` or `target_ids`. It rejects
unknown keys, missing fields, empty selections, duplicate IDs, unknown target
IDs, more than twelve targets, invalid enum values, unselected failure targets,
and invalid strategy/failure mode combinations. The profile has only finite
`color` and `shape` values.

The known targets are four logical vehicles in each environment:

| Environment | Target IDs |
| --- | --- |
| `test` | `test-vehicle-01` through `test-vehicle-04` |
| `stage` | `stage-vehicle-01` through `stage-vehicle-04` |
| `prod` | `prod-vehicle-01` through `prod-vehicle-04` |

The currently verified capability matrix is deliberately narrower than the
reserved enum vocabulary. Python has a verified stable-order canary path;
Ansible remains on its verified all-at-once path:

| Implementation | Strategy | Failure mode |
| --- | --- | --- |
| `python` | `all_at_once` | `abort` |
| `python` | `canary` | `abort` |
| `python` | `canary` | `rollback` |
| `ansible` | `all_at_once` | `abort` |

The backend publishes this matrix with the anonymous fleet snapshot, and the
frontend renders only those combinations. The shared validator rejects every
other combination before manifest admission. The reserved values remain in
the schema for later, separately verified rollout work.

The complete reserved enum vocabulary is:

| Field | Values |
| --- | --- |
| `profile.color` | `blue`, `green`, `orange`, `purple` |
| `profile.shape` | `circle`, `hexagon`, `square`, `triangle` |
| `implementation` | `python`, `ansible` |
| `strategy` | `all_at_once`, `rolling`, `canary` |
| `failure_mode` | `abort`, `rollback` |
| `failure_target_id` | optional known target selected by `environment` or `target_ids` |

`all_at_once` currently accepts only `abort`; Python `canary` accepts `abort`
and `rollback`, while rolling remains reserved. `failure_target_id` is
orthogonal to that capability matrix: when present, the simulator deliberately
leaves that finite target's observed profile divergent, so the deployer must
fail its ordinary fresh-readback verification. With `rollback`, the deployer
restores only targets that reached the new observed profile and verifies their
pre-deployment snapshots through a protected restore API. It cannot identify a
real host, carry a command, or reach Home Assistant or the normal HomeOps
release path.

## Deterministic serialization

`validate_deployment_spec()` returns an immutable `DeploymentSpec`. Its
`canonical_json()` method emits one compact JSON line with sorted object keys.
Explicit target lists are normalized into the stable `test`, `stage`, `prod`
target order. The CLI applies the same validator and serializer:

```bash
python -m deploy_demo < deployment-spec.json
```

The canonical manifest content and deployment provenance are separate concerns.

## Deterministic verification failure

The optional `failure_target_id` is a test switch, not a deployment command.
The queue boundary validates that it names one selected simulated vehicle. The
simulator preserves desired-versus-observed divergence, records a bounded
target-level error, and leaves later canary targets pending. Python and Ansible
then perform their normal fresh public readback and exit nonzero when the
requested observed state cannot be proven. The failed run remains linked to
its manifest and Actions URL for the `/deploy` UI to display.

## Partial rollout rollback

The Python canary `rollback` mode captures every selected target's observed
profile from the queue response before the first apply. If a later target
fails, the deployer stops the rollout and sends only the targets that reached
the new observed digest to the protected restore endpoint. A fresh anonymous
read must report `rollback_status: "succeeded"`, the exact restored target IDs,
their pre-deployment desired and observed profiles, `ready` status, and no
active deployment reservation. The deployment itself remains `failed`. If the
restore call or readback cannot be verified, the run stays failed and the
changed target state remains visible as partial drift.

The future workflow must retain the manifest event commit SHA independently
from the built artifact's content digest. One must never be substituted for
the other or accepted as proof of the other.

## Trust boundary

The browser submits only this closed data structure. It cannot provide a
command, executable code, arbitrary path, URL, shell argument, or deployer
instruction. The API and CI must call the same
`validate_deployment_spec()` implementation; later API/workflow PRs must not
recreate the schema in Pydantic, YAML, JavaScript, or shell.
