# OpenSpec Change: choose-no-confirm-valid-proof

## Summary
Modify `verdict choose --no-confirm` to select routes with valid exact-route proof (passport admitted and not expired) even when that proof is older than the 10-minute permit freshness window (600 seconds). Previously, `--no-confirm` only selected routes with "fresh" proof (within 600 seconds of `qualified_at`).

## Motivation
The operator ruling (2026-11-18) states that `verdict choose --no-confirm` should select among routes whose exact-route proof is still VALID (the passport is admitted and not expired), even when that proof is older than the 10-minute permit freshness. The previous behavior incorrectly treated valid-but-older passports as stale and required a probe, which with a zero probe budget resulted in `probe_budget_exhausted` and no route selected.

## Specification Changes
Modified `openspec/specs/route-selection/spec.md`:
- Updated "Requirement: Read-only choose" to accept proof older than 10 minutes when passport remains valid
- Added three scenarios:
  1. Valid but older-than-10-minute proof is selected (zero probes, zero writes)
  2. Expired or missing passport is not selected (named reason, not probe_budget_exhausted)
  3. No valid proof returns null

## Implementation Plan
1. Add `read_only` flag to `EligibilityLadder.__init__`
2. In `EligibilityLadder.select()`, when `read_only=True`, treat proof as stale only when:
   - Route not in `passport_confirmation(...)['admitted']` (passport missing, expired, unauthorized, ineligible, or newer negative evidence)
   - NOT when `qualified_at` exceeds 600 seconds
3. Pass `read_only=no_confirm` from `build_selector` in `eligibility_report.py`

## Testing
- Unit tests for no-confirm mode with valid-but-old passport
- Unit tests for expired/missing passport with named reasons
- Regression tests for normal mode (confirm allowed) behavior unchanged

## Tasks
- [ ] (post-merge) Install from merged main and confirm `verdict choose --task-class implementation --no-confirm --json` selects a route