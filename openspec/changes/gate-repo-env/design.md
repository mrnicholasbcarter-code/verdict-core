### Design — repo gate resolution uses the target repo's own environment

## Context

`verdict orchestrate` runs repo quality gates (ruff-check, ruff-format, mypy)
after each integration step.  The gate argv is discovered from the target
repo's `pyproject.toml` (e.g. `mypy --strict verdict`) and then resolved to an
executable path before dispatch.

Before this change, `resolve_gate_argv` searched: (1) `<worktree>/.venv/bin`,
(2) `Path(sys.executable).parent`, (3) `shutil.which`.  When Verdict's own
interpreter directory did not contain mypy, step 3 returned the global tool at
`~/.local/bin/mypy`, which lacks the project's stubs.  For verdict-core this
caused 49 missing-stub errors per node that were silently ignored.

## Goals / Non-Goals

**Goals**
- Gates for uv-managed repos use the locked project environment via `uv run
  --frozen`.
- A missing `uv` binary when `uv.lock` is present is a named environment
  failure, not a silent pass.
- Non-uv-managed repos are unaffected.

**Non-Goals**
- Multi-project orchestration with heterogeneous lock formats.
- Automatic `uv sync` on first run (intentionally avoided — `--frozen`
  enforces the lock).

## Decisions

**Gate on `uv.lock`, not `pyproject.toml`.**  `uv.lock` is the authoritative
signal that a repo is uv-managed with a deterministic lock.  `pyproject.toml`
alone is ambiguous.  The existing test `repo` fixture writes `pyproject.toml`
but no `uv.lock`; gating on `pyproject.toml` would have broken all existing
runtime gate tests by trying to invoke the real `uv` binary.

**Use `--frozen`.**  `uv run --frozen` refuses to modify the lock file and
errors if the lock is out of date.  This prevents inadvertent lock updates
inside an orchestration worktree and surfaces lock drift as an explicit error.

**`uv.lock` present but `uv` not found → `FileNotFoundError`.**  The gate
cannot run in the right environment; a silent pass is worse than a failure.
The existing 127-exit-code path in `_run_repo_gates` already converts this to
a named `environment_error` event.

**Fallback order preserved for non-uv repos.**  Steps 3 and 4 (Verdict-env,
PATH) remain valid for repos without `uv.lock`.  This preserves compatibility
with the npm gate path and hypothetical non-uv Python repos.

## Risks / Trade-offs

- First-time `uv run` in a worktree with no `.venv` creates a venv (~3 s for
  verdict-core with 15 base packages; full dev env ~377 ms after first sync).
  This is per-node at the integration barrier, which is already the slow path.
- If `uv` is not on PATH in a CI environment, verdict-core gates fail closed.
  This is intentional: a misconfigured environment must not silently pass.

## Security / trust implications

No new network access.  `uv run --frozen` reads only the local lock file and
the project's local venv.  No new external inputs.

## Routing / context / memory implications

No change to route selection, context packs, or memory.  Gate resolution is
a pure filesystem/PATH lookup.

## Migration / rollback

Rollback: revert `verification.py` to the previous resolution order.  No
schema changes, no persisted state.

Existing worktrees without `uv.lock` are unaffected; the new branch is only
reached when `uv.lock` exists.

## ADR impact

No new ADR required.  Consistent with ADR-036 (goal-to-receipt) which requires
quality gates to run correctly in the integration barrier.
