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

The validator requires exactly one of `environment` or `target_ids`. It rejects
unknown keys, missing fields, empty selections, duplicate IDs, unknown target
IDs, more than twelve targets, invalid enum values, and invalid strategy/failure
mode combinations. The profile has only finite `color` and `shape` values.

The known targets are four logical vehicles in each environment:

| Environment | Target IDs |
| --- | --- |
| `test` | `test-vehicle-01` through `test-vehicle-04` |
| `stage` | `stage-vehicle-01` through `stage-vehicle-04` |
| `prod` | `prod-vehicle-01` through `prod-vehicle-04` |

The currently verified capability matrix is deliberately narrower than the
reserved enum vocabulary. Both deployers use the simulator's atomic apply path;
they do not yet implement rolling, canary, or rollback semantics:

| Implementation | Strategy | Failure mode |
| --- | --- | --- |
| `python` | `all_at_once` | `abort` |
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

`all_at_once` currently accepts only `abort`; `rolling`, `canary`, and
`rollback` are not enabled by the current capability matrix. This explicit
fail-closed boundary prevents the UI or a bypassed API caller from claiming
behavior the selected implementation does not provide.

## Deterministic serialization

`validate_deployment_spec()` returns an immutable `DeploymentSpec`. Its
`canonical_json()` method emits one compact JSON line with sorted object keys.
Explicit target lists are normalized into the stable `test`, `stage`, `prod`
target order. The CLI applies the same validator and serializer:

```bash
python -m deploy_demo < deployment-spec.json
```

The canonical manifest content and deployment provenance are separate concerns.
The future workflow must retain the manifest event commit SHA independently
from the built artifact's content digest. One must never be substituted for
the other or accepted as proof of the other.

## Trust boundary

The browser submits only this closed data structure. It cannot provide a
command, executable code, arbitrary path, URL, shell argument, or deployer
instruction. The API and CI must call the same
`validate_deployment_spec()` implementation; later API/workflow PRs must not
recreate the schema in Pydantic, YAML, JavaScript, or shell.
