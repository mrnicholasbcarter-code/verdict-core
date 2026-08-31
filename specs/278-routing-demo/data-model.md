# Data Model: Routing Demo Cost vs Quality

## DemoRequest

| Field | Rule |
|-------|------|
| `request_id` | Stable id `r001`…`r100` |
| `class` | `simple` or `complex` |
| `est_input_tokens` | Positive int used for pricing |
| `est_output_tokens` | Positive int used for pricing |
| `requires_tools` | bool; complex may require tools-capable kept identities |

## PriceQuote

| Field | Rule |
|-------|------|
| `identity_id` | Concrete identity id from live/recorded catalog |
| `input_per_mtok` | float from pricing capture |
| `output_per_mtok` | float from pricing capture |
| `cost_class` | `local` \| `free` \| `cheaper` \| `paid` |

## RoutingDecisionRecord

| Field | Rule |
|-------|------|
| `request_id` | FK DemoRequest |
| `chosen_id` | cheaper-first selection among qualified kept |
| `baseline_id` | costliest qualified kept for same request |
| `rationale` | Short human-readable reason (cheaper-first / class filters) |
| `routed_cost_usd` | Estimator on chosen |
| `baseline_cost_usd` | Estimator on baseline |
| `latency_ms` | From bounded execute attempt or null if not attempted |
| `success` | bool from bounded execute attempt or null if not attempted |

## DemoSummary

| Field | Rule |
|-------|------|
| `mode` | `live` \| `recorded` \| `blocked` |
| `request_count` | Must be 100 when completed |
| `routed_cost_usd` | Sum of decision routed costs |
| `baseline_cost_usd` | Sum of decision baseline costs |
| `savings_usd` | `baseline - routed` |
| `savings_pct` | `savings_usd / baseline` when baseline > 0 |
| `success_rate` | successes / execute_attempts |
| `avg_latency_ms` | Mean over execute attempts with latency |
| `catalog_captured_at` | ISO time |
| `gateway_base_url` | Redacted of secrets; endpoint only |
| `wall_clock_ms` | End-to-end |

## EvidenceCapture (recorded)

| Field | Rule |
|-------|------|
| `schema_version` | `routing-demo/v1` |
| `catalog_rows` + `pricing_index` | Real prior fetch |
| `decisions` / `summary` | Optional replay material |
| Label | Outputs MUST say `mode=recorded` |

## Validation

- Completed non-blocked summary has exactly 100 decisions.
- No decision may choose paid while a cheaper kept qualified candidate existed for that request.
- `mode=live` forbidden if catalog fetch failed.
- Outputs must pass secret stripping.
