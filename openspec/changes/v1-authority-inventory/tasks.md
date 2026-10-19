# Tasks

## 1. Wave 2 — task authority and bounded inventory

- [x] 1.1 **S04 / BOD-345** (acceptance A01, A03, A05): bound gateway inventory discovery and keep missing telemetry unknown. Proof: `pytest -q tests/test_free_tier_admit.py tests/test_eligibility_gateway_contract.py tests/test_authoritative_eligibility.py tests/test_admit_snapshot_resilience.py` reported 106 passed at 03942a03 (owner report). Pre-merge verification only.
- [x] 1.2 **S02 / BOD-432** (acceptance A07, A08, A25): server-owned task authority and price-invariant selection with trusted shortlist receipts; ADR-035 amended. Proof: the 19-file focused run in the fix pass 2 and fix pass 5 reports (492 passed at e13f9f0d) plus `tests/test_role_assignment.py`, `tests/test_controller_launch.py`, `tests/test_execution_path.py` (120 passed at fa81f547). Pre-merge verification only.
- [x] 1.3 **BOD-442** test isolation: doctor tests no longer read the host systemd user manager. Proof: doctor, metadata and prove-at-rest test files, 215 passed with the host timer active (4127e18c).

## Scope and Delivery Notice

- **Active scope**: Wave 2 stories S02 and S04, pre-merge implementation and automated test verification only.
- **Not claimed**: live acceptance on the operator's gateway from merged main, three maintenance cycles, full DoD proof, Done status, or archive. These remain open and are not listed as tasks here so that ticked items stay true; the parent verifies them on merged main.
- **Known gap**: `tests/test_cli_inprocess.py` doctor tests depended on host gateway and systemd state before BOD-442; the autodev ladder rank still mentions price (A25 follow-up).
- **Umbrella plan**: `docs/contracts/v1-hardening-umbrella-plan.md` is unchanged. Wave 1 is in `openspec/changes/verdict-core-v1-hardening`.
