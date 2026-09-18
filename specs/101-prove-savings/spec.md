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
6. Frontier-direct pin is a current identity (`cx/gpt-5.6-sol`), not a 2024
   Opus snapshot. Each arm stamps `completed_with` — direct is the pin, Verdict
   is the chooser pick (`decision.model`). The report prints both.

## Proof

- Hydrated Verdict arm with cheaper measured cost and passing AC → savings claimed.
- Quality miss (even if cheaper) → `savings_claimed=false`, reason `quality_miss`.
- Cache-hit cost → withheld as `cache_hit_is_not_model_savings`.

## Non-goals

- Live provider invoices
- Invented % talk track
- Selling empty/partial packs as hydrated
