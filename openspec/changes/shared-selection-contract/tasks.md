# Tasks

Verification for this contract lives in the slices that implement it. Every task below is open: this
change states the contract, it does not implement or run the verification. Tick a task only in the slice
that actually ran it, with its command and output.

## 1. One selector across entrypoints

- [ ] 1.1 `verdict route` selects through the same admission ladder and selection function as
  `verdict choose`, so the catalog-only fresh-passport intersection no longer decides the route; verify
  with the route/choose agreement test and `verdict route --json` and `verdict choose --json` naming the
  same first route for one task and one evidence set
- [ ] 1.2 The serve candidate order reports the ordering it was given by the shared selector instead of an
  independent one; verify with the serve-path test that a serve decision's first route equals the shared
  selector's first route for the same evidence
- [ ] 1.3 `verdict orchestrate` consumes the same ordering per node; verify with the orchestration test
  that a node's first attempt matches the shared selector's first route under the same evidence
- [ ] 1.4 `--allow-legacy-selector` remains the only escape, is explicit per request, and is recorded when
  used; verify with the test that no surface falls back to a legacy selector without the flag, and that a
  flagged run records its use

## 2. Tier order

- [ ] 2.1 Capable free routes are exhausted before any subscription route on every surface; verify with
  the free-before-subscription ordering test plus a three-free-route attempt budget test showing the
  subscription route is not displacing free capacity
- [ ] 2.2 Subscription order is observed token consumption of the task family (median of at least three
  accepted attempts, unknown after measured) and is unaffected by remaining quota; verify with the
  ordering test that flips each subscription route's quota headroom and leaves the order unchanged
- [ ] 2.3 Permitted metered routes sort last, after every capable free and subscription route, with
  existing payment-required negatives still excluding a route; verify with the metered-last ordering test
- [ ] 2.4 An explicit free-only caller stays free-only; verify with the test that a free-only admission
  narrow fails closed instead of selecting a subscription route

## 3. Capability floor, no critical-path exception

- [ ] 3.1 A frontier-worthy task raises the capability floor and still selects a free route that clears
  it; verify with the frontier test asserting a free first route when both a free and a subscription
  route clear the raised floor
- [ ] 3.2 Free routes that do not clear the raised floor are recorded as failing the floor, not as
  skipped; verify with the receipt/diagnostic test for that case

## 4. Receipt supply classes

- [ ] 4.1 A selection receipt names eligible free routes attempted, known-unavailable routes, unqualified
  supply, and eligible routes unattempted because caller authority expired, with each route in exactly
  one class; verify with the receipt-class test over a mixed evidence set
- [ ] 4.2 `budget_exhausted` and `free_supply_unknown` keep their current names and are not replaced or
  merged by the new classes; verify with the regression test asserting both existing reasons still appear
  under their current names

## 5. Integration

- [ ] 5.1 The four surfaces agree on the first route in one test over a single evidence set, and the
  strict OpenSpec validation plus `scripts/check_openspec_tasks.py --base origin/main` pass on the merged
  head; record the command output with the evidence for this story
