# Orchestrate: integrate nodes are merge-only; a verification command that cannot collect is an environment error

> **Scope.** Issue [#1356](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1356), split from
> [#1354](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1354). Not built here: validating
> verification commands before dispatch, free-first route failures, serve load, repository-wide tests in
> node verification, keeping rejected attempts.

## Why

Both real self-build runs of 2026-11-26 (`20261126T003756Z`, `20261126T020427Z`) ended BLOCKED before
review, each after 45 to 55 minutes of paid worker time, for plan defects, not worker defects.

1. **An integrate node was given authoring work.** The node `deliver_integrate` had `owned_files` (a new
   test and a task list). The runtime never dispatches a worker for an integrate node (`_drive` sends it
   to `_integrate_node`), so nothing was written, its verification command named a file that did not
   exist (pytest exit 4), and the node was judged `verification_failed`.
2. **A verification command could not collect.** The node `deliver-e2e` ran `python -m pytest -q tests -k
   deliver` where pytest exited 2 with `59 errors during collection`, none in a file the node owned. It was
   judged `verification_failed -> CORRECT_IMPLEMENTATION`, rerouted, and ended `pool_exhausted`.

## What Changes

- A `WorkNode` of kind `integrate` or `review` with `owned_files` is refused, with a message that says to
  move the authoring work to an implement node. The planner prompt states the rule; the planner's
  existing repair call receives the message. A fresh graph (`verdict orchestrate --graph`) gets the same
  check and is refused with exit 2 before any run starts; a graph read back from a recorded run's
  `graph.json` (resume, controls, receipts) is not re-checked.
- `classify_verify_env_error` takes the node's `owned_files`. A pytest run that exits 2 on collection
  errors, none in a file the node owns, is an `environment_error` on the existing path; its reason names
  the command and the first erroring paths. Everything else stays `verification_failed`, including an
  erroring path that is absolute or has a `..` part, which cannot be matched to an owned file.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `orchestrate-plan-verify-guards`: merge-only integrate and review nodes; collection errors in
  unowned files classified as `environment_error`.

## Impact

`verdict/orchestration/{contracts,planner,verification,runtime}.py` and their tests. Admission, routing,
other recovery categories and `verdict serve` are not changed.

KNOWN: both defects, from the events of the two runs; the graph validation and the classifier, from the
source at `9bca4982`. INFERRED: that a repository-wide command's collection errors are environmental
(see the design's risks). NOT AVAILABLE: the full pytest output of run `20261126T020427Z`; its event keeps
the last 600 characters.

## Acceptance criteria

- An integrate node with `owned_files` is refused, the repair call receives the reason, and a file-less one
  is accepted; the tests in `tasks.md` 1.1 to 1.3 fail on `9bca4982` and pass.
- The exact tail of run `20261126T020427Z` is an `environment_error` and the node ends BLOCKED after one
  attempt with no reassignment, cooldown or escalation count (tasks 2.1, 2.2).
- A collection error in an owned file, exit 4, exit 5 and test failures stay `verification_failed`.
- `tests/test_admission.py` is unchanged; CI is green on the exact head.
