# Tasks

## 1. Implementation

Verification: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/lint-autofix-python-only` exits 0. Test: all 31 tests in `tests/test_repo_gates_runtime.py` pass.

- [x] 1.1 Add `_RUFF_PYTHON_EXTENSIONS` constant to `verdict/orchestration/runtime.py`.
- [x] 1.2 Split `targets` in `_apply_lint_autofix` into `python_targets` and
  `non_python_targets`; pass only `python_targets` to ruff.
- [x] 1.3 Emit `skipped_non_python` field in every `lint_autofix` event emitted
  after the extension split.
- [x] 1.4 Update docstring in `_apply_lint_autofix` to describe Python-only scope.

## 2. Tests

Verification: `uv run python -m pytest -q tests/test_repo_gates_runtime.py` — 31 tests pass, 0 failures. Test: each assertion is specific to the invariant it checks.

- [x] 2.1 Add `test_non_python_owned_files_are_never_passed_to_ruff`: node owns
  `.py`, `.yaml`, `.md`; assert `.yaml` and `.md` never appear in ruff argv;
  assert `.yaml` bytes unchanged; assert `skipped_non_python` in event.
- [x] 2.2 Add `test_all_non_python_owned_files_emit_no_ruff_calls`: only
  `.yaml` and `.md` owned; assert no ruff calls; assert `files=[]` in event;
  assert `skipped_non_python` lists both files.
- [x] 2.3 Update existing autofix tests to use `owned_file="a.py"` so the
  Python-only filter does not suppress the ruff invocations they depend on.
- [x] 2.4 Add `owned_file` parameter to `make_runtime`; add `_FileExecutor`
  for non-default owned files.

## 3. Lint, type checks, and gates

Verification: all commands below exit 0. Test: each command exits 0 with no errors reported.

- [x] 3.1 `uv run ruff check . && uv run ruff format --check .`
- [x] 3.2 `uv run mypy verdict --strict`
- [x] 3.3 `uv run python scripts/check_lint_gates.py`
- [x] 3.4 `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/lint-autofix-python-only`
- [ ] (post-merge) 3.5 Push through vgate; CI exact-head checks green; squash-merge to main.

## 4. Post-merge live acceptance

Verification: operator runs `verdict orchestrate` on a story that owns a `.yaml` file and confirms the yaml is not corrupted after the autofix step. Test: post-merge live proof recorded in evidence.

- [ ] (post-merge) 4.1 From merged main, run `verdict orchestrate` on a story
  that includes a node owning an OpenSpec `.yaml` file; confirm the yaml is not
  corrupted and the node passes review.
