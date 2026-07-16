# Contract migration (v1)

`llm-gate` now emits versioned shared contracts in `schemas/contracts.v1.json`.
The JSON is intentionally usable by Python and `llm-gate-node`; unknown fields are
rejected at contract boundaries, while arbitrary forward-compatible data is kept
under `metadata` only. Never put credentials or bearer tokens in metadata.

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

Streaming, tools, response formats, aborts, retries, and fallbacks are execution
metadata and belong in task/tool requirements, route explanations, and outcome
events; they must not be silently dropped during proxy forwarding.
