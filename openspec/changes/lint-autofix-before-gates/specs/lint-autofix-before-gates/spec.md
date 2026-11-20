# Spec Delta

## Purpose

Before running the verification command and repo gates on an IMPLEMENT DAG node,
the runtime applies safe lint fixes (`ruff check --fix`) and formatting
(`ruff format`) to the node's owned changed files. Both the verifier and the
gates see the corrected code. The outcome is recorded as a `lint_autofix` event
in the event log and surfaced in the per-attempt node record in the receipt.

## ADDED Requirements

### Requirement: Safe lint fixes are applied before verification and gates on IMPLEMENT nodes

For every IMPLEMENT DAG node attempt that has changed files, when ruff is a
declared gate (`ruff-check` or `ruff-format`) in the worktree's declared gate
list, the runtime SHALL run `ruff check --fix` (safe fixes only, no
`--unsafe-fixes`) followed by `ruff format` on the node's owned changed files
before calling `verification_command` and `_run_repo_gates`. The binary used
SHALL be the same one resolved by `_resolve_gate_argv` for the declared gate.

#### Scenario: Fixable lint issue is auto-corrected before the gate sees it

- **WHEN** an IMPLEMENT node attempt changes `a.txt` with a fixable lint error (F401, N818)
- **AND** ruff is a declared gate in the repo's configuration
- **THEN** `ruff check --fix a.txt` and `ruff format a.txt` are called before `verification_command` and `_run_repo_gates`
- **AND** both the verifier and the gate see the corrected file
- **AND** a `lint_autofix` event is emitted with `skipped=False` and `ruff_missing=False`
- **AND** the node validates without a lint gate failure

#### Scenario: Non-fixable lint error still fails the gate

- **WHEN** an IMPLEMENT node attempt changes `a.txt` with a lint error that `ruff check --fix` cannot resolve (exit 1)
- **THEN** `_run_repo_gates` runs against the (unchanged) file
- **AND** the gate fails with the original lint error as correction feedback
- **AND** the `lint_autofix` event has `changed_files=[]`

#### Scenario: Autofix calls precede verification and gate calls

- **WHEN** `_validate` runs for an IMPLEMENT node with changed files and a ruff gate
- **THEN** the `ruff check --fix` and `ruff format` calls are made before the `verification_command` subprocess call
- **AND** before any `ruff check` or `ruff format --check` gate calls

### Requirement: Autofix is skipped when ruff is not a declared gate

When ruff (`ruff-check` or `ruff-format`) is not among the worktree's declared
gates, the runtime SHALL emit a `lint_autofix` event with `skipped=True` and
`reason="no_ruff_gate"` and SHALL NOT call ruff on any file. Gates run unchanged.

#### Scenario: Mypy-only repo skips autofix

- **WHEN** the repo declares only a mypy gate (no ruff gate)
- **THEN** `_apply_lint_autofix` emits `lint_autofix` with `skipped=True`, `reason="no_ruff_gate"`
- **AND** no ruff subprocess is called

### Requirement: Ruff missing records the event and gates proceed unchanged

When ruff cannot be resolved by `_resolve_gate_argv` (`FileNotFoundError`), the
runtime SHALL emit a `lint_autofix` event with `ruff_missing=True` and SHALL NOT
raise an exception. The subsequent `_run_repo_gates` call SHALL proceed normally
(which will also fail to find ruff and handle it via its own gate resolution path).

#### Scenario: Ruff not installed in environment

- **WHEN** ruff is not found in the worktree venv, the Verdict environment's bin directory, or PATH
- **THEN** `_apply_lint_autofix` emits `lint_autofix` with `ruff_missing=True`, `skipped=False`
- **AND** the method returns without raising
- **AND** `_validate` continues to `verification_command` and `_run_repo_gates`

### Requirement: Autofix never widens the node's file scope

The runtime SHALL pass to ruff only files that satisfy all of:
1. the file is in the node's `changed` list,
2. `node.owns(path)` returns True,
3. the path is a regular file (not a symlink),
4. the resolved absolute path is under the worktree root.

No other files SHALL be passed to `ruff check --fix` or `ruff format`.

#### Scenario: Only owned changed files are passed to ruff

- **WHEN** an IMPLEMENT node with `owned_files=("a.txt",)` changes `a.txt` and `b.txt` is also present in the worktree but not owned
- **THEN** ruff is called with `a.txt` only
- **AND** `b.txt` is not included in the ruff invocation

### Requirement: lint_autofix event is recorded in the per-attempt node receipt record

When a `lint_autofix` event is present in the event log for a node attempt, the
per-attempt record in `receipt["nodes"]` SHALL include a `lint_autofix` field
with:
- `files_changed`: list of files whose digest changed after autofix,
- `ruff_missing`: bool,
- `skipped`: bool.

#### Scenario: Successful autofix reflected in receipt

- **WHEN** `_apply_lint_autofix` runs and ruff fixes at least one file
- **THEN** `receipt["nodes"][n]["attempts"][k]["lint_autofix"]["files_changed"]` lists the fixed files

#### Scenario: No files changed in receipt

- **WHEN** `_apply_lint_autofix` runs but ruff makes no changes (all files already clean)
- **THEN** `receipt["nodes"][n]["attempts"][k]["lint_autofix"]["files_changed"]` is `[]`
