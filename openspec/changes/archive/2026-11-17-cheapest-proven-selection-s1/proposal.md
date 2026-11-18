# Cheapest qualified selection and current dispatch proof

## Why
BOD-342 D6 (operator 2026-10-21): "use the most capable cheapest option first ... preserve paid/subscription for the key tasks ... when we need and can satisfy the use of it, or when its required or valid". D7 assumes free capacity unless specific paid/subscription evidence exists. The earlier price-free order spent subscription even when a free route qualified. The retired prover also left dispatch passport proof older than the unchanged 600-second permit window.

## What changes
- Rank and actually select qualified routes by free, subscription, metered, unknown capacity, then existing rating and tie-breaks.
- Screen free before subscription with existing consent and caps.
- Fresh agentic-screen-qualified low-risk free workers are labelled probation in selection, receipts, and existing attempt/checker records. No medium/high/review gate changes.
- Real bounded confirms, screens and dispatch successes produce current exact-route passport proof. No new timer or ledger. Scope, epoch, consumed-attempt and MAYBE_SENT gates remain.
- Missing-price routes are assumed free, not capable. Anthropic API-key and OpenRouter routes outside the public free set are metered. Missing connection stays UNKNOWN and fails entitlement.

## Scope
Serve/request shortlist tier ordering is S2, not implemented here. Billing/402 promotion of assumed-free to paid is a follow-up. No caps, TTL constants, reserve policy, paid fallback policy or installed units change.

## Rollout / verification after merge
From merged main on the operator host, install through normal setup, run doctor, consent to one bounded free tool screen, then run a low-risk orchestrate slice with free,subscription capacity. Show selected free route, minted permit, independent verification and checked outcome row. With no qualified free route, show subscription fallback. Repeat after ten minutes without a background prover. Parent must rerun raw proof. This PR alone does not claim real-host acceptance or Done.

## Behaviour changes (D7 confirmed)
Connected no-price routes moved from UNKNOWN to FREE. Prepaid-only auto refresh now sends chat+tool calls to them without the unknown-cost prompt. The capacity evidence rule is recorded on each refreshed/screened route. Existing refresh bounds are unchanged: interactive default 20 routes / 40 calls, hard maximum 50 routes / 100 calls; maintenance default maximum 4 routes / 16 calls (verdict/orchestration/verified_refresh.py). Specifically paid routes still require existing consent. Positive pricing is specific paid evidence even with a nonstandard authentication label.
