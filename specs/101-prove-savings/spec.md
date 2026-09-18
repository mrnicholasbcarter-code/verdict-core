# Feature Specification: Paired legit-task savings bench (BOD-101)

## Issue

Linear BOD-101. Parent BOD-97.

## Goal

Measure frontier-direct vs Verdict-path cost and quality on **real** tasks.
Talk track is **we measure**. Invented percentages and hello-world prompts
are out of scope. Savings stay blocked unless `pack_state=hydrated` with
real `included_sources`.

## Functional requirements

1. Paired arms: frontier-direct vs Verdict cheap path; same acceptance criteria.
2. Three legit fixtures: debug, refactor+tests, implement-from-AC.
3. Verdict arm runs the live control plane (worthiness, capability, free-first).
4. Cost is parsed only from `X-OmniRoute-Response-Cost` / Tokens-In/Out headers
   or the same fields on a receipt. Cache hit ≠ model savings.
5. A quality miss is reported and **not** sold as savings.
6. Each arm **must specify the model the measured cost belongs to**
   (`X-OmniRoute-Model` / `completed_with`). Identities are never inferred
   and never taken from a hardcoded Python pin. Verdict also stamps
   `routed=decision.model` from the live chooser. The report prints
   `direct_used`, `verdict_used`, and `routed`.
7. A cheaper-model cache-hit replay is a first-class fixture task, labeled
   `cache_hit`, and **not** sold as model savings.

## Proof

- Hydrated Verdict arm with cheaper measured cost and passing AC → savings claimed.
- Quality miss (even if cheaper) → `savings_claimed=false`, reason `quality_miss`.
- Cheaper-model cache-hit cost → withheld as `cache_hit_is_not_model_savings`.
- Missing used-model on an arm fails closed.

## Non-goals

- Live provider invoices
- Invented % talk track
- Selling empty/partial packs as hydrated
- Hardcoding a winner identity that scripts the chooser

## Execution binding (BOD-114)

The fixture-only run is a labeled **simulation** (`mode=simulation-not-executed`,
`claims_allowed=false`) and can never set `savings_claimed=true`. Claims require
`--live-paired` (or an `execute_arm` hook): both arms executed with execution
IDs, the same input hash, observed costs, provider-bound identities with an
explained attempt chain, output-evaluated quality, a hydrated pack, and no
cache hit. See `docs/benchmarks/paired-savings.md`.

