# Proposal: Cheapest sufficient route first (slack before rating) with role split

## Why

Every node in the DAG goes to the tier-0 (frontier) subscription route because
the current `_rank_key` puts rating before slack.  A tier-1 route and a tier-0
route for a tier-1 required node both pass admission, but the tier-0 route ranks
higher because it usually has a stronger declared tier and therefore a better
rating position.  This burns premium quota (tier 0) on work a tier-1 route can
already do.

The operator decision (2026-11-20) supersedes D3 of the
`2026-11-15-shared-selection-contract` for the sub-ordering within a capacity
class for **worker/implement nodes**: **cheapest sufficient first** — the route
with the smallest slack (least tier overage above the task floor) wins.  Rating
and Elo then break ties among routes at the same slack.  Subscription token usage
(D3's ordered key) moves after slack and before rating.

A second operator decision (also 2026-11-20) scopes this change: **planner and
reviewer roles keep strongest-first ordering** because decomposition quality
drives everything downstream and the marginal cost difference on control-plane
calls is acceptable.  For those roles, rating and declared tier come before slack.

## What Changes

- `verdict/orchestration/contracts.py` (`TaskRequirements`): add field
  `role_ordering: str = "cheapest_sufficient_first"`.  Worker/implement nodes use
  the default.  Planner and reviewer callers pass `"strongest_first"`.
- `verdict/orchestration/eligibility.py` (`_rank_key`): support two orderings
  via a threading-local flag set in `_assess_all` from `requirements.role_ordering`.
  Cheapest-sufficient-first puts slack at position 2; strongest-first puts rating
  before slack.
- `verdict/orchestration/eligibility.py` (`_rank_components`): `"ordering_basis"`
  is now dynamic — `"cheapest_sufficient_first"` or `"strongest_first"`.
- `verdict/orchestration/eligibility.py` (`_rank_components_for_display`): pass
  through the dynamic `ordering_basis` value from components.
- `verdict/orchestration/eligibility.py`: update docstrings for
  `_rank_components`, `_rank_key` and `_rank_components_for_display`.
- `verdict/orchestration/run.py` (planner requirements): add
  `role_ordering="strongest_first"` to `TaskRequirements`.
- `verdict/orchestration/review.py` (reviewer requirements): add
  `role_ordering="strongest_first"` to `TaskRequirements`.
- `verdict/role_selection.py` (`PLANNER_REQUIREMENTS`): add
  `role_ordering="strongest_first"` so `build_ladder` uses strongest-first for
  the planner role.
- `tests/test_orch_eligibility.py`: update two existing test assertions
  (`test_cheaper_sufficient_beats_unnecessary_premium`,
  `test_different_work_units_get_different_models_from_same_inventory`) to match
  the new cheapest-sufficient-first behaviour; add `TestCheapestSufficientFirst`
  class with three failing-first tests; add `TestRoleOrdering` class with one
  test per ordering side.
- `tests/test_explain_selection.py`: update the ordered key list in
  `test_selection_identical_for_representative_fixture` and the hardcoded sort
  tuple in `test_rank_key_built_from_rank_components`.
- `docs/CONFIGURATION.md`: update the advisory-ranks tie-break description.

## Supersession of D3

D3 of `2026-11-15-shared-selection-contract` states that subscription ordering
uses token consumption (never quota) and places it before rating.  This change
inserts slack ahead of token consumption for worker/implement nodes: the full
sub-ordering within a capacity class is now `slack → subscription tokens →
rating`.  Token consumption still sorts before rating (D3's core rule is
preserved); slack is inserted before it as a new primary within-class key.
Planner and reviewer roles keep the original D3 order: `subscription tokens →
rating → … → slack`.

## Capabilities

### Modified Capabilities

- `orchestration/selection`: `_rank_key` sort order changes by role.  Admitted
  set and per-route eligibility logic are unchanged.  The cheapest-sufficient
  invariant (smallest slack wins within a class for worker nodes) is now enforced
  rather than aspirational.  Planner and reviewer nodes continue to prefer the
  highest-rated admitted route.

## Evidence boundary

KNOWN: `_rank_key` in `verdict/orchestration/eligibility.py` sorts by rating before
slack today; every DAG node dispatches to the tier-0 subscription route regardless of
its declared floor. KNOWN: `TaskRequirements` has `max_capability_tier` but no
`role_ordering` field; planner and reviewer callers build requirements in
`verdict/orchestration/run.py`, `verdict/orchestration/review.py`, and
`verdict/role_selection.py`. KNOWN: `threading.local` (`_select_stats`) is already in
`EligibilityLadder` for per-select state; reading `strongest_first` from it in
`_rank_key` is safe because `_assess_all`, `_rank_key`, and their callers all run on the
same thread per select. INFERRED: planner and reviewer roles benefit from
strongest-first because decomposition quality drives downstream work; the marginal
routing cost is small relative to quality loss from a weaker planner. NOT AVAILABLE
until a live run: whether the new ordering materially changes the tier-1 hit rate in
real stories.

## Acceptance Criteria

- [x] For a task requiring tier 1 with `role_ordering="cheapest_sufficient_first"`,
  a tier-1 subscription route (slack=0) is selected over a tier-0 subscription
  route (slack=1).
- [x] After escalation to ceiling=0 the tier-1 route drops as
  `below_escalation_floor` and the tier-0 route is selected.
- [x] Two admitted routes with equal slack are ordered by rating quality
  (declared-tier evidence beats unknown).
- [x] For a task with `role_ordering="strongest_first"`, the highest-tier admitted
  route is selected even when it has worse slack.
- [x] `test_rank_key_built_from_rank_components` (500 synthetic routes) passes.
- [x] `test_selection_identical_for_representative_fixture` passes with the
  updated ordered key list.
- [x] `TestRoleOrdering` both sides pass.
- [x] `mypy --strict verdict` (exit 0).
- [x] `ruff check .` and `ruff format --check .` (exit 0).
- [x] `verdict openspec admit cheapest-sufficient-first` (exit 0).

## Impact

Affected modules:
- `verdict/orchestration/contracts.py` — `role_ordering` field on `TaskRequirements`
- `verdict/orchestration/eligibility.py` — dual sort order, dynamic `ordering_basis`
- `verdict/orchestration/run.py` — planner uses `role_ordering="strongest_first"`
- `verdict/orchestration/review.py` — reviewer uses `role_ordering="strongest_first"`
- `verdict/role_selection.py` — `PLANNER_REQUIREMENTS` uses `role_ordering="strongest_first"`
- `verdict/autodev_run.py` — `_live_planner_models` uses `role_ordering="strongest_first"`
- `tests/test_orch_eligibility.py` — test assertions and new role-ordering tests
- `tests/test_explain_selection.py` — fixture and tuple tests
- `docs/CONFIGURATION.md` — advisory-ranks description

No new dependencies.  No network calls added.  No schema version bump required.
