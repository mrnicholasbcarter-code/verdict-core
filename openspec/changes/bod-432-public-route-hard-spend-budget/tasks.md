# Tasks

## 1. Local implementation — wave 2: budget validation

- [x] 1.1 RED: add public HTTP tests in `tests/test_public_route_budget.py` covering:
  - Valid integer input (e.g., `5`), valid fractional input (e.g., `0.01`), omitted input.
  - 400 for: `null`, `true`/`false`, `"5"`, `[]`, `{}`, `0`, `-1`, `NaN`, `Infinity`, float overflow (`1e400`).
  - No probe or authority work on invalid input (assert early 400 before any admission call).
- [x] 1.2 GREEN: in `_public_execution_path_request` (verdict/api.py:1119), validate `hard_spend_budget` from `raw.get("hard_spend_budget")`:
  - Absent key → do not set (preserves `None`).
  - Explicit `null` or non-numeric → `ExecutionPathError("execution_path_request.hard_spend_budget must be a positive finite number")`.
  - `bool` check first (since `isinstance(True, int)`), then `int`/`float`, then `math.isfinite`, then `> 0`.
  - Thread validated value into `ExecutionPathRequest(hard_spend_budget=validated)` at line 1295.
- [x] 1.3 Run focused tests: `uv run python -m pytest -q tests/test_public_route_budget.py`.
- [x] 1.4 Run related suites: `tests/test_candidate_pool.py`, `tests/test_live_candidate_shortlist.py`, `tests/test_security.py`.

## 2. Local implementation — wave 2: routing trace plumbing

- [x] 2.1 RED: add public HTTP tests in `tests/test_public_route_routing_trace.py` covering:
  - `POST /v1/route?include_routing_trace=true` returns `routing_trace` with all four fields.
  - `POST /v1/route` (no query param) returns legacy response without `routing_trace`.
  - Trace present on no-selection responses (denied/blocked) when an authoritative decision was reached.
  - Trace fields match snapshots: admitted = receipt.admitted ∩ offered (or confirmed.admitted), shortlist = pool_receipt.shortlist route_ids, optimizer_decision = ExecutionPathDecision outcome + selected_candidate_id, rejected_offers = decision.rejected (candidate_id + reason).
- [x] 2.2 GREEN: in `verdict/models.py`, add optional internal field `routing_trace: dict | None = None` to `RoutingDecision` (not in `_safe_decision_dict` projection).
- [x] 2.3 GREEN: in `verdict/intelligence.py`, `_prepare_execution_path_request_at` (lines 1371-1489):
  - Snapshot `offered_admitted` (line 1372: admitted IDs narrowed to offered universe) or `confirmed.admitted` on live path (line 1437).
  - Snapshot ordered shortlist route_ids from `pool.shortlist` (line 1400).
  - After `optimize_execution_path` returns the `ExecutionPathDecision`, build the trace dict once:
    ```python
    trace = {
        "admitted_candidate_ids": list(snapshot_admitted),
        "shortlist_candidate_ids": [item.route_id for item in pool.shortlist],
        "optimizer_decision": {
            "outcome": "selected" if decision.selected_candidate_id else "denied",
            "selected_candidate_id": decision.selected_candidate_id,
        },
        "rejected_offers": [
            {"candidate_id": r.candidate_id, "code": r.reason} for r in decision.rejected
        ],
    }
    ```
  - Attach to the `RoutingDecision` returned by `intelligence.route()` (the caller wraps the decision).
- [x] 2.4 GREEN: in `verdict/api.py`, `route_task` (line 1399):
  - Add `include_routing_trace: bool = False` as a typed query parameter (FastAPI default).
  - After getting `decision` from `_route_with_intelligence`, if `include_routing_trace` and `decision.routing_trace`, include it in the response JSON.
- [x] 2.5 Run focused tests: `uv run python -m pytest -q tests/test_public_route_routing_trace.py`.
- [x] 2.6 Run related suites: `tests/test_candidate_pool.py`, `tests/test_live_candidate_shortlist.py`, `tests/test_security.py`.

## 3. Local implementation — wave 2: A25 invariance tests

- [x] 3.1 RED: add tests in `tests/test_public_route_a25_invariance.py` covering:
  - With matched authorization/proof and multiple eligible candidates, label-only and price-only mutations preserving budget eligibility keep: admitted membership, ordered shortlist, optimizer outcome/choice, public selection unchanged.
  - Assertions use structured fields (`routing_trace.admitted_candidate_ids`, `shortlist_candidate_ids`, `optimizer_decision`, `rejected_offers`), not reason prose.
- [x] 3.2 GREEN: ensure the implementation satisfies these invariants (no code change expected if trace plumbing is correct).
- [x] 3.3 Run focused tests.

## 4. Local implementation — wave 2: gates

- [x] 4.1 Run full test suite: `uv run python -m pytest -q`.
- [x] 4.2 Run linters: `uv run ruff check . && uv run ruff format --check .`.
- [x] 4.3 Run type checker: `uv run mypy verdict --strict`.
- [x] 4.4 Run doc links: `uv run python scripts/check_doc_links.py`.
- [x] 4.5 Run reachability: `uv run python scripts/check_reachability.py --ratchet-base origin/main`.
- [x] 4.6 Run OpenSpec validation: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/bod-432-public-route-hard-spend-budget` and `npx --yes @fission-ai/openspec@1.13.2 validate bod-432-public-route-hard-spend-budget --strict --no-interactive` and `npx --yes @fission-ai/openspec@1.13.2 validate --all --strict --no-interactive`.
- [x] 4.7 Run git diff check: `git diff --check`.

## 5. Independent review — wave 3

**Verification:** Independent code review by a different model family, selected per-review via `~/bin/pick-reviewer --author`. Reviewer verifies the implementation against the spec and tests.

- [ ] 5.1 Orchestrator selects reviewer with `~/bin/pick-reviewer --author` using served author model; capture observed distinct reviewer family and substantive review.
- [ ] 5.2 Reviewer verifies: input rejection, unchanged legacy response and routing, no re-admission or fallback, accurate stage provenance, no secret exposure.
- [ ] 5.3 Findings resolved and exact-head gates green before merge.

## 6. Post-merge live verification — wave 4

**Verification:** Live end-to-end proof on installed merged main with operator consent and independent parent confirmation. Raw HTTP evidence captured and validated via `scripts/check_dod.py`.

- [ ] 6.1 (post-merge) After exact-head CI and merge, verify installed SHA, `verdict doctor`, and service state; do not restart OmniRoute.
- [ ] 6.2 (post-merge) Repeat after required freshness interval and record scheduled coverage.
- [ ] 6.3 (post-merge) Prepare reviewed JSON files in INPUT from current live candidates, using held evidence only as a structural reference. Pin consent, fresh proof and eligible non-pay-per-token routes; stop rather than trigger any unauthorized or pay-per-token probe.
- [ ] 6.4 (post-merge) Run the live acceptance commands from the plan (see `docs/plan/20261118-route-hard-spend-budget/plan.yaml`).
- [ ] 6.5 (post-merge) Negative cases show the matching structured rejection and no selection of the rejected candidate. The same unpriced candidate in the no-budget control remains eligible and can be selected. Invalid budget is 400 with the field name; omitted budget without opt-in preserves the legacy response.
- [ ] 6.6 (post-merge) Parent independently reruns and records raw evidence.
- [ ] 6.7 (post-merge) Only after all AGENTS.md DoD parts pass, complete post-merge tasks, validate evidence with `scripts/check_dod.py`, archive the change and close BOD-432.