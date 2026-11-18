# Tasks

## 1. OpenSpec Change Artifacts

- [x] Create proposal.md with ## Why, ## What Changes, ## Safety, ## Capabilities, ## Impact, ## Acceptance Criteria, and Evidence Boundary sections
- [x] Create design.md with ## Context, ## Goals / Non-Goals, ## Decisions, ## Risks / Trade-offs, ## Security / trust implications, ## Routing / context / memory implications, ## Migration / rollback, ## ADR impact
- [x] Create spec delta in specs/route-selection/spec.md with MODIFIED 'Read-only choose' requirement and three scenarios
- [x] Run OpenSpec validator: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/choose-no-confirm-valid-proof`

## 2. Implementation

- [x] Add `read_only` flag to `EligibilityLadder.__init__`
- [x] In `select()`, re-evaluate HEALTHY-failed assessments in read-only mode using direct passport load (ignores 10-minute window)
- [x] Check passport validity: exists, authorized, eligible, not expired, no newer negative health evidence
- [x] Add valid routes to candidates; superseded routes get `passport_superseded` reason
- [x] In proof-stale branch, derive named skip reasons (`passport_missing`, `passport_unauthorized`, `passport_ineligible`, `passport_expired`, `passport_superseded`)
- [x] Pass `read_only=no_confirm` from `build_selector` in `eligibility_report.py`
- [x] Ensure lease_held still wins in read-only mode (checked before proof_stale)

## 3. Testing

- [x] Test: read-only mode selects route with valid passport whose `qualified_at` is 30 minutes old (zero probes)
- [x] Test: read-only mode skips expired passport with reason `passport_expired`
- [x] Test: normal mode unchanged — 30-minute-old passport triggers a probe
- [x] Test: read-only mode skips passport superseded by newer negative health evidence with reason `passport_superseded`
- [x] Test: read-only mode with lease held returns `lease_held` reason
- [x] All existing tests pass (test_orch_eligibility.py, test_acceptance.py, test_admit_prove_confirm.py, test_route_ladder_parity.py, test_admission.py, test_eligibility_event_post_probe.py)

## 4. Quality Gates

- [x] `ruff check .`
- [x] `ruff format --check .`
- [x] `mypy verdict --strict`
- [x] `check_reachability --ratchet-base origin/main`
- [x] `check_openspec_tasks --base origin/main`
- [x] Full test suite with `-n auto` (known unrelated: test_doctor_service_code_path prime_sync x2, test_lazy_imports::test_version_cli_output_unchanged)

## 5. Live Verification

Verification method: manual test of `verdict choose --task-class implementation --no-confirm --json` on installed main; observes `selected.route_id` and `reached: "SELECTED"` in JSON output. This test verifies the fix works end-to-end.

- [ ] (post-merge) Install from merged main and confirm `verdict choose --task-class implementation --no-confirm --json` selects a route