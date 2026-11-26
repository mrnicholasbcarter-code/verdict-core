# Design

## Context

Two defects stopped the fourth self-build run (`20261126T082406Z`, #1354) from finishing
unattended. Both were reproduced here before any code changed.

**Gates.** `resolve_gate_argv` (`verdict/orchestration/verification.py`) turns a bare `ruff` or
`mypy` into `uv run --frozen <tool>` when the worktree has `uv.lock` and no `.venv`. uv then
builds an environment of the base dependencies (`Installed 15 packages`). Neither `ruff` nor `mypy` is a base dependency (both are in the `dev`
extra), so `uv run` fell back to the global ones on `PATH`, and a global `mypy` cannot see the
project's typed libraries: `mypy --strict verdict` printed 50 errors in 16 files, 24 of them
unresolved imports (`prompt_toolkit`, `fastapi`, and stubs for `yaml`, `jsonschema`,
`defusedxml`) and 26 others (22 `untyped-decorator`, 3 `no-any-return`, 1 `misc`). With
`--all-extras` the same tree is `Success: no issues found in 381 source files`.

**Workers.** The executor starts `opencode run` with `start_new_session=True` and, on every exit
path of an attempt, signals that process group, waits, and signals it again. Run against a local
fake model server (no live call), the installed OpenCode 2.3.2 shows the tree it makes:
`opencode run` starts `opencode serve --stdio --port 0` in a session and group of its own, that
server starts an `opencode-pty daemon` in another, and the shell command of a tool call runs in
the daemon's session. A command the model leaves running (`sleep 777 &` in the probe) was alive
six seconds after `opencode run` exited 0, with its parents gone and the attempt's environment
variable still in its environment. Signalling the group of `opencode run` cannot reach any of
them.

What was not reproduced: a surviving `opencode run` leader. On the success path the executor
reaps its group before it returns, and the probe showed the leader gone. The leftover
`opencode run --standalone` processes in the evidence run are not explained by the source; the
processes that are explained are the ones outside the group.

## Goals / Non-Goals

**Goals:** repo gates see the project's dependencies; an environment fault at integration is
labelled as one; no process of an attempt outlives it; a signalled orchestrator ends its workers.

**Non-Goals:** the other items of #1354; naming extras per project; changing the node-level base
comparison; the exit-126/127 wording; non-Linux process discovery.

## Decisions

### D1. mypy runs with all extras; ruff and everything else are unchanged

`--all-extras` on `uv run --frozen`: assessed first. The lock already resolves every extra, so
the flag installs and never re-resolves. It is a superset of what AGENTS.md prescribes
(`--extra dev --extra server --extra dashboard`; it adds `tracing` and `all`) and the strict
check passes in it. The project declares nowhere which extras its gates need, so none is guessed
by name. Only `mypy` gets it: it is the gate that needs the project's libraries; `ruff` reads source and
imports nothing from the project.

The command stays as it is today when the project declares no extras, declares
`[tool.uv] conflicts` (uv refuses `--all-extras` then; naming the non-conflicting extras instead
is left out), or has a `pyproject.toml` that cannot be read: the flag is a decision, never a
failure. A `.venv` that was not made by uv is the operator's and is used as is (existing
behaviour). A `.venv` uv made (uv writes `uv = <version>` into its `pyvenv.cfg`) is the very
environment `uv run` uses, and an earlier gate's `uv run --frozen ruff` in the same worktree
creates exactly such a one with the base dependencies and, when mypy is a base or default-group
dependency, with mypy but without the extras: that one is completed through `uv run`, not used as
it is. If `uv` is not on `PATH` the venv is used, so the rule never turns a runnable gate into an
error.

Known limits: the first mypy gate in a fresh worktree installs every extra (about a minute
without a warm uv cache, under the gate's own timeout); an ordering the sequence of gates can
produce is covered above only for uv-made venvs.

### D2. Why the node gates passed and the integration gate did not

Node-level gates ran in the same broken environment and failed the same way (the evidence shows
two failed mypy gates). `_validate` forgives a gate that also fails on the node's base
(`_gate_fails_on_base`): existing debt is not the node's fault. The base worktree had the same
broken environment, so the environment fault looked like debt and every node validated. The
integration barrier has no base comparison and no worker, so it blocked. The resolver is shared
by the node worktree, the base worktree and the integration worktree, so D1 reaches all three; the
base comparison stays as it is.

### D3. An environment fault at integration is an `environment_error`

`classify_gate_env_error` reads the whole gate output (not the 600-character tail the reason
keeps): a failed `mypy` gate with at least one `import-not-found` or `import-untyped` error,
every one for a third-party module, is an environment error. "Third party" means the name is not
a package or module of the worktree at its root or under `src/`; an unresolved import of the
project's own module is a wrong implementation. Other mypy errors in the same output are not told
apart (the observed output mixes them; all of them pass with the libraries installed), so a
genuine type error beside the import errors shows once the environment is fixed.

Applied only at the integration barrier (`classify_environment=True`): at node level an import
error that the base does not also have goes back to the worker, as before. A gate that failed for
another reason wins: the result is `repo_gate_failed`, so the implementation is never reported as
sound because another gate's environment was broken. The failure takes the existing fail-closed
path (`FailureClassification("environment_error", "BLOCK", 0, "none")`, REJECTED then BLOCKED),
and `_record_failure` does not count it as a judged failure. The reason names three packages at
most; no other output text reaches it. Only the output the runner kept is read (its last 4,000
characters): a first-party error in a cut-off part is not seen.

Left as it is: a gate that cannot start (exit 126 or 127) stays `verification_failed` at
integration, as existing tests pin.

### D4. Every process of an attempt carries a marker, and ends with the attempt

`_build_child_env` sets `VERDICT_WORKER_ATTEMPT` to a value unique to the attempt. The
processes the worker starts inherit it wherever they are, so `/proc/<pid>/environ` finds them.
`_reap_attempt` runs in the `finally` that already surrounds collection, so it covers success,
failure, stall, timeout, output limit and cancellation: it ends the group (`_kill_process_group`,
unchanged), then `_reap_marked`: SIGTERM, one second of grace, SIGKILL, searching again after
each signal until a search finds nothing or three seconds are spent. Its last act, on any way
out, is a synchronous SIGKILL pass, so a second cancellation inside the wait leaves nothing
behind. The attempt directory is removed after all of that. Worst case for a worker that ignores
SIGTERM: 5 s (group) plus 3 s, inside the supervisor's 10 s kill grace. A spawn that is cancelled
while it starts gets the SIGKILL pass too.

Matching is by exact marker, never by a name or a command line, and a search skips its own
process. Limits: Linux only (`/proc`; elsewhere the group kill is all there is); a process that
scrubs its environment (`env -i`) is not found; a process of another user cannot be read; a PID
can be reused between a search and a signal (microseconds); a process in uninterruptible sleep
survives SIGKILL and is left after the bound.

### D5. SIGTERM and SIGINT cancel the run; the process then exits as a signalled one

A default SIGTERM ends the orchestrator on the spot, and with it every attempt's cleanup.
`run_golden_path` is wrapped so that, in the main thread, SIGTERM and SIGINT cancel its task.
`DagRuntime.run` already cancels and awaits its node drivers, and each driver's attempt ends its
worker (D4). The wrapper then raises `SystemExit(128 + signal)`: `cli.py` is not changed, and a
`CancelledError` would otherwise print a traceback. The first signal takes the handlers down, so
a second signal keeps its default effect, the operator's way out of a cleanup that hangs, and the
previous handlers are restored on the way out. Outside the main thread nothing is installed.

The signal is deliberately not routed into the operator's cancel request: that settles the run
as CANCELLED with `run_finished`, whereas the supervisor reads "exited without `run_finished`"
as an interrupted controller and resumes it.

## Risks / Trade-offs

- `--all-extras` is slower the first time in a fresh worktree and needs the extras' wheels (from
  uv's cache or the index). It fails closed: a failure is a gate failure, as before.
- The marker search reads the environment of every process of the user once per attempt end; a
  few milliseconds on this host.
- An environment error at integration blocks the run exactly as a verification failure did: the
  change is the label, the route-health writes (none) and the judged-failure count (not
  incremented).

## Security / trust implications

- The marker is a random value used only to find the attempt's own processes. It is not a
  credential; it is in the worker's environment next to the worker assignment, which is the
  worker's only credential and is unchanged. Signalling is limited to processes of this user that
  carry the marker; nothing is matched by name.
- Gate output is read for the classification, and only package names reach the reason.

## Routing / context / memory implications

None. The change is in how gates are executed and how worker processes are ended; admission,
routing, context packs and memory are untouched, and `tests/test_admission.py` is unchanged.

## Migration / rollback

No data migration. Existing receipts and events are unchanged except that a `repo_gate` event may
carry an `environment_error` field and the failure category of an integration environment fault
is `environment_error`. Roll back by restoring `uv run --frozen <tool>`, removing the
`classify_environment` argument, and restoring the single `_kill_process_group` call and the
unwrapped `run_golden_path`.

## ADR impact

None. ADR-053 (OpenCode worker adapter) describes the worker's isolation; this change makes its
process cleanup complete. ADR-036 (goal-to-receipt orchestration): the supervisor still reads
"exited without `run_finished`" as an interrupted controller.
