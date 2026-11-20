# ADR-045: Safe lint autofix before the repo gates

**Status:** Accepted (merged in #1188; the operator was told about the reversal
on 2026-11-20 and did not object)
**Date:** 2026-11-20
**Deciders:** Operator
**Related:** ADR-036 (goal-to-receipt orchestration), ADR-041 (generic harness
contracts)

## Context

### Prior behavior

The orchestration runtime never applied `ruff check --fix` to a node's output
before running the repo gates. Only a reactive `_repair_formatting` path
handled `ruff format` failures — and only after the gate had already failed.
This policy was encoded in the test
`test_lint_failures_are_never_auto_fixed` in
`tests/test_repo_gates_runtime.py`, introduced in PR #811 (commit `8519e54a`,
"Release trains 1+2: self-hosting Verdict, verdict improve, quota-aware
routing, release-review fixes").

The CI pipeline already auto-fixed via PR #817 (commit `ea4c7afd`, "Stop false
done: executable definition of done, OpenSpec and reachability ratchets,
proof-path guard, pipeline lint autofix"): the `lint` GitHub Actions job
commits `ruff check --fix-only` + `ruff format` on same-repo PRs. The runtime
node was the remaining gap.

### Why the policy changed

Production orchestrate runs discarded correct implementations because a worker
introduced a trivially fixable lint issue (F401 unused import, N818 naming
convention) that the repo gates then rejected. The retry cycle received the
same correction feedback and restarted from the clean base, burning paid
tokens on a problem that `ruff check --fix` could resolve in milliseconds.
Every retry wastes per-token quota with no quality gain. This directly
conflicts with the mission goal of accepted work per paid dollar (BOD-342
D15).

### Live failure after the initial fix (PR #1190)

After PR #1188 merged, a node that owned
`openspec/changes/doctor-capability-gateway-probe/.openspec.yaml` had
`_apply_lint_autofix` pass that file to `ruff check --fix` and `ruff format`.
Ruff treats any explicitly-named file as Python and rewrote
`schema: verdict-change-v1` to `schema: verdict - change - v1`, corrupting
the YAML and blocking independent review (commit `190e18bd`). PR #1190
restricted autofix to Python files only.

## Decision

Replace the reactive `_repair_formatting` path with a proactive
`_apply_lint_autofix` method in `verdict/orchestration/runtime.py`. The method
runs `ruff check --fix` (safe fixes only; `--unsafe-fixes` is never passed)
followed by `ruff format` on the node's owned changed files before
`verification_command` and `_run_repo_gates` are called. Both the verifier and
the gates see the corrected code. Only what remains after auto-fix can fail.

### Preconditions

Auto-fix runs only when all of the following hold:

1. The node is `NodeKind.IMPLEMENT`.
2. Ruff is a declared gate (`ruff-check` or `ruff-format` in
   `discover_repo_gates`).
3. Each target file is owned by the node and is a real file (no symlinks, no
   path escape).
4. The file extension is `.py` or `.pyi` (`_RUFF_PYTHON_EXTENSIONS =
   frozenset({".py", ".pyi"})`).

Non-Python files owned by the node are recorded in `skipped_non_python` on the
`lint_autofix` event and are not passed to ruff.

### Recording

Each attempt that reaches the auto-fix step emits a `lint_autofix` event
(added to `EVENT_TYPES` in `verdict/orchestration/contracts.py`) with the
files touched, before/after digests, exit codes, and flags:

- `ruff_missing=True` — ruff binary not found; gates proceed unchanged.
- `skipped=True` with `reason="no_ruff_gate"` — ruff is not a declared gate;
  no ruff call is made.
- `skipped_non_python` — list of owned files skipped because their extension
  is not `.py` or `.pyi`.

The per-attempt node record in the receipt carries the `lint_autofix` data.

### What is not changed

- Admission, ranking, receipt digest binding, and attempt budgets are
  untouched.
- Safe fixes only. `--unsafe-fixes` is never passed.
- Autofix is scoped to the node's owned changed files only.
- If ruff is missing or not a declared gate, behavior is unchanged from before
  PR #1188.

## Consequences

- Trivially fixable lint issues (unused imports, naming conventions) no longer
  cause retry cycles that waste tokens and quota.
- `test_lint_failures_are_never_auto_fixed` keeps its old name, but PR #1188
  updated its docstring and assertion to verify the `lint_autofix` event. The
  name no longer describes what the test checks; renaming it is a follow-up.
- OpenSpec changes: `lint-autofix-before-gates` (#1188, merge SHA
  `fbc06ab5f67a0cca81232e34056fc94f90269bdf`) and `lint-autofix-python-only`
  (#1190, merge SHA `2c85357ffe339e4363c018d4576b99a501520d8d`).
- A live acceptance run confirming that `verdict orchestrate` completes without
  a lint gate failure on a story that previously failed with F401/N818 is an
  open post-merge task in
  `openspec/changes/lint-autofix-before-gates/tasks.md`.
