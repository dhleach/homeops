# Fleet Deploy Lab DeploymentSpec

PR 02 defines the single request contract shared by the future FastAPI
endpoint, trusted GitHub Actions workflow, deployers, and `/deploy` preview.
The executable source of truth is the dependency-free
[`deploy_demo.deployment_spec`](../deploy_demo/deployment_spec.py) module.

## Canonical shape

An environment selector expands to the complete deterministic group:

```json
{"deployment_id":"demo-20260925-001","environment":"test","failure_mode":"rollback","implementation":"python","profile":{"color":"blue","shape":"circle"},"schema_version":1,"strategy":"rolling"}
```

An explicit selection uses `target_ids` instead of `environment`:

```json
{"deployment_id":"demo-20260925-002","failure_mode":"abort","implementation":"python","profile":{"color":"green","shape":"hexagon"},"schema_version":1,"strategy":"canary","target_ids":["test-vehicle-01","prod-vehicle-02"]}
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

Supported values are:

| Field | Values |
| --- | --- |
| `profile.color` | `blue`, `green`, `orange`, `purple` |
| `profile.shape` | `circle`, `hexagon`, `square`, `triangle` |
| `implementation` | `python`, `ansible` |
| `strategy` | `all_at_once`, `rolling`, `canary` |
| `failure_mode` | `abort`, `rollback` |

`all_at_once` currently accepts only `abort`; `rolling` and `canary` accept
either failure mode. The failure mode is a reserved contract value in this PR;
the runtime behavior is implemented by later failure/rollback tasks.

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
