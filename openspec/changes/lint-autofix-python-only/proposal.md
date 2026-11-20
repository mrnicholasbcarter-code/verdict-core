# lint-autofix-python-only: restrict autofix to Python source files

## Why

`_apply_lint_autofix` (added in `lint-autofix-before-gates`) passed every owned
changed file to `ruff check --fix` and `ruff format` regardless of extension.
When ruff receives an explicitly named non-Python file it processes it as Python
syntax, corrupting YAML, Markdown, TOML and other formats.

Live failure (2026-11-20): a node with `owned_files` containing
`openspec/changes/doctor-capability-gateway-probe/.openspec.yaml` triggered
`ruff format` on it.  The file's `schema: verdict-change-v1` was rewritten to
`schema: verdict - change - v1` (ruff inserted spaces around the `-`),
breaking the OpenSpec validator and causing the independent review to block
the run.  The integrated commit `190e18bd` contained the corrupted value.

Repro: `printf 'schema: verdict-change-v1\n' > /tmp/x.yaml && ruff format /tmp/x.yaml && cat /tmp/x.yaml` outputs `schema: verdict - change - v1`.

## What Changes

**`verdict/orchestration/runtime.py`**

- New constant `_RUFF_PYTHON_EXTENSIONS = frozenset((".py", ".pyi"))`.
- In `_apply_lint_autofix`, after computing `targets`, split into
  `python_targets` (suffix in `_RUFF_PYTHON_EXTENSIONS`) and `non_python_targets`
  (all others). Only `python_targets` are passed to ruff. When
  `python_targets` is empty, the event is emitted with `files=[]` and
  `skipped_non_python=non_python_targets` and the method returns without calling
  ruff.
- Every `lint_autofix` event emitted after the split now carries
  `skipped_non_python=[...]`.
- Docstring updated to describe Python-only scope and the reason for it.

**`tests/test_repo_gates_runtime.py`**

- Added `test_non_python_owned_files_are_never_passed_to_ruff` (regression test).
- Added `test_all_non_python_owned_files_emit_no_ruff_calls`.
- Updated `make_runtime` to accept `owned_file` parameter; added `_FileExecutor`
  for non-default owned files.
- Updated 5 existing autofix tests to use `owned_file="a.py"` so the new
  Python-only filter does not suppress the ruff invocations they depend on.

## Capabilities

### Modified Capabilities

- `lint-autofix-before-gates` (modified): the autofix step now only processes
  `.py` and `.pyi` files. Non-Python owned changed files are recorded in the
  `lint_autofix` event as `skipped_non_python` and are never passed to ruff.
  All other behaviour (gate-conditional, owned-only, fail-safe ruff_missing,
  skipped when no ruff gate) is unchanged.

## Evidence boundary

KNOWN: `ruff format <explicit-non-py-file>` reformats the file as Python syntax,
corrupting YAML/TOML (confirmed by live failure and local repro above).  KNOWN:
ruff's `include`/`exclude` config is bypassed when files are named explicitly on
the command line; the only safe filter is the extension.  INFERRED: `.py` and
`.pyi` are the only extensions ruff treats as Python source when invoked without
explicit file arguments; named files have no extension guard.  NOT AVAILABLE
before merge: live acceptance run from merged main.

## Acceptance criteria

- A node that owns a `.yaml`, `.md`, and `.py` file passes `_apply_lint_autofix`
  with only the `.py` in the ruff argv; the `.yaml` bytes are identical before
  and after.
- When every owned changed file is non-Python, ruff is not called at all, the
  `lint_autofix` event has `files=[]`, and `skipped_non_python` lists the
  skipped files.
- All 31 tests in `tests/test_repo_gates_runtime.py` pass.
- `uv run ruff check . && uv run ruff format --check .` exit 0.
- `uv run mypy verdict --strict` exits 0.
- `uv run python scripts/check_lint_gates.py` exits 0.

## Impact

`verdict/orchestration/runtime.py`, `tests/test_repo_gates_runtime.py`.

IMPLEMENT node attempts that own non-Python files (specs, docs, YAML configs)
no longer risk having those files corrupted by `ruff format`.  Python files are
still auto-fixed as before.
