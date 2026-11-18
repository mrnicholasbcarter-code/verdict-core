# choose-no-confirm-valid-proof: Read-only choose selects routes with valid proof older than 10 minutes

## Why

The operator ruling (2026-11-18) diagnosed that `verdict choose --no-confirm` was selecting zero routes even when 41 task-eligible routes existed. Root cause: the eligibility ladder inferred read-only mode from `max_probes_per_select=0` but still enforced the 10-minute `PERMIT_PROOF_MAX_VALIDITY_SECONDS` freshness window. Routes whose passport `qualified_at` exceeded 600 seconds were marked `proof_stale` and required a probe; with a zero probe budget, every such route became `probe_budget_exhausted`. Meanwhile, `passport_confirmation` (in `verdict/admit_prove_confirm.py`) already admits a route only while its passport has not expired — the 10-minute window is a dispatch-time concern, not a validity concern.

## What Changes

- Add explicit `read_only` flag to `EligibilityLadder.__init__` (passed as `read_only=no_confirm` from `build_selector`). This separates "no probes allowed" from "ignore 10-minute freshness window".
- In read-only mode, the ladder does its own passport validity check: a route is selectable iff its passport exists, is authorized, eligible, not expired, and has no newer negative health evidence. The 10-minute `qualified_at` window is ignored.
- Expired/invalid passports get named skip reasons (`passport_missing`, `passport_unauthorized`, `passport_ineligible`, `passport_expired`, `passport_superseded`) instead of `probe_budget_exhausted`.
- Normal mode (confirm allowed) behavior unchanged: proof is stale if not admitted OR `qualified_at` > 600s.
- Lease-held path unchanged: `lease_held` reason still wins when the verified-refresh lease is held.

## Safety

- Read-only mode never probes and never writes selection evidence (unchanged from `--no-confirm` contract).
- The validity check uses `passport_confirmation`'s health-cache-backed negative evidence, so a route with newer negative evidence is still excluded.
- Normal mode behavior is unchanged; existing tests pass.
- Tests use injected transports and temporary state; no live calls.

## Capabilities

### Modified Capabilities
- `verdict choose --no-confirm` now selects routes with valid passports whose `qualified_at` exceeds 600 seconds (previously returned no route).
- Skip reasons for invalid passports are now specific (e.g., `passport_expired`) instead of generic `probe_budget_exhausted`.

## Impact

- Operators can use `--no-confirm` to see which routes have valid passports even when proof is older than 10 minutes.
- No change to dispatch-time proof freshness; `--no-confirm` remains read-only.

## Acceptance Criteria

- [x] Read-only mode selects a route with a valid passport whose `qualified_at` is 30 minutes old (zero probes).
- [x] Read-only mode skips an expired passport with reason `passport_expired`.
- [x] Normal mode unchanged: a 30-minute-old passport triggers a probe.
- [x] Read-only mode skips a passport superseded by newer negative health evidence with reason `passport_superseded`.
- [x] Read-only mode with lease held returns `lease_held` reason.
- [x] All existing tests pass (test_orch_eligibility.py, test_acceptance.py, test_admit_prove_confirm.py, test_route_ladder_parity.py, test_admission.py, test_eligibility_event_post_probe.py).

## Evidence Boundary

- **KNOWN**: Root cause diagnosed from live logs (41 task-eligible, 0 selected); `passport_confirmation` admits only unexpired passports; `PERMIT_PROOF_MAX_VALIDITY_SECONDS = 600` in `health_cache.py`.
- **INFERRED**: The 10-minute window was incorrectly applied in read-only mode because `max_probes=0` was used as a proxy for read-only intent.
- **NOT AVAILABLE**: Live dispatch data for routes with 30-minute-old passports (no such routes existed at time of diagnosis).