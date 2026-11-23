# ADR-043: Role-dependent selection order

**Status:** Accepted (operator, 2026-11-20)
**Date:** 2026-11-20
**Deciders:** Operator
**Amends:** ADR-042 (ordering stage within a capacity class)
**Related:** ADR-0001 (control-plane invariants), ADR-032 (Core model metadata
store), ADR-042 (dynamic role-based model selection)

## Context

Before this change, `_rank_key` in `verdict/orchestration/eligibility.py`
placed `rating_rank` before `slack` for every role. Because frontier-tier
(tier-0) routes carry a stronger declared tier and higher rating, they
consistently won over tier-1 routes for work that required at most tier 1.
Every DAG node dispatched to the subscription frontier route regardless of its
declared capability floor, burning paid quota on tasks a cheaper route could
already handle.

The operator stated two rules on 2026-11-20 (source: PR #1194 and the
`openspec/changes/cheapest-sufficient-first` proposal):

- For implement/worker nodes: "we want the correct model to do it not the
  greatest model we have".
- For planning: "we want the opposite its where we want almost frontier" and
  "use highest rated one for planning".

External evidence cited by the operator on 2026-11-20 supports a role split:
Aider's architect/editor separation (2024-09-26 post); Anthropic's internal
multi-agent research system, where an Opus-led planner with Sonnet subagents
scored higher on an internal eval than a single agent (operator-cited figure;
not independently verified here); Plan-and-Act (arXiv:2503.09572); LLMCompiler
(arXiv:2312.04511). These are cited as motivating evidence, not as
verified facts of this repository.

## Decision

Add `role_ordering: str = "cheapest_sufficient_first"` to `TaskRequirements`
in `verdict/orchestration/contracts.py`. Two orderings are defined:

### cheapest_sufficient_first (default — implement/worker nodes)

Rank key (ascending, so the first tuple element wins):

1. `capacity_order` (capacity class precedence: FREE < SUBSCRIPTION < METERED)
2. `slack` (`max_capability_tier − capability_tier`; smallest first — the route
   that just clears the task floor wins over an over-qualified route)
3. subscription token usage (known usage first; applies to subscription-class
   routes only)
4. `rating_rank` (best-rated route wins as a final tiebreak)

This supersedes the sub-ordering in D3 of
`openspec/changes/2026-11-15-shared-selection-contract` for the within-class
order: slack is now inserted at position 2, before subscription tokens and
rating.

### strongest_first (planner and reviewer roles)

Rank key (ascending):

1. `capacity_order` (same as above — capacity class is always the primary key)
2. subscription token usage
3. `rating_rank` (best-rated route comes first — quality wins over cost)
4. `slack` (slack is a tiebreak only after rating)

Callers that pass `role_ordering="strongest_first"` (verified against source):

- `verdict/orchestration/run.py` — planner `TaskRequirements` in
  `plan_with_failover`
- `verdict/orchestration/review.py` — reviewer `TaskRequirements` in `review`
- `verdict/role_selection.py` — the module-level constant
  `PLANNER_REQUIREMENTS` (used by `build_ladder` for the planner role)
- `verdict/autodev_run.py` — autodev planner `TaskRequirements` in
  `_live_planner_models`

All other callers use the default `cheapest_sufficient_first`.

The `ordering_basis` field in `rank_components` (and
`rank_components_for_display`) reflects the active ordering so callers can
confirm which policy produced the selection.

### Invariants kept

- `capacity_order` is key 1 (the first element) in both orderings. Capacity
  class precedence is never overridden by role.
- Ranking never changes admission membership. The admitted set is fixed before
  `_rank_key` runs; `_rank_key` only orders within that set.
- Unknown is never sufficient. A route with an unknown capability tier is never
  treated as satisfying a task's capability floor.

## Consequences

- Implement and worker nodes now dispatch to the cheapest route proven
  sufficient for the task tier rather than always to the frontier. This reduces
  paid quota consumption per DAG node.
- Planner and reviewer nodes continue to use the highest-rated admitted route,
  preserving decomposition quality.
- ADR-051 (accepted 2026-11-23) narrows the planner candidate pool further: at
  runtime, `plan_with_failover` restricts planners to routes whose stripped
  model key falls in the frontier band (top `VERDICT_PLANNER_FRONTIER_TOP`
  fraction of `aa_intelligence`-rated models, default 20 %). When the frontier
  band is empty, it falls back to the fallback band (default 30 %); when that
  too is empty it falls back to `PLANNER_MAX_CAPABILITY_TIER=2`. This band
  filter runs on top of the `strongest_first` ordering and does not change the
  admission invariant.
- `_probe_order` slices `[:4]` from `_rank_key`: for `cheapest_sufficient_first`
  that captures `(capacity, slack, tokens_known, tokens_float)`; for
  `strongest_first` it captures `(capacity, tokens_known, tokens_float,
  rating_rank)`. Probe ordering stays consistent with the new rank key.
- Two previously-pinned test assertions in `test_identify_rank.py` were updated
  to document the new cheapest-sufficient-first behaviour (PR #1194).
- A live acceptance run confirming tier-1 route selection for tier-1 required
  nodes is an open post-merge task in
  `openspec/changes/cheapest-sufficient-first/tasks.md`.
