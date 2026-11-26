# Orchestrate: integrate nodes are merge-only; a verification command that cannot collect is an environment error

> **Scope.** Issue [#1356](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1356), split from
> [#1354](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1354). Not built here: validating
> verification commands before dispatch, free-first route failures, serve load, repository-wide tests in
> node verification, keeping rejected attempts.

## Why

Both real self-build runs of 2026-11-26 (`20261126T003756Z`, `20261126T020427Z`) ended BLOCKED before
review, each after 45 to 55 minutes of paid worker time, for plan defects, not worker defects.

1. **An integrate node was given authoring work.** The node `deliver_integrate` had `owned_files` (a
   new test and a task list). The runtime never dispatches a worker for an integrate node (`_drive`
   sends it to `_integrate_node`), so nothing was written, its verification command named a file that
   did not exist (pytest exit 4), and the node was judged `verification_failed`.
2. **A verification command could not collect.** The node `deliver-e2e` ran `python -m pytest -q tests
   -k deliver` where pytest exited 2 with `59 errors during collection`, none in a file the node owned.
   It was judged `verification_failed -> CORRECT_IMPLEMENTATION`, rerouted, and ended `pool_exhausted`.

## What changes

- A `WorkNode` of kind `integrate` or `review` with `owned_files` is refused, with a message that says
  to move the authoring work to an implement node. The planner prompt states the rule; the planner's
  existing repair call receives the message. A graph read back from `graph.json` is not re-checked, so
  recorded runs still resume and their receipts still verify. The runtime still skips dispatch for these nodes.
- `classify_verify_env_error` takes the node's `owned_files`. A pytest run that exits 2 on collection
  errors, none in a file the node owns, is an `environment_error` on the existing path (one attempt,
  BLOCKED, no escalation count, no reroute, no cooldown, no ledger or health entry); its reason names
  the command and the first erroring paths. A collection error in an owned file, unreadable erroring
  paths, exit 4, exit 5 and ordinary test failures stay `verification_failed`.

Admission, routing, other recovery categories and `verdict serve` are not changed.

## Limit

If a node's own source file breaks imports for test files it does not own, under a repository-wide
verification command, the output cannot say whose edit caused it and the node is also classified
`environment_error`. Narrowing such commands is the separate "repository-wide tests" item.
