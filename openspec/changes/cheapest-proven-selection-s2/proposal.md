# Cheapest qualified serve selection and read-only choose

## Why
BOD-342 D6 applies to every request: select the cheapest capacity tier with qualified routes, then the strongest route inside it. D7 treats connected routes as free without specific paid/subscription evidence. S1 applied this only to the orchestration ladder. Serve still cut Top-k by score alone.

## What changes
- Move the existing classifier and tier order into one shared capacity module; preserve compatibility aliases.
- Derive serve capacity from existing snapshot connection and pricing evidence, with cache-only public OpenRouter evidence. For ordinary tasks tier order applies before Top-k; frontier-worthy tasks retain their existing proof-bar ranking until outcome evidence exists (approved plan point 2). Admission gates, alias dedupe and within-tier provider diversity remain.
- A real upstream 402 writes a route-scoped payment negative and billing_402_metered override using the existing feedback writer and TTL. Both selectors read it.
- `choose --no-confirm` is read-only and uses current exact-route passport proof; it never constructs or calls model confirmation transport and fails closed without proof. Existing permit-age checks remain.
- Add explicit `choose --task` text (previously argparse abbreviated --task-class), and share task-family inference with serve so code-edit text uses aa_coding. Explicit task families still win; no capability is inferred from text.

## Scope and constants
No permit machinery, TTL, budget, outcome-demotion thresholds, recovery policy, timer or service changes. Tier values 0/1/2/3 and default probe count 8 are inherited unchanged. No live gateway calls performed during implementation.

## Rollout / verification after merge
Parent must run focused tests and the real installed choose/serve commands from merged main on the operator host, with existing real proof, show raw selected route and capacity receipt, show --no-confirm disclosure and unchanged passport timestamps, then setup/doctor and ongoing acceptance. This PR does not claim host acceptance or Done.

## Bounded recovery consequence
The relay builds a fixed attempt list from the selected decision and its admitted alternatives, then iterates that list with existing legal-transition checks. It does not re-open the catalog after shortlist exhaustion. If a tier-first shortlist contains only free routes and all fail, the request fails closed; it does not reach subscription outside that shortlist. No new fallback rule is added.
