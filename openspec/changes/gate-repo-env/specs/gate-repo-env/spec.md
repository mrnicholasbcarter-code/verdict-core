# Spec Delta

## ADDED Requirements

### Requirement: gate executable resolution

Verdict SHALL resolve a bare `ruff` or `mypy` gate executable to the target
repo's own environment before dispatch, using the following order:

1. `<worktree>/.venv/bin/<tool>` — explicit virtualenv in the worktree.
2. `uv run --frozen <tool>` — when `uv.lock` is present in the worktree. When
   `uv.lock` is present and `uv` is not on PATH, Verdict SHALL raise
   `FileNotFoundError` (a named environment failure, never a silent pass).
3. `Path(sys.executable).parent/<tool>` — Verdict's own interpreter environment.
4. `shutil.which(<tool>)` — OS PATH, only reached when `uv.lock` is absent.
5. `FileNotFoundError` when nothing is found.

Other executable names (e.g. `npm`) SHALL be returned unchanged.

#### Scenario: uv-managed repo resolves via uv run

- **WHEN** a worktree has `uv.lock` and `uv` is on PATH
- **THEN** `resolve_gate_argv(("mypy", ...), worktree)` returns
  `[<uv_path>, "run", "--frozen", "mypy", ...]`

#### Scenario: uv-managed repo without uv on PATH fails closed

- **WHEN** a worktree has `uv.lock` and `uv` is NOT on PATH
- **THEN** `resolve_gate_argv` raises `FileNotFoundError` containing `"uv"`

#### Scenario: non-uv-managed repo uses existing fallback

- **WHEN** a worktree has neither `uv.lock` nor `.venv/bin/<tool>`
- **THEN** `resolve_gate_argv` uses `Path(sys.executable).parent` or PATH
