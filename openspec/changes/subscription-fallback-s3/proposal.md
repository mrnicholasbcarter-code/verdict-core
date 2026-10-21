# Subscription fallback inside the request

## Why
BOD-342 D8 requires capable free routes first, then capable subscription routes with the lowest expected token consumption per task. Capable metered routes are last resort; active payment-required negatives remain excluded. The previous fixed shortlist could contain only free routes and fail without reaching a subscription.

## What changes
- Keep task capability, context, proof, confirmation, admission and transition gates. Preserve WORTHY handling.
- Rank qualified subscriptions by median observed input+output tokens from validated accepted attempts in the same task family, then rating. The proposed minimum sample is three unique accepted checked attempts per model/family; below that usage is unknown. Unknown usage follows measured usage. Reuse the bounded verified outcome loader and `load_checked_attempts`, not quota or route-name guesses. Existing quota-low filters are unchanged.
- Reserve one qualified subscription slot inside Top-k when Top-k permits it. Relay sends free attempts first, then subscription, then capable metered last inside the existing three-attempt policy. Failed tool capability can step up only to another route; ordinary invalid requests do not become retries. Idempotency and unsafe tool retry fences remain binding.
- Keep capable metered routes last, in existing rating order, after free and subscription. Existing payment-required negatives still block routes without funds. Preserve the original strategy-authority failure when no capable route remains.
- Persist exact free-route rescreen hints in the existing health cache when subscription fallback occurs. The installed consented daily screen prioritizes them inside its existing total/provider/wall caps, negative/cooldown checks and alias dedupe. No request-time probe, new timer or new cap. A hint grants no health/proof/dispatch permission and is consumed only when the existing screen sends a turn. Read-only choose does not write hints.

## Scope and constants
No guard, policy budget, permit age, TTL, timer, service or endpoint changes. The three-sample threshold is new and explicitly proposed. The relay retains its existing three-attempt budget. Top-k=1 cannot reserve fallback without expanding its caller-owned bound; it keeps the primary. The existing screen defaults and installed caps are unchanged.

## Host verification after merge
Parent must install merged main and exercise choose, orchestrate and serve with real current proof/usage. Verify raw fallback route and receipt reason, rescreen hints and the installed daily screen. Setup/doctor and acceptance remain required. Tests and this PR are not Done.
