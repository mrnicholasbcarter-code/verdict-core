# Design

## Context

Every DAG node dispatches to the tier-0 (frontier) subscription route because
`_rank_key` in `EligibilityLadder` sorts by rating before slack.  A tier-1 route
and a tier-0 route for a tier-1 required node both pass admission, but the tier-0
route ranks higher because it usually has a stronger declared tier.  This burns
premium quota on work a weaker route can already do.

Planner and reviewer roles are different: decomposition quality drives everything
downstream, so near-frontier models are preferred even when a cheaper one would
technically pass admission.

## Goals / Non-Goals

Goals: implement cheapest-sufficient-first ordering for worker/implement nodes;
keep strongest-first ordering for planner and reviewer roles; expose the active
ordering policy in `rank_components["ordering_basis"]`.

Non-goals: changing admission, eligibility floors, the capacity class primary
sort key, or the advisory-signal path.  No new live calls in tests.

## Decisions

- **`role_ordering` field on `TaskRequirements`.** Default is
  `"cheapest_sufficient_first"`.  Callers that need strongest-first pass
  `role_ordering="strongest_first"` explicitly.  Explicit is better than
  inferring the role from the task floor.
- **threading.local flag in `_rank_key`.** `_assess_all` sets
  `self._select_stats.strongest_first` before sorting.  `_rank_key` reads it
  without a parameter change.  `_select_stats` is already `threading.local`;
  each call to `_assess_all` overwrites the flag before sorting, so concurrent
  selects on separate threads cannot interfere.
- **Two sort tuples, same length (13 elements).** Both orderings are explicit
  `return` paths in `_rank_key` with static types; no conditional mutation of a
  shared tuple.  This keeps mypy happy and makes the two policies easy to compare.
- **`ordering_basis` is now dynamic.** `_rank_components` reads
  `_select_stats.strongest_first` to emit the active value.  Consumers of
  `rank_components` see which policy produced the selection.
- **Three call sites updated.** `verdict/orchestration/run.py` (planner),
  `verdict/orchestration/review.py` (reviewer), and
  `verdict/role_selection.py` (`PLANNER_REQUIREMENTS`) each add
  `role_ordering="strongest_first"`.  Worker nodes use the default.

## Risks / Trade-offs

- Using `threading.local` couples `_rank_key` to call order inside
  `_assess_all`.  The current code path is deterministic (sort always follows
  `_assess_all`'s flag write), but a future refactor that calls `_rank_key`
  outside `_assess_all` would read a stale flag.  The docstring calls this out.
- The flag defaults to `False` (cheapest-sufficient-first) if `_assess_all` has
  never been called on this thread, which is the safe fallback.

## Security / trust implications

No new API calls.  `role_ordering` is set by trusted internal callers only
(`run.py`, `review.py`, `role_selection.py`); it cannot be injected via route
inventory, ticket text, or external data.

## Routing / context / memory implications

No change to admission, the capacity class primary sort, or memory.  The
advisory-signal path is unchanged; `ordering_basis` is an informational field
only.

## ADR impact

None.  Advisory signals still never change admission membership (ADR-0001).
The cheapest-sufficient-first invariant is now enforced rather than aspirational
for worker nodes.

## Migration / rollback

Revert `verdict/orchestration/contracts.py`, `eligibility.py`, `run.py`,
`review.py`, and `verdict/role_selection.py`.  No stored state changes shape;
existing `events.jsonl` files are unaffected.
