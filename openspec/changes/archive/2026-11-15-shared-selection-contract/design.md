# Design — shared selection contract

## Context

See `proposal.md` — Why. The current state on the base SHA:

- One live selection function already exists (`select_route_for_task`), and `choose` without an explicit
  candidate list already calls it. `orchestrate` builds the same admission ladder. Admission order is free,
  subscription, metered, unknown.
- Two surfaces do not yet meet the contract. `verdict route` on the catalog path fail-closes on an empty
  fresh-passport intersection instead of running the ladder, and the serve path's execution-path optimizer
  is a third selector that reports its own ordering.
- `allow_legacy_selector` already exists as an explicit context flag on the serve path and the route
  action; it is an escape hatch for offline and unit paths, and it is not a silent default.
- Free-first tier order and subscription token ordering (median of at least three accepted same-family
  attempts, unknown after measured) are already implemented by the earlier changes cited in the proposal.
- Receipts carry two supply-related termination reasons and per-attempt capacity class, but not the four
  supply classes this contract names.

## Goals / Non-Goals

- **Goals:** one ordering authority named by every entrypoint for the same task and evidence; an ordering
  rule that reads the same wherever it is reported; a written rule that the capability floor, not the
  critical path, decides when a subscription route may be chosen; receipts that make "free supply was
  tried" auditable.
- **Non-Goals (design-level):** no second router, no second admission authority, no opaque `auto/*` or
  `combo/*` candidate (existing admission invariants keep them out); no new caps, TTLs, permit ages,
  timers, services or endpoints; no paid authority; no change to the attempt budget, idempotency fences or
  MAYBE_SENT rules.

## Decisions

### D1. One ordering authority owns the first route

The first route returned by the live selection function is the contract's answer. The other surfaces
consume that ordering rather than deriving their own: `verdict route` runs the same ladder instead of a
catalog-only intersection, and the serve path's optimizer consumes the same admitted ordering and reports
the selected candidate it was given. Each surface removes its own tie-break so that a disagreement can
only be evidence, not a second rule.

*Alternatives considered:* (a) keep per-surface ordering and reconcile in a test — rejected, because the
gaps appear only when evidence differs per surface and a fixture-level test cannot catch drift; (b) build
a new cross-surface selector service — rejected, it would be a second selector and an admission authority
the invariants forbid.

### D2. Legacy selector stays an explicit, recorded escape

`--allow-legacy-selector` and its request-context equivalent remain the only escape. It is per request, it
is not inferred from a profile, and its use is recorded in the result so an operator can tell a legacy
answer from a shared-selector answer. There is no automatic retirement schedule in this contract; the
escape stays until the legacy paths have no callers.

*Alternatives considered:* removing the flag — rejected because migration callers would then fail without
a documented path; making it implicit when evidence is missing — rejected, that is the silent fallback the
contract forbids.

### D3. Subscription ordering is token consumption of the task family, never quota

Ordering by remaining quota spends premium capacity to protect a number instead of the work. The
implemented rule (median total tokens of accepted checked attempts in the same family, minimum three
samples, unknown after measured, rating as the existing tie-break) is retained unchanged. This contract
only fixes it as a rule the surfaces must agree on; the sample minimum is already proposed by the earlier
change and is not re-proposed here.

### D4. Frontier-worthy raises the floor; there is no critical-path exception

A frontier-worthy task sets a higher capability floor on the node. A skip rule would let a
frontier-worthy task spend subscription quota on work a capable free route can already do, which
contradicts ETHOS.md "Cheapest qualified tier first, then strongest" and the operator rule that preserves
subscription for work that needs it. The floor is therefore the only mechanism: a free route that clears
the raised floor is still the answer, and free routes that do not clear it are recorded as failing the
floor, not as skipped for urgency. Capability assignment stays `free,subscription`; the floor never grants
paid authority.

*Alternatives considered:* a critical-path fast path that prefers a subscription route when the node is on
the critical path — rejected by the same reasoning; a capability-tier bump that excludes free routes
entirely for frontier tasks — rejected, that is the skip rule under another name.

### D5. Receipt supply classes are additive

The four classes (eligible free routes attempted, known-unavailable routes, unqualified supply, eligible
routes unattempted because caller authority expired) are recorded alongside the existing fields. The
existing `budget_exhausted` and `free_supply_unknown` reasons keep their names because readers and the
acceptance checks already key on them; the new classes answer "what happened to free supply" without
overloading those two terms. A route may not appear in two classes for the same decision: attempted,
known-unavailable, unqualified and authority-expired are mutually exclusive per route.

*Alternatives considered:* replacing `free_supply_unknown` with a richer enum — rejected, it breaks
existing readers for no gain; deriving the classes in the reviewer instead of the receipt — rejected,
a receipt must be self-contained evidence.

## Risks / Trade-offs

- **Route and serve disagree through the execution-path optimizer** → the optimizer consumes the shared
  ordering and reports the candidate it was given; an agreement test covers all four surfaces on one
  evidence set.
- **Receipt grew wider than readers expect** → classes are additive and mutually exclusive per route;
  existing keys keep their names, so no reader changes are required.
- **A free route clears the frontier floor but is slow** → latency remains an advisory ranking signal
  inside the admitted set; it may reorder, never change membership or authorize a route.
- **The legacy escape becomes permanent** → recorded on every use, so usage is visible in receipts and can
  be retired once callers are gone.

## Migration Plan

No migration. The contract is stated now; the slices implement and verify it, and the change is archived
with the evidence after the parent confirms host acceptance. Surfaces that already comply need no change;
the two that do not yet comply are the ladder alignment for `verdict route` and the serve ordering report.

## Open Questions

None.
