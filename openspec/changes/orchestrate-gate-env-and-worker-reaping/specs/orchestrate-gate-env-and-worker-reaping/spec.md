# Spec Delta: orchestrate gate environment and worker reaping

## ADDED Requirements

### Requirement: Repo gates SHALL run in the project's environment, and an environment fault SHALL NOT be a wrong implementation

`resolve_gate_argv` SHALL run a bare `mypy` gate of a uv-managed worktree (`uv.lock` present)
as `uv run --frozen --all-extras mypy ...` when its `pyproject.toml` declares optional
dependencies and no `[tool.uv] conflicts`. A `ruff` gate, a project without extras, a project
with declared conflicts and an unreadable `pyproject.toml` SHALL keep `uv run --frozen <tool>`.
A worktree `.venv` that uv did not make SHALL be used as it is. One resolver SHALL serve the node
worktree, the base worktree and the integration worktree.

At the integration barrier a failed repo gate SHALL end the node as `environment_error` (action
`BLOCK`, no cooldown, not a judged failure), not as `verification_failed`, in two cases only: the
gate command could not start (`resolve_gate_argv` raised `FileNotFoundError`, or the exit code is
126 or 127), or a failed `mypy` gate has unresolved imports of third-party packages that the same
gate also cannot resolve on the run's untouched base commit, and every other error it reports is
one the base reports too. Unresolved imports alone SHALL NOT be enough: an import the base
resolves (a typo, a deleted top-level package) and an error the base does not report (a new type
error) SHALL stay `verification_failed`. When the base cannot be checked there is no evidence and the failure SHALL
stay `verification_failed`. When the runner cut the head off the gate's output (fewer errors are
listed than mypy's total), the candidate SHALL also report no more errors in total than the base.
A gate that failed for another reason SHALL take precedence whichever gate comes first, and an
unresolved import of a module of the project itself SHALL stay `verification_failed`.

#### Scenario: Resolution in a project with extras
- **WHEN** the worktree has `uv.lock` and a `pyproject.toml` with `[project.optional-dependencies]`
- **THEN** the mypy gate resolves to `uv run --frozen --all-extras mypy ...`, and the ruff gates to `uv run --frozen ruff ...`

#### Scenario: Cases that keep the command
- **WHEN** the project declares no extras, declares `[tool.uv] conflicts`, or has an unreadable `pyproject.toml`
- **THEN** the mypy gate resolves to `uv run --frozen mypy ...` and nothing raises

#### Scenario: Worktree environments
- **WHEN** the worktree has a `.venv` with `mypy` that uv did not make
- **THEN** that mypy is used as it is
- **WHEN** the `.venv` was made by uv and the project declares extras
- **THEN** the gate runs through `uv run --frozen --all-extras`, which completes that environment

#### Scenario: A real project
- **WHEN** a uv project's typed module imports a library whose stubs only an extra installs
- **THEN** the resolved mypy gate passes where `uv run --frozen mypy` fails with `import-untyped`

#### Scenario: Environment fault at integration
- **WHEN** the integration mypy gate fails with unresolved third-party imports and the same gate fails on the run's base with the same unresolved packages (other mypy errors in the output do not change this)
- **THEN** the failure event has category `environment_error` and action `BLOCK`, the node ends REJECTED then BLOCKED, its judged-failure count stays 0, and the reason starts `environment_error: repo_gate_failed: mypy exit 1`

#### Scenario: A gate that cannot start at integration
- **WHEN** a repo gate's executable cannot be found (`uv.lock` without `uv` on PATH included) or the command exits 126 or 127
- **THEN** the failure is `environment_error` with action `BLOCK` and judged-failure count 0, and the reason names the gate and the exit code
- **WHEN** another gate failed for a real reason, before or after it
- **THEN** the failure is `verification_failed` and names that gate

#### Scenario: A missing import the implementation introduced
- **WHEN** the integration mypy gate cannot resolve a package (a typo such as `reqeusts`) that the base resolves, or one more package than the base cannot resolve, or the base cannot be checked
- **THEN** the failure is `verification_failed` with action `CORRECT_IMPLEMENTATION`, the `repo_gate` event records why it was not the environment (`environment_ruled_out`), and the base worktree and branch are removed

#### Scenario: A new error beside the environment fault
- **WHEN** the integration mypy gate reports the unresolved imports the base has and also an error the base does not report (for example `[return-value]`)
- **THEN** the failure is `verification_failed`, and a further unresolved import of a package the base cannot resolve either does not change that

#### Scenario: A cut error list
- **WHEN** the kept output lists fewer errors than mypy's total and the total exceeds the base's
- **THEN** the failure is `verification_failed`

#### Scenario: Real failures are not hidden
- **WHEN** the unresolved import is for a module of the project, or another gate failed for a real reason
- **THEN** the failure is `verification_failed` and names that gate

### Requirement: Every process of a worker's attempt SHALL end with the attempt

`OpenCodeExecutor` SHALL give each attempt's worker an environment variable
(`VERDICT_WORKER_ATTEMPT`) whose value is unique to the attempt. On every exit path of the attempt
(success, failure, stall, timeout, output limit, cancellation, and a second cancellation during
cleanup) it SHALL end the worker's process group and every process that carries the value,
including those in sessions of their own and orphans, wait for them within a bound, and only then
remove the attempt directory. It SHALL NOT end any process that does not carry the value.
`run_golden_path` SHALL turn SIGTERM and SIGINT, in the main thread, into cancellation of the
run, so that each attempt ends its worker before the process exits with status 128 plus the
signal number and without `run_finished`.

#### Scenario: Worker that exits 0 with a server of its own
- **WHEN** a worker exits 0 while a child in a session of its own, or an orphan of one, is running
- **THEN** none of them is running when `run` returns the terminal, and the attempt directory is gone

#### Scenario: Failure, stall, timeout and cancellation
- **WHEN** a worker fails, prints its final event and keeps running, outlives the attempt's timeout, or the attempt is cancelled
- **THEN** the worker and its children, in any session, are gone when `run` returns or raises `CancelledError`, and the attempt directory is gone

#### Scenario: Second cancellation during cleanup
- **WHEN** the attempt is cancelled again while it is ending the group, or while it is ending the other marked processes
- **THEN** the marked processes are still ended

#### Scenario: Unrelated processes
- **WHEN** a process that does not carry the attempt's value runs during the attempt, or another attempt runs beside it
- **THEN** it is not ended

#### Scenario: Orchestrator signalled
- **WHEN** an orchestrator process running `run_golden_path` receives SIGTERM or SIGINT while a worker runs
- **THEN** it exits with status 143 or 130 and no traceback, and neither the worker nor its server is running afterwards

#### Scenario: Handlers after a run
- **WHEN** a run returns
- **THEN** the caller's SIGTERM and SIGINT handlers are as they were before it

#### Scenario: A signal the embedding loop handles
- **WHEN** the caller's loop already handles SIGTERM or SIGINT with `add_signal_handler`
- **THEN** the run installs nothing for that signal and the caller's callback is still registered afterwards

#### Scenario: Handlers outside the main thread
- **WHEN** a run is started in a thread other than the main thread
- **THEN** no handler is installed and the run proceeds
