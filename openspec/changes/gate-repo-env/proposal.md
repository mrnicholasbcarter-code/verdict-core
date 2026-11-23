### Proposal: repo gate resolution uses the target repo's own environment

## Why

Orchestration repo gates (ruff, mypy) must run inside the target repository's
own environment.  Before this change, `resolve_gate_argv` fell through to
`shutil.which` and picked up the global tool (e.g. `~/.local/bin/mypy`), which
lacks the project's stubs and plugins.  For verdict-core this produced 49
missing-stub errors on every node — errors that were silently ignored, making
the gate useless.

A gate that cannot find the right environment is a named environment failure,
never a silent pass (issue #1276).

**Evidence boundary**

- KNOWN: `resolve_gate_argv` in `verdict/orchestration/verification.py` is the
  sole resolution point for `ruff` and `mypy` gate argv before subprocess
  dispatch.
- KNOWN: when `uv.lock` is present the worktree is uv-managed and `uv run
  --frozen <tool>` runs within the locked environment.
- KNOWN: `uv run --frozen mypy --version` in a fresh verdict-core worktree
  returns `mypy 2.3.1` from the locked venv in ~3 s (measured 2026-11-23).
- INFERRED: a worktree with neither `.venv/bin/<tool>` nor `uv.lock` is not
  uv-managed; Verdict-env and PATH fallbacks remain valid for those repos.
- NOT AVAILABLE: live CI evidence showing gate exit-0 on a clean verdict-core
  node (requires a live orchestrate run; blocked on DoD step 1).

## What Changes

- **`verdict/orchestration/verification.py` — `resolve_gate_argv`**: updated
  resolution order:
  1. `<worktree>/.venv/bin/<tool>` — explicit venv (unchanged).
  2. `uv run --frozen <tool>` — when `uv.lock` is present. `uv` must be on
     PATH; if not, `FileNotFoundError` is raised (named environment failure).
  3. `Path(sys.executable).parent/<tool>` — Verdict's own env (unchanged).
  4. `shutil.which(<tool>)` — OS PATH (unchanged, only reached without
     `uv.lock`).
  5. `FileNotFoundError` — gate cannot run; callers fail closed.

- **`tests/test_repo_gates_runtime.py`**: six new unit/integration tests covering
  the uv.lock branch, the FileNotFoundError path, and the unchanged non-uv
  fallback.

## Capabilities

### Modified Capabilities

- **Repo gate execution environment**: gates for uv-managed repos now run via
  `uv run --frozen` instead of a global tool. Gates for non-uv-managed repos
  are unaffected.

## Impact

- Verdict-core orchestrate runs: mypy/ruff gates now use the locked project
  environment, eliminating false 49-error failures.
- Non-uv repos: no change.
- Tests: 6 new tests; all existing gate tests continue to pass.

## Acceptance criteria

On installed main, an orchestrate run on verdict-core shows `repo_gate` events
whose `executed_command` starts with the absolute path to `uv` and includes
`run --frozen`, with `exit_code: 0` on a clean node.  A node that introduces a
type error must fail the gate with `exit_code: 1`.
