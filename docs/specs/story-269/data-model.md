# Story 269 data model

Story 269 reuses existing runtime contracts and adds two CLI evidence payloads.

## Offline detection report

| Field | Type | Meaning |
|---|---|---|
| `mode` | string | Always `offline` for `detect --offline` |
| `network_access` | boolean | Always `false` |
| `credentials_read` | boolean | Always `false` |
| `local_providers` | array | Empty because no ports or CLIs are probed |
| `cli_providers` | array | Empty because no executables are invoked |
| `centralized_routers` | array | Empty because routers are not contacted |
| `cloud_apis` | array | Empty because credential variables are not read |
| `custom_endpoints` | array | Empty because config endpoints are not contacted |

## Failover proof report

The CLI serializes selected fields from the existing `ReplayProof` object.

| Field | Type | Meaning |
|---|---|---|
| `session_id` | string | Persisted execution session replay key |
| `initial_model` | string | Model bound before the forced HTTP 429 |
| `replacement_model` | string | Eligible model selected after failure |
| `failure_status` | integer | Deterministic transient failure status (`429`) |
| `completed_steps` | array[string] | Steps committed exactly once |
| `event_sequence` | array[object] | Ordered, bounded execution/failover proof events |
| `replay_digest` | string | Deterministic digest of proof evidence |

## Journey command matrix

| Stage | Command | Success evidence |
|---|---|---|
| Install | `verdict --help` | exit 0 and all journey subcommands registered |
| Provider | `verdict detect --offline --json` | explicit offline/no-network report |
| Route | `verdict quickstart --non-interactive --dry-run --json` | selected fixture route plus exclusions |
| Mission | `verdict autodev-golden-path ... --json` | accepted three-stage local report |
| Failover | `verdict failover-proof ... --json` | forced 429, replacement, session id |
| Replay | `VERDICT_MEMORY_DB=... verdict replay ID --json` | persisted session state with completed steps |

All paths are local, bounded, and safe to discard. Live provider health, quota,
latency, output quality, and cost savings remain outside this data model.
