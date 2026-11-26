## Context

`verdict orchestrate` plans a graph of work nodes. An `integrate` or `review` node is never given a
worker: `DagRuntime._drive` sends it to `_integrate_node`, which merges the finished nodes and runs the
node's verification command. `WorkNode` already refuses an `implement` node without `owned_files`, but
accepted an integrate node with them, so a planner could hand authoring work to a node that never runs
it. After a worker's attempt, `_validate` runs the node's verification command; any non-zero exit was
`verification_failed`, except exit 126/127 and a missing `-m` module (`classify_verify_env_error`), which
are `environment_error` and take the fail-closed path (one attempt, BLOCKED, no escalation count).

## Goals / Non-Goals

- Goals: an integrate or review node cannot own files, with a message the planner's repair call can act
  on; a pytest run that cannot collect because of files the node does not own is an environment error.
- Non-goals: validating verification commands before dispatch, repository-wide tests in node
  verification, free-first route failures, serve load, keeping rejected attempts (other items of #1354).
  No change to admission, routing, other recovery categories, or the runtime's skip of integrate dispatch.

## Decisions

- **The rule sits in `WorkNode.__post_init__`**, next to the implement-node rules, so every construction
  path obeys it. `parse_plan` already wraps a `WorkNode` error as `parse_plan: node[i]: <message>` and
  `FrontierPlanner.plan` feeds that text into the single repair call; no planner wiring was needed.
  A repair that still breaks the rule fails as "plan invalid after one repair round", before any worker.
- **A recorded graph still loads.** `WorkGraph.from_dict` (receipts, resume, controls, `--graph`) reads
  persisted data with `allow_merge_only_owned_files=True`; the files are kept exactly, so the digest is
  unchanged. The retained proof run `docs/proof/dogfood-bod-225-live-2026-09-29` has such a node and its
  receipt must keep verifying. The planner calls `WorkNode.from_dict` without the flag.
- **`owned_files` is `None` by default** in `classify_verify_env_error`, so callers that pass nothing keep
  today's behaviour, while a node that owns nothing passes `()` and every erroring path counts as unowned.
- **Collection rule.** Exit code 2, a pytest command (`pytest`, `py.test`, `-m pytest`), the
  `N errors during collection` marker, and at least one erroring path read from `ERROR <path>` lines or
  `ERROR collecting <path>` headers, none owned (`path_is_owned`, the same test as `WorkNode.owns`). Any
  doubt (another exit code, no marker, no readable path, one owned path, an `ERROR` line that is not a
  plain path such as a path with a space, or a capped output that shows fewer paths than pytest
  counted) returns `None`, the old result. The cap is `VERIFY_OUTPUT_TAIL_CHARS`, shared with the runner.
  The reason carries the command and at most three paths, taken only from a path-character pattern, so no
  other output text reaches an event.
- Exit 4 and exit 5 stay `verification_failed`: for an implement node they mean the worker did not write
  its test.

## Risks / Trade-offs

- The runner keeps the last 4,000 characters of output, so with very many errors the first paths may be
  cut off. At the cap a list that shows fewer paths than pytest counted is not trusted, so a cut list
  stays `verification_failed` (a worker retry, as before this change); an output below the cap, such as
  the 600-character tail kept in an event, is judged on the paths it shows.
- If a node's own source file breaks imports for test files it does not own and its verification command
  is repository-wide, the output cannot say whose edit it was, and the node is classified
  `environment_error` (BLOCK, no retry). The repository-wide command is the defect fixed in a separate item.
- A persisted graph may keep an integrate node that owns files; the runtime still ignores them, as before.

## Security / trust implications

None. No credential, route, passport or admission data is read or written. The reason holds the
planner-written verification command, which the `verify` event already records, and file paths matched
by a restricted pattern from the command's output; no other output text is kept.

## Routing / context / memory implications

None for routing: the `environment_error` path writes no health-cache, session-ledger or selector entry
and sets no cooldown, so no route is penalised for a plan or environment defect. No context pack or
memory change; the planner prompt gains one rule and a rule 6 clause.

## Migration / rollback

No data migration. Recorded graphs load as before. Revert the commit to roll back; runs planned while it
was live have graphs that still load.

## ADR impact

None. [ADR-036](../../../docs/adr/ADR-036-goal-to-receipt-orchestration.md) already says integration
nodes merge validated commits; this change makes the graph say so.
