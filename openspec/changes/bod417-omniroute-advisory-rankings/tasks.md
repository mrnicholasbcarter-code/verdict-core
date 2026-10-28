# Tasks

## 1. Spec authoring (this change)

- [x] 1.1 Create `.openspec.yaml` with schema verdict-change-v1
- [x] 1.2 Write `proposal.md` mapping BOD-417 AC1-AC6 to why/what/capabilities/impact/evidence boundary/acceptance
- [x] 1.3 Write `design.md` with advisory-overlay decision, exact matching, metric separation, bounded fetch, category plumbing
- [x] 1.4 Write `tasks.md` with test-first verification for each increment
- [x] 1.5 Validate `openspec validate bod417-omniroute-advisory-rankings --strict` passes
- [x] 1.6 Validate `python3 scripts/validate_openspec_change.py bod417-omniroute-advisory-rankings` passes

## 2. Implementation (next packet; not part of this change)

- [x] 2.1 Add tracked RED tests for `rankings[]` API shape, exact match, mismatch, alias, metric separation, excluded-top regression, fallback (fail before production edit)
- [x] 2.2 Implement optional OmniRoute rankings fetch/parse in `verdict/omniroute_rankings.py` (bounded, existing transport/credential resolver)
- [x] 2.3 Wire rankings advisory into `EligibilityLadder._rank_components`/`_rank_key` and `build_selector`
- [x] 2.4 Propagate coding/debugging/review category through `_action_choose_live` → selection contracts
- [x] 2.5 Keep `_rank_components`, `_rank_key`, `_probe_order` consistent; preserve admission and generic-gateway fallback
- [x] 2.6 Run focused static + unit + neighbor suites (RED → GREEN); strict mypy/ruff once
- [x] 2.7 Parent runs real `verdict choose` for implementation/debugging/review; exact-head OCR; PR/CI/merge/main replay
