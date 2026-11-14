# Tasks

Verification for this contract lives in the slices that implement it. The audit below checks only
requirements fully supported by executed tests against main-delivered product code; partial evidence
leaves the original task open. The focused audit commands ran against main `6ed4f21a34c63326f6cd046a263c983a50461d32`
(product and tests unchanged). Test evidence is fixture-based, not live-operation or story-completion proof.
In the checkbox evidence below, the named pytest node IDs passed with
`uv run python -m pytest -q -p no:cacheprovider` (146-test focused run), and the receipt and subscription
files also passed in reordered execution with `-p no:xdist` (124-test run).

## 1. One selector across entrypoints

- [ ] 1.1 `verdict route` selects through the same admission ladder and selection function as
  `verdict choose`, so the catalog-only fresh-passport intersection no longer decides the route; verify
  with the route/choose agreement test and `verdict route --json` and `verdict choose --json` naming the
  same first route for one task and one evidence set
  - Partial: `tests/test_route_ladder_parity.py::test_choose_and_route_terse_parity` passed, but compares route terse output to choose JSON, not both requested JSON commands. Remaining: JSON-command agreement proof (entrypoint-parity slice).
- [ ] 1.2 The serve candidate order reports the ordering it was given by the shared selector instead of an
  independent one; verify with the serve-path test that a serve decision's first route equals the shared
  selector's first route for the same evidence
  - Partial: `tests/test_route_ladder_parity.py::test_serve_uses_only_ladder_candidates_in_ladder_order` passed; it injects ladder assessments and checks prepared offers/shortlist, not the final serve decision against the shared selector. Remaining: final-decision parity proof preserving ADR-035 optimizer authority (serve-parity slice).
- [ ] 1.3 `verdict orchestrate` consumes the same ordering per node; verify with the orchestration test
  that a node's first attempt matches the shared selector's first route under the same evidence
  - Partial: `tests/test_route_ladder_parity.py::test_orchestrate_selector_and_choose_share_first_route` passed but calls the selector directly, not a DAG node's first dispatch. Remaining: runtime first-attempt agreement (runtime-parity slice).
- [ ] 1.4 `--allow-legacy-selector` remains the only escape, is explicit per request, and is recorded when
  used; verify with the test that no surface falls back to a legacy selector without the flag, and that a
  flagged run records its use
  - Partial: default-route and flagged-route tests in `tests/test_route_ladder_parity.py` passed; the latter asserts request-context recording only. `_action_route` also accepts an injected gate or explicit offline mode. Remaining: distinguish the existing offline contract and prove no silent fallback plus durable legacy-use recording on every surface (legacy-contract slice).

## 2. Tier order

- [ ] 2.1 Capable free routes are exhausted before any subscription route on every surface; verify with
  the free-before-subscription ordering test plus a three-free-route attempt budget test showing the
  subscription route is not displacing free capacity
  - Partial: `tests/test_subscription_fallback.py::test_free_candidates_fill_shortlist_before_subscription` and `::test_relay_three_free_routes_consume_budget_before_higher_tiers` passed; actual three-send coverage also passed. Remaining: exhaustion proof across every entrypoint, including DAG attempts (cross-surface tier-order slice).
- [ ] 2.2 Subscription order is observed token consumption of the task family (median of at least three
  accepted attempts, unknown after measured) and is unaffected by remaining quota; verify with the
  ordering test that flips each subscription route's quota headroom and leaves the order unchanged
  - Partial: token-sample, measured-before-unknown, task-family and serving-order tests in `tests/test_subscription_fallback.py` passed. Remaining: explicit per-route quota-headroom flip invariance and nonconstant-sample median ordering proof (subscription-ranking slice).
- [ ] 2.3 Permitted metered routes sort last, after every capable free and subscription route, with
  existing payment-required negatives still excluding a route; verify with the metered-last ordering test
  - Partial: `tests/test_subscription_fallback.py::test_paid_is_last_resort_when_no_free_or_subscription_qualifies`, `::test_no_capable_subscription_uses_capable_paid_last_resort` and `::test_active_payment_negative_still_blocks_metered` passed. Existing mixed-tier relay tests consume the attempt budget before metered; they do not assert a shared-selector ordering containing all three capable tiers. Remaining: complete mixed-tier metered-last ordering proof (tier-order slice).
- [ ] 2.4 An explicit free-only caller stays free-only; verify with the test that a free-only admission
  narrow fails closed instead of selecting a subscription route
  - Partial: `tests/test_serve_capacity_order.py::test_explicit_free_only_excludes_paid_snapshot_routes`, `tests/test_authoritative_eligibility.py::test_p_fallback_expansion_cannot_violate_free_only`, and capacity-narrowing tests passed. The fail-closed fixture is metered-only; the canonical narrowing fixture permits free plus subscription. Remaining: explicit free-only admission with only subscription supply fails closed (admission-policy slice).

## 3. Capability floor, no critical-path exception

- [x] 3.1 A frontier-worthy task raises the capability floor and still selects a free route that clears it; verify with the frontier test asserting a free first route when both a free and a subscription route clear the raised floor — Evidence: `tests/test_orch_eligibility.py::test_planner_respects_capability_floor_before_cheapest_tier` (both parameters passed in the 146-test focused pytest run).
- [x] 3.2 Free routes that do not clear the raised floor are recorded as failing the floor, not as skipped; verify with the receipt/diagnostic test for that case — Evidence: `tests/test_selection_accounting.py::test_selector_emits_accounting_without_claiming_a_dispatch` and `::test_receipt_uses_real_dispatches_and_preserves_selector_rejections` (passed in both pytest runs).

## 4. Receipt supply classes

- [x] 4.1 A selection receipt names eligible free routes attempted, known-unavailable routes, unqualified supply, and eligible routes unattempted because caller authority expired, with each route in exactly one class; verify with the receipt-class test over a mixed evidence set — Evidence: `tests/test_selection_accounting.py::test_classes_are_disjoint_sorted_and_authority_requires_evidence` (both parameters), `::test_buffered_relay_reports_actual_free_sends`, and `::test_receipt_uses_real_dispatches_and_preserves_selector_rejections` (passed in both pytest runs).
- [ ] 4.2 `budget_exhausted` and `free_supply_unknown` keep their current names and are not replaced or
  merged by the new classes; verify with the regression test asserting both existing reasons still appear
  under their current names
  - Partial: `tests/test_subscription_fallback.py::test_relay_three_free_failures_report_budget_exhaustion` asserts the terminal reason; `::test_real_preparation_fences_outside_shortlist_free_proof` asserts the unchanged unknown-supply safety flag, and `::test_relay_does_not_escalate_when_free_supply_is_unknown` asserts its escalation fence (passed in both pytest runs). Remaining: assert the actual `free_supply_unknown` terminal reason, rather than only supplying or inspecting its safety flag (termination-regression slice).

## 5. Integration

- [ ] 5.1 The four surfaces agree on the first route in one test over a single evidence set, and the
  strict OpenSpec validation plus `scripts/check_openspec_tasks.py --base origin/main` pass on the merged
  head; record the command output with the evidence for this story
  - Partial: separate route/choose, orchestration-selector and serve-shortlist tests passed, using different evidence fixtures and no DAG dispatch. Remaining: one four-surface same-evidence test plus strict validation/task-check output on the merged head (integration and post-merge verification slices). This change remains active and incomplete.
