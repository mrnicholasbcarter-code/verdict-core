# Tasks

Verification for this contract lives in the slices that implement it. The audit below checks only
requirements fully supported by executed tests against main-delivered product code; partial evidence
leaves the original task open. The focused audit commands ran against main `6ed4f21a34c63326f6cd046a263c983a50461d32`
(product and tests unchanged). Test evidence is fixture-based, not live-operation or story-completion proof.
In the checkbox evidence below, the named pytest node IDs passed with
`uv run python -m pytest -q -p no:cacheprovider` (146-test focused run), and the receipt and subscription
files also passed in reordered execution with `-p no:xdist` (124-test run).

## 1. One selector across entrypoints

- [x] 1.1 `verdict route` selects through the same admission ladder and selection function as
  `verdict choose`, so the catalog-only fresh-passport intersection no longer decides the route; verify
  with the route/choose agreement test and `verdict route --json` and `verdict choose --json` naming the
  same first route for one task and one evidence set — Evidence: `tests/test_route_ladder_parity.py::test_choose_and_route_json_parity` (PR #1016, merged as 70c0bae6; passed in the 70-test focused run on this head).
- [x] 1.2 The serve candidate order reports the ordering it was given by the shared selector instead of an
  independent one; verify with the serve-path test that a serve decision's first route equals the shared
  selector's first route for the same evidence — Evidence: `tests/test_route_ladder_parity.py::test_serve_final_decision_matches_shared_selector_first_route` (drives `_prepare_execution_path_request` plus the real ADR-035 optimizer; passed in the 70-test focused run).
- [x] 1.3 `verdict orchestrate` consumes the same ordering per node; verify with the orchestration test
  that a node's first attempt matches the shared selector's first route under the same evidence — Evidence: `tests/test_route_ladder_parity.py::test_runtime_first_attempt_matches_selector_first_route` (DagRuntime first dispatch with an injected `_FirstRouteSelector`; passed in the 70-test focused run).
- [x] 1.4 `--allow-legacy-selector` remains the only escape, is explicit per request, and is recorded when
  used; verify with the test that no surface falls back to a legacy selector without the flag, and that a
  flagged run records its use — Evidence: `tests/test_route_ladder_parity.py::test_legacy_contract_no_silent_fallback_on_missing_supply` (route/run/choose/orchestrate) and `::test_legacy_contract_escape_is_request_local_and_durably_logged` (route/run/serve; durable `decisions.jsonl` record with `LEGACY_NON_AUTHORITY_FLAG`); both passed in the 70-test focused run.

## 2. Tier order

- [x] 2.1 Capable free routes are exhausted before any subscription route on every surface; verify with
  the free-before-subscription ordering test plus a three-free-route attempt budget test showing the
  subscription route is not displacing free capacity — Evidence: `tests/test_tier_order_cross_surface.py::test_capable_free_route_wins_choose_route_serve_and_dag_first_attempt` (choose/route/serve/DAG first attempt all name the free route) and `::test_three_free_routes_consume_dag_attempt_budget_before_subscription` (three free attempts consume the budget; subscription never attempted); both passed in the 70-test focused run.
- [x] 2.2 Subscription order is observed token consumption of the task family (median of at least three
  accepted attempts, unknown after measured) and is unaffected by remaining quota; verify with the
  ordering test that flips each subscription route's quota headroom and leaves the order unchanged — Evidence: `tests/test_subscription_fallback.py::test_subscription_order_follows_token_median_not_quota_headroom` (PR #1018, merged as ab35eb3d; passed in the 70-test focused run).
- [x] 2.3 Permitted metered routes sort last, after every capable free and subscription route, with
  existing payment-required negatives still excluding a route; verify with the metered-last ordering test — Evidence: `tests/test_subscription_fallback.py::test_permitted_metered_sorts_after_capable_free_and_subscription` (PR #1021, merged as e851e45b; free→subscription→metered capacity_order 0/1/2 plus payment_required exclusion; passed in the 70-test focused run).
- [x] 2.4 An explicit free-only caller stays free-only; verify with the test that a free-only admission
  narrow fails closed instead of selecting a subscription route — Evidence: `tests/test_serve_capacity_order.py::test_free_only_admission_fails_closed_when_only_subscription_supply` (orchestration ladder returns no route and serve execution path raises `ExecutionPathError` with subscription routes hard-excluded; passed in the 70-test focused run).

## 3. Capability floor, no critical-path exception

- [x] 3.1 A frontier-worthy task raises the capability floor and still selects a free route that clears it; verify with the frontier test asserting a free first route when both a free and a subscription route clear the raised floor — Evidence: `tests/test_orch_eligibility.py::test_planner_respects_capability_floor_before_cheapest_tier` (both parameters passed in the 146-test focused pytest run).
- [x] 3.2 Free routes that do not clear the raised floor are recorded as failing the floor, not as skipped; verify with the receipt/diagnostic test for that case — Evidence: `tests/test_selection_accounting.py::test_selector_emits_accounting_without_claiming_a_dispatch` and `::test_receipt_uses_real_dispatches_and_preserves_selector_rejections` (passed in both pytest runs).

## 4. Receipt supply classes

- [x] 4.1 A selection receipt names eligible free routes attempted, known-unavailable routes, unqualified supply, and eligible routes unattempted because caller authority expired, with each route in exactly one class; verify with the receipt-class test over a mixed evidence set — Evidence: `tests/test_selection_accounting.py::test_classes_are_disjoint_sorted_and_authority_requires_evidence` (both parameters), `::test_buffered_relay_reports_actual_free_sends`, and `::test_receipt_uses_real_dispatches_and_preserves_selector_rejections` (passed in both pytest runs).
- [x] 4.2 `budget_exhausted` and `free_supply_unknown` keep their current names and are not replaced or
  merged by the new classes; verify with the regression test asserting both existing reasons still appear
  under their current names — Evidence: `tests/test_subscription_fallback.py::test_terminal_reasons_free_supply_unknown_and_budget_exhausted_preserve_names_and_distinction` (asserts `termination_reason == "free_supply_unknown"` under the free-supply-unknown safety flag and `"budget_exhausted"` after three free attempts, and that neither collides with selection_accounting class keys; passed in the 70-test focused run).

## 5. Integration

- [x] 5.1 The four surfaces agree on the first route in one test over a single evidence set, and the
  strict OpenSpec validation plus `scripts/check_openspec_tasks.py --base origin/main` pass on the merged
  head; record the command output with the evidence for this story — Evidence: `tests/test_route_ladder_parity.py::test_four_surfaces_agree_on_first_route` (one `live_pool` evidence set drives `choose --json`, `route --json`, `IntelligenceService` serve, and `DagRuntime` first attempt; all name `free/weak`; passed in the 70-test focused run). Strict validation and the task check run on the merged head; outputs recorded in the BOD-417 DoD evidence file.
