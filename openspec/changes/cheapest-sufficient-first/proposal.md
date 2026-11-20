# Proposal: Cheapest sufficient route first (slack before rating)

## Why

Every node in the DAG goes to the tier-0 (frontier) subscription route because
the current `_rank_key` puts rating before slack.  A tier-1 route and a tier-0
route for a tier-1 required node both pass admission, but the tier-0 route ranks
higher because it usually has a stronger declared tier and therefore a better
rating position.  This burns premium quota (tier 0) on work a tier-1 route can
already do.

The operator decision (2026-11-20) supersedes D3 of the
`2026-11-15-shared-selection-contract` for the sub-ordering within a capacity
class: **cheapest sufficient first** — the route with the smallest slack (least
tier overage above the task floor) wins.  Rating and Elo then break ties among
routes at the same slack.  Subscription token usage (D3's ordered key) moves
after slack and before rating.

## What Changes

- `verdict/orchestration/eligibility.py` (`_rank_key`): move `c["slack"]` from
  position 7 to position 2 (right after `capacity_order`), ahead of subscription
  token ordering and rating.  Update the type annotation accordingly.
- `verdict/orchestration/eligibility.py` (`_rank_components`): add constant
  `"ordering_basis": "cheapest_sufficient_first"` to the components dict.
- `verdict/orchestration/eligibility.py` (`_rank_components_for_display`): move
  `"slack"` and add `"ordering_basis"` before `"rating_basis"` so the display
  dict reflects the actual sort order.
- `verdict/orchestration/eligibility.py`: update docstrings for
  `_rank_components`, `_rank_key` and `_rank_components_for_display`.
- `tests/test_orch_eligibility.py`: update two existing test assertions
  (`test_cheaper_sufficient_beats_unnecessary_premium`,
  `test_different_work_units_get_different_models_from_same_inventory`) to match
  the new cheapest-sufficient-first behaviour; add `TestCheapestSufficientFirst`
  class with three failing-first tests.
- `tests/test_explain_selection.py`: update the ordered key list in
  `test_selection_identical_for_representative_fixture` and the hardcoded sort
  tuple in `test_rank_key_built_from_rank_components`.
- `docs/CONFIGURATION.md`: update the advisory-ranks tie-break description.

## Supersession of D3

D3 of `2026-11-15-shared-selection-contract` states that subscription ordering
uses token consumption (never quota) and places it before rating.  This change
inserts slack ahead of token consumption: the full sub-ordering within a capacity
class is now `slack → subscription tokens → rating`.  Token consumption
still sorts before rating (D3's core rule is preserved); slack is inserted before
it as a new primary within-class key.

## Capabilities

### Modified Capabilities

- `orchestration/selection`: `_rank_key` sort order changes.  Admitted set and
  per-route eligibility logic are unchanged.  The cheapest-sufficient invariant
  (smallest slack wins within a class) is now enforced rather than aspirational.

## Acceptance Criteria

- [x] For a task requiring tier 1, a tier-1 subscription route (slack=0) is
  selected over a tier-0 subscription route (slack=1).
- [x] After escalation to ceiling=0 the tier-1 route drops as
  `below_escalation_floor` and the tier-0 route is selected.
- [x] Two admitted routes with equal slack are ordered by rating quality
  (declared-tier evidence beats unknown).
- [x] `test_rank_key_built_from_rank_components` (500 synthetic routes) passes.
- [x] `test_selection_identical_for_representative_fixture` passes with the
  updated ordered key list.
- [x] `mypy --strict verdict` (exit 0).
- [x] `ruff check .` and `ruff format --check .` (exit 0).
- [x] `verdict openspec admit cheapest-sufficient-first` (exit 0).

## Impact

Affected modules:
- `verdict/orchestration/eligibility.py` — sort order, components dict
- `tests/test_orch_eligibility.py` — test assertions
- `tests/test_explain_selection.py` — fixture and tuple tests
- `docs/CONFIGURATION.md` — advisory-ranks description

No new dependencies.  No network calls added.  No schema version bump required.
