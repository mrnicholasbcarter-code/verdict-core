# AD-03: `verdict autodev` selects its own executor

## Why
`verdict autodev "<objective>" --repo <path> --allow-live` without
`--executor-model` has no selection path today: `cmd_autodev` builds launch
authority only when a model is pinned (`verdict/cli.py`), so the unpinned
command fails at `_require_launch_decision` with "missing
ExecutionPathDecision". The operator's ask is the opposite: the command picks a
FREE executor itself from live eligibility evidence, and the receipt shows why
that route won and which candidates it beat, so nobody has to trust a pinned
model name.

## What Changes
- Add `prepare_live_autodev_selected` beside the unchanged pinned
  `prepare_live_autodev`: ask the live ladder (`build_selector` +
  `EligibilityLadder.select`) for task-eligible FREE routes, keep the top 3 in
  rank order, and probe each with the same identity/content/zero-cost checks the
  pinned path already uses.
- Offer every route that answered correctly to
  `verdict.execution_path.optimize_execution_path` as its own `direct_cheap`
  candidate, in ladder order, so the decision carries `why_selected` and the
  losing candidates in `rejected`. Return the ladder order for retry use.
- `cmd_autodev` takes that path when no pin is given; the selected route becomes
  the executor and orchestrator model, and `--output-json` gains
  `executor_selection` (`why_selected`, `selected`, `rejected`, `probed`,
  `dropped`), with the same facts in the human summary.
- The autodev outcome receipt records `execution_path_why_selected`,
  `execution_path_rejected` and `executor_probe_dropped` next to
  `execution_path_selected_route`, so a route that failed its probe stays
  visible even when another route survived.
- Both gateway-key lookups in `cmd_autodev` use
  `verdict.credentials_store.resolve_api_key()` instead of reading
  `OMNIROUTE_API_KEY` from the environment directly.

## Capabilities

### New Capabilities
- `autodev-executor-selection`: unpinned autodev selects, probes and reports its
  own free executor, and fails closed instead of falling back to a paid route.

### Modified Capabilities
- None. The pinned `--executor-model` path and its probe keep their behaviour.

## Acceptance criteria
- `verdict autodev "<objective>" --repo <path> --allow-live` with no
  `--executor-model` runs a free route the ladder chose, and the JSON report
  shows `executor_selection.why_selected` plus the rejected candidates.
- The persisted autodev receipt holds `execution_path_why_selected`,
  `execution_path_rejected` and `executor_probe_dropped` unredacted.
- With no eligible free route, or when every probed free route fails its
  identity/content/zero-cost check, the command raises before planning and
  launches nothing; no paid or frontier route is offered as a fallback.

## Impact
- `verdict/autodev_run.py`: new `prepare_live_autodev_selected`; two new receipt
  keys in `run_autodev`.
- `verdict/cli.py`: `cmd_autodev` selection branch, `executor_selection` payload,
  `resolve_api_key()` for the gateway key.
- `verdict/credentials_store.py`: taken from commit 40af6218 (the BOD-354
  resolver) so `resolve_api_key` exists on this branch; no hand-written resolver.
- `tests/test_autodev_wiring.py`: five new tests, no existing assertion changed.

## Evidence boundary
- KNOWN: the pinned probe contract, the optimizer's `why_selected`/`rejected`
  fields, the ladder's rank and stage semantics, and the receipt store's
  redaction rules were read in this repository and are covered by the tests
  added here.
- INFERRED: which free routes the live ladder ranks first on the operator's
  gateway at run time; that is measured per run, never assumed.
- NOT AVAILABLE: a live end-to-end unpinned run on the operator's gateway. This
  change ships the code path and its hermetic tests; the real-command proof is
  the ticket owner's step.
