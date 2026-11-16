# Shared selection contract

## Why

ETHOS.md "In force" is "Cheapest qualified tier first, then strongest." BOD-342 D6 and D8 say the same
thing: use the most capable cheapest option first, preserve paid/subscription capacity for the work that
needs it, assume free capacity unless specific paid or subscription evidence exists, and treat capable
metered routes as a last resort. The selection story exists to make free-first selection succeed more
often, so a cheaper route that clears the task's floor is chosen before a subscription route that also
clears it.

Four earlier changes (`cheapest-proven-selection-s1`, `cheapest-proven-selection-s2`,
`subscription-fallback-s3`, `bod417-omniroute-advisory-rankings`) each prove a piece of that. None states
the contract the pieces now have to meet together: one selector across the surfaces, one tier order, no
exception for the critical path, and receipt wording that says what happened to free supply. Without that
written contract, the surfaces drift apart and a receipt cannot be read as evidence that free capacity was
tried first.

## What changes

- **One selector.** `verdict choose`, `verdict route`, the serve candidate order, and
  `verdict orchestrate` name the same first route for the same task and the same evidence.
  `--allow-legacy-selector` is the only escape from that rule and it is explicit, never a silent default.
- **Tier order.** Capable free routes are exhausted before subscription. Subscription routes order by
  observed token consumption for the task family — the median of at least three accepted attempts, with
  unknown usage after measured usage — never by remaining quota. Permitted metered routes are last. An
  explicit free-only caller stays free-only; a caller-owned capacity narrow is never widened by selection.
- **No critical-path exception.** A frontier-worthy task raises the capability floor. It does not skip a
  free route that clears that floor.
- **Receipts name supply.** A selection receipt names eligible free routes attempted, known-unavailable
  routes, unqualified supply, and eligible routes left unattempted because caller authority expired.
  `budget_exhausted` and `free_supply_unknown` keep their current names.

## Capabilities

- **New Capabilities**
  - `shared-selection`: the cross-entrypoint selection contract — one selector, tier order, capability
    floor without a critical-path exception, and the receipt supply classes.

- **Modified Capabilities**

  (none: this change states the contract for behaviour the four cited changes already own; it does not
  restate or rewrite their requirements.)

## Scope

No product code, tests or docs outside this change. No caps, TTL constants, permit ages, timers, services
or endpoints change. No paid authority is granted. Existing admission, proof, confirmation, attempt and
retry-safety gates stay binding: the selector chooses among routes that are already admitted, and this
change only fixes the order and the accounting of that choice.

## Impact

- **Surfaces:** `verdict choose`, `verdict route`, `POST /v1/route` and the serve candidate order, and
  `verdict orchestrate`.
- **Contract:** new capability `shared-selection`; a selection receipt gains the supply classes named
  above. Existing receipt keys `budget_exhausted` and `free_supply_unknown` are unchanged, so existing
  readers keep working.
- **Other slices own the code.** The verification tasks below name the slices that implement each part:
  the ladder/entrypoint alignment, the free-first and subscription-token ordering with rescreen hints,
  and the receipt supply classes. This change is the contract they are verified against.

## Rollout / verification after merge

Verification happens in the slices, not here: aligned first-route agreement across the four surfaces,
free-before-subscription ordering under the proposed three-sample median, and a receipt that names the
four supply classes. On the operator host, acceptance additionally shows a real run whose receipt can be
read as free-first evidence. Tests and this document are not Done; the parent records host acceptance.
