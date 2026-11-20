# lint-autofix-before-gates: apply safe lint fixes before repo gates

## Why

Self-host runs and real-story replays showed `verdict orchestrate` discarding
correct implementations because a worker introduced a trivially fixable lint
issue (unused import F401, naming convention N818) that the repo gates then
rejected. The worker had no way to know the fix was required before submission,
and the retry attempt received the same correction feedback without any
mechanical repair. The result was wasted attempts and paid tokens spent on a
problem that `ruff check --fix` and `ruff format` could resolve in milliseconds.

The root cause: `_run_repo_gates` ran against the worker's raw output. A
reactive `_repair_formatting` path only handled `ruff-format` gate failures, and
only after the gate had already failed — not `ruff-check` failures.

## What Changes

**`verdict/orchestration/runtime.py`**

- `_repair_formatting` removed. Replaced by `_apply_lint_autofix`, a proactive
  method that runs `ruff check --fix` (safe fixes only, no `--unsafe-fixes`) and
  `ruff format` on the node's owned changed files before `verification_command`
  and `_run_repo_gates` are called. Both the verifier and the gates see the fixed
  code.
- `_apply_lint_autofix` runs only when: (a) the node is an IMPLEMENT node with
  changed files, (b) ruff is a declared gate (`ruff-check` or `ruff-format`) in
  `discover_repo_gates`, and (c) each target file is owned by the node and is a
  real file (no symlinks, no path-escape). If ruff is missing from the
  environment, a `lint_autofix` event with `ruff_missing=True` is emitted and
  gates run unchanged.
- `_validate` calls `_apply_lint_autofix` after the ownership check and before
  the `if node.verification_command:` block.

**`verdict/orchestration/receipt.py`**

- `_node_record` records `lint_autofix` data (`files_changed`, `ruff_missing`,
  `skipped`) in the per-attempt node record when a `lint_autofix` event is
  present.

**`verdict/orchestration/contracts.py`**

- `"lint_autofix"` added to the `EVENT_TYPES` frozenset so `RunEvent` accepts
  the new event type.

**`tests/test_repo_gates_runtime.py`**

- Updated `test_implement_node_gate_failure_is_correction_feedback` and
  `test_existing_debt_in_one_gate_does_not_hide_a_new_failure_in_another` to
  guard against the proactive autofix runner calls.
- Updated `test_formatting_only_failure_is_repaired_with_the_formatter` and
  `test_lint_failures_are_never_auto_fixed` to assert the `lint_autofix` event
  instead of the removed `FORMATTER_APPLIED` controller state.
- Added four new tests: `test_lint_autofix_runs_before_verification_and_gate`,
  `test_lint_autofix_event_in_node_receipt`, `test_ruff_missing_records_event_gates_proceed`,
  `test_autofix_skipped_when_no_ruff_gate`.

## Capabilities

### New Capabilities

- `lint-autofix-before-gates`: before running `verification_command` and
  `_run_repo_gates` on an IMPLEMENT node, the runtime applies safe lint fixes
  (`ruff check --fix`, no `--unsafe-fixes`) and formatting (`ruff format`) to the
  node's owned changed files. This runs only when ruff is a declared gate
  (`ruff-check` or `ruff-format`). The outcome is recorded as a `lint_autofix`
  event (files touched, before/after digests, exit codes, skipped/ruff_missing
  flags) and surfaced in the per-attempt node record in the receipt. If ruff is
  not found, the event is recorded with `ruff_missing=True` and gates proceed
  unchanged. Autofix never widens the node's file scope.

### Modified Capabilities

None. The old `_repair_formatting` reactive path is removed; `lint-autofix-before-gates`
replaces it completely.

## Evidence boundary

KNOWN: real-story runs discarded implementations with fixable F401 and N818 ruff
errors (confirmed in self-host run evidence). KNOWN: `_repair_formatting` handled
only `ruff-format` gate failures reactively; `ruff-check` failures were never
auto-fixed. KNOWN: `ruff check --fix` only applies fixes that ruff classifies as
safe (no `--unsafe-fixes`). INFERRED: auto-fixing before verification also lets
the verifier see correct style. NOT AVAILABLE before merge: live acceptance run
from merged main against a real orchestrate story.

## Acceptance criteria

- An IMPLEMENT node whose worker introduced a fixable lint issue passes its gates
  without a failed attempt after `_apply_lint_autofix` fixes it.
- Autofix runs before `verification_command` and before `_run_repo_gates`.
- If ruff is not a declared gate, `_apply_lint_autofix` skips (emits
  `skipped=True`) and gates run unchanged.
- If ruff is missing from the environment, `_apply_lint_autofix` emits
  `ruff_missing=True` and gates run unchanged (no crash).
- Autofix never touches files outside the node's `owned_files` scope.
- `--unsafe-fixes` is never passed to `ruff check`.
- The `lint_autofix` event is recorded in the per-attempt node receipt record.
- All 27 tests in `test_repo_gates_runtime.py` and 175 orch tests pass.

## Impact

`verdict/orchestration/runtime.py`, `verdict/orchestration/receipt.py`,
`verdict/orchestration/contracts.py`, `tests/test_repo_gates_runtime.py`.

IMPLEMENT node attempts that produced only fixable lint errors now complete
without a failed attempt. The reactive `_repair_formatting` path is removed.
No other orchestration path, admission logic, ranking, or receipt format is
changed.
