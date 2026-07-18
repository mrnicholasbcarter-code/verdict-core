# Contract migration (v1)

`llm-gate` now emits versioned shared contracts in `schemas/contracts.v1.json`.
The JSON is intentionally usable by Python and `llm-gate-node`; unknown fields are
rejected at contract boundaries, while arbitrary forward-compatible data is kept
under `metadata` only. Never put credentials or bearer tokens in metadata.

Each v1 contract freezes `schema_version` to the literal string `"1"`. Producers
may omit the field and rely on the v1 default, but any explicit non-`"1"` value
must be rejected by both JSON Schema validation and the Python contract loader.

## Python helpers

The Python contract module now exposes three migration-safe entry points:

- `contract_from_dict(name, payload)` for strict v1 payloads.
- `contract_from_legacy_dict(name, payload)` for pre-v1 shapes that still use
  fields like `task`, integer `tier`, or `model`/`provider` pairs.
- `redact_contract_secrets(value)` for diagnostics and learning events that must
  preserve structure while scrubbing credentials from nested text and secret-like
  keys.

Legacy fields that do not have a first-class v1 location are preserved under a
nested `legacy` object inside `metadata`, `signals`, `adaptive_influence`, or
`details`, depending on the contract.

## From criticality/tier

The legacy `criticality` and integer `tier` values remain accepted by the
existing `RoutingDecision` API for compatibility. New integrations should put
`criticality` in `TaskSpec` as one safety signal, and use `policy_floor` plus
`selected_route` in `RoutingDecisionContract`. A tier is not a routing policy:
capabilities, privacy, production impact, availability, budget, and latency must
also be evaluated. This contract does not implement criticality-first routing.

Legacy mapping:

| Legacy | v1 |
|---|---|
| `task` | `TaskSpec.objective` |
| `criticality` | `TaskSpec.criticality` |
| `tier` | `RoutingDecisionContract.policy_floor` (named, not numeric) |
| `model` / `provider` | `selected_route.runtime_id` and route metadata |
| `alternatives` | `exclusions` and candidate snapshot |

Tier-to-floor migration in the Python helper follows this conservative mapping:
`0 → isolated`, `1 → protected`, `2 → standard`, `3 → best_effort`, and unknown
values fall back to `none`.

Streaming, tools, response formats, aborts, retries, and fallbacks are execution
metadata and belong in task/tool requirements, route explanations, and outcome
events; they must not be silently dropped during proxy forwarding.
