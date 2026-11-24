# Design

Source tags: **[main]** read at main `c51cd994`. **[U]** read on the unmerged OC1 branch at
head `f684a7df`; not on main, may change, re-read by task 1.1. **[exp]** observed in a
scratch repository with git 2.56.1 for this design (commands in "Verified git behaviour").

## Context

`verdict autodev --allow-live` decomposes an objective into work units, then
`_run_unit_once` runs each unit in the user's own working tree [main]:

1. snapshot the tree (`_tree_state`: every dirty path with a status and content hash),
2. try the mechanical tier (`_try_mechanical`, ruff) and, if it declines,
3. call `executor.execute_unit(unit)` (`PatchExecutor`: one gateway call, a diff, applied
   atomically after `git apply --check`),
4. attribute the change with `_touched_since` and judge it with `_verify` (any touched path
   outside `owned_files` fails; then the unit's command runs in the user's repo).

`PatchExecutor` cannot stage, commit, or leave the tree half-edited, so nothing needs to
roll back. An agent that edits files in place can do all of those. Running OpenCode with
`cwd=<user repo>` (the removed prototype did) is the source of the first two defects in
#1301. Defect 1: an out-of-bounds or failed attempt stays on disk. Defect 2: the executor's
context file is attributed to the unit by `_touched_since`. Defect 3 (retries leave
OpenCode) is OC2b's.

This change adds an executor that never lets the worker touch the user's tree.
`_run_unit_once` and `_verify` stay the single judge.

## Goals / Non-Goals

**Goals**

- A worker's edits, staging, commits, hooks, excludes and config never reach the user's
  repository. Only validated owned-file content is copied back.
- Out-of-bounds, empty, failed, timed-out or refused attempts leave the user's repository
  as it was (defined under "What byte-identical means").
- A failed `_verify` after a transfer restores the owned files.
- Opt-in by an explicit flag; fail closed with one message when opencode or serve is
  unavailable; no fallback.
- No dependence on how #1299 delivers the worker credential or on the context file.

**Non-Goals**

- Retries on OpenCode, lineage `harness=opencode`, default selection, a live run (OC2b).
- Sandboxing the worker's shell. ADR-053 Decision 4 accepts that risk. This change
  separates trees; it does not stop a worker from writing to an absolute path.
- `verdict autodev packet-execute` (operator decision).
- Isolating the mechanical tier. It still runs ruff in the user's tree, as today.
- Changing the default path, `PatchExecutor`, or any admission rule.

## Settled by the operator (2026-11-24, comments on #1301)

| # | Decision | Where it shows up |
|---|---|---|
| 1 | The attempt tree is a **separate clone**, not a linked worktree sharing the user's `.git`. A worker's commits, hooks and excludes must not reach the user's repository, and the seed must not write the user's uncommitted content into its object store. | D2, D3 |
| 2 | **Opt-in by explicit flag first, fail closed.** When opencode or serve is unavailable autodev stops with one clear message. No silent fallback to `PatchExecutor`. Default only after a live run (OC2b). | D8 |
| 3 | `verdict autodev packet-execute` is **out of scope**. | Non-Goals |
| 4 | **Split.** This change is OC2a: isolated executor, restore after a failed verify, `run_autodev` wiring behind the flag, retries disabled for it. | D7, "Not in this change" |

Everything else below is this design's proposal. The ones the operator may want to change
are listed under "Open points" with a recommended default.

## Divergences from the research

The research (`research/issue-1301.md`, recommendation Option B) is followed except for
these, all caused by decision 1 or by an experiment.

1. **The delta is computed from file contents, not by git.** The research computed the
   delta "against the seed SHA" with git in the attempt tree. With an unshared clone the
   worker has a shell inside the clone's `.git`. A worker-planted `core.fsmonitor` command
   ran when Verdict ran `git status` there, and a planted `pre-commit` hook ran on
   Verdict's `git commit` [exp]. Verdict therefore never runs git in an attempt tree after
   the worker has started (D4).
2. **No `_ensure_local_excludes` and no `commondir` hazard.** The research found OC1's
   helper edits the user's `.git/info/exclude` when `cwd` is a linked worktree [U]. In a
   clone `cwd` has its own `.git`, so the hazard does not exist. Autodev still never calls
   the helper.
3. **The gateway equality check is skipped for the isolated executor, not made to pass.**
   The research said "skip the gateway check". This design keeps the model-identity check
   and skips only the base-URL equality, because the executor's real transport is serve,
   not the gateway the route was probed on (D7).
4. **Credential delivery and lineage are not here.** The research listed them under OC2b.
   This design keeps them there and builds the seam instead (D9).

## Verified git behaviour

Observed with git 2.56.1 in a scratch repository under `/tmp`, `HOME` pointed at a temp
directory. The user repository had a tracked file modified, one deleted, one staged, one
untracked, one ignored, a `pre-commit` hook, a local `user.signingkey` and an
`.git/info/exclude` entry.

| Observation | Consequence |
|---|---|
| `git clone --no-local <repo>`: no `objects/info/alternates`, no object file with a link count above 1. A plain local clone hardlinked object files. | Use `--no-local`. |
| The clone had none of the user's hook, local config value or `info/exclude` entry. | The clone cannot leak them; nothing to scrub. |
| The clone's `origin` remote pointed at the user's repository path. | Remove the remote right after cloning, so a worker's plain `git push` has no target. |
| The clone's working tree is HEAD, not the user's dirty state. Ignored files are absent. | The seed must overlay the dirty state (D3). The worker has no `.venv` or caches (Risks). |
| A planted `core.fsmonitor` in the clone's `.git/config` ran when the host ran `git status` in the clone. | Never run git in an attempt tree after launch (D4). |
| A planted `.git/hooks/pre-commit` ran on the host's `git commit` in the clone. `-c core.hooksPath=/dev/null` with `--no-verify` did not run it. | Verdict's own seed-time git calls use `-c core.hooksPath=/dev/null`. |
| `git status --porcelain=v1 -z --untracked-files=all` lists modified, deleted, staged and untracked paths, and lists ignored paths never. | Use it as the seed's file list. |
| `git check-ignore --no-index -z --stdin` classifies paths that do not exist. `git status` and `git check-ignore` left the bytes of `.git/index` unchanged, including after `touch` made a file's mtime differ. | Ignore rules are read from the user's repository, which is trusted (D4). `.git/index` is still excluded from "byte-identical" because git may refresh it. |

The build repeats these as tests (tasks 2.1, 2.2) so they hold on whatever git version
runs them.

## Decisions

### D1. A separate executor; autodev stays the judge (research Option B)

`OpenCodeUnitExecutor` in `verdict/autodev_opencode.py` has the same surface autodev
already uses on `PatchExecutor` (`config`, `response_headers`, `execute_unit`) and adds
`restore()`. It does all the isolation work inside `execute_unit`. After it returns
`applied`, `_run_unit_once` runs `_touched_since` and `_verify` unchanged, so there is one
judge and today's rule (a failed verify does not retry) is kept. The alternative, verifying
inside the attempt tree and transferring on pass (research Option A), makes the attempt
tree's environment the judge, which lacks ignored files such as `.venv`. It was rejected in
the research and stays rejected.

### D2. The attempt tree is an unshared clone

Per attempt, in a new `0700` directory under the system temp directory (never inside the
repository):

1. `git clone --no-local --quiet -c core.hooksPath=/dev/null <repo> <tree>`.
2. `git -C <tree> remote remove origin`.

The tree is removed in a `finally` on every exit path: applied, rejected, error, worker
timeout, exception and cancellation. The seed contains the user's uncommitted work, so a
leftover tree would be a copy of it; after SIGKILL one can remain (Risks).

### D3. Seed from the current tree by file copy, and take the manifest

After the clone, Verdict overlays the user's dirty state by copying files, not by writing
git objects. The list comes from `git -C <repo> status --porcelain=v1 -z
--untracked-files=all` run in the user's repository (a read, as `_tree_state` already
does). For each listed path: a path that exists in the user's tree is copied into the
clone, a symlink as a symlink and never followed; a path that does not exist is removed
from the clone. Path components that are absolute, contain `..` or name `.git` are refused.

The unit's owned files are always copied from the user's tree, whether or not git lists them
as dirty. A checkout can differ from the working-tree bytes (an `eol` attribute, a clean or
smudge filter), and the transfer check in D5 compares the user's current bytes with M0, so M0
must hold the user's bytes for every path that could be transferred. An owned file the user
does not have is simply absent from the clone.

The seed fails closed, with a named reason, before any worker starts, when:

- the repository has no HEAD commit;
- it contains a submodule (a gitlink entry) or a nested repository;
- the seed exceeds the file-count or byte limit (Open point 3);
- `git clone` or a copy fails.

The user's repository receives no git object, ref, config or hook from the seed.

Then Verdict walks the clone's working tree (the root `.git` excluded) and records the
**seed manifest M0**. The manifest has one entry per non-directory path: a kind (regular
file, symlink, other), the executable bit, and the SHA-256 of a file's bytes or a symlink's
target. Directories are not entries: git does not track them, a directory is implied by the
files under it, and an empty directory is not part of the tree's content. The walk never
follows a symlink, opens only regular files, and opens them without following links and
without blocking (see D4, "Reading the worker's tree"). M0 is computed by Verdict before the
worker starts.

### D4. The delta is a filesystem comparison; no git runs in the tree after launch

After the worker exits (OC1 reaps its process group before returning [U]), Verdict walks the
tree again to get **M1** and compares. It runs no git command in the attempt tree, so
anything the worker planted in the tree's `.git` is inert. This is also why the staged,
committed and untracked variants need no separate handling: a `git add`, a `git commit` or a
`git checkout` changes git's state but the delta only sees file contents. A worker that
reverts its own edit leaves no delta. A worker that switches branches shows as changed paths
and is rejected by the ownership check.

Classification, in order:

1. A changed path is any path whose kind, executable bit or hash differs between M0 and M1,
   including paths added and removed.
2. **Byproducts.** A path that is new (absent from M0) is a byproduct, and is dropped
   from the delta, when it is in the executor-byproduct set (D9) or when the user's
   ignore rules ignore it. The ignore rules are evaluated in the user's repository with
   `git check-ignore --no-index -z --stdin`, fed the new paths. A worker-edited
   `.gitignore` is not consulted: it is itself a changed path, subject to ownership. A
   path that existed at seed time is never a byproduct, whatever its name or whether the
   ignore rules match it.
3. **Ownership.** Every remaining changed path must satisfy `WorkUnit.owns` (the same
   normalisation `_verify` uses). Otherwise the attempt is `rejected`, naming the paths, and
   nothing is transferred.
4. Every remaining changed path must have a regular-file entry in M1 or no entry in M1 (a
   delete). A symlink, a device, a FIFO or any other kind is `rejected`. Ownership is
   decided on these leaf entries only, so creating an owned file `newdir/file.py` makes
   `newdir/file.py` the changed path and `newdir` is not one. Directory-for-file swaps need
   no separate rule: replacing the directory `a` that held an owned `a/b.py` by a file `a`
   shows as a delete of `a/b.py` and a new unowned path `a`, and is rejected by rule 3.
5. An empty delta is `rejected` with the reason "worker made no change to the owned files".

**Reading the worker's tree.** The clone is worker-controlled, so no step may follow a
link or block on a special file. The walk uses `lstat` and `scandir` without following
symlinks. Each regular file is opened with `O_NOFOLLOW | O_NONBLOCK`, checked with `fstat`
to be a regular file after it is open, and read in bounded chunks up to the per-file limit
(Open point 3). A path that is not a regular file at open time is recorded with its real
kind and never opened again. If an open fails because the path changed kind during the
walk, the attempt is `error` ("attempt tree changed during validation").

**Read once.** The delta's bytes are read once into memory for each transferred path, from
an open descriptor, and hashed; that hash must equal the M1 entry. A mismatch is the same
`error`. Nothing is read from the clone after that, and what is transferred is exactly the
bytes that were hashed. This narrows, but does not close, the window in which a descendant
that left the process group (for example through `setsid`) keeps writing: it can still
change the clone while the walk runs, and the walk fails closed on what it can detect. A
descendant that outlives the attempt and writes elsewhere is the shell risk ADR-053
Decision 4 accepts.

### D5. Transfer is all-or-nothing and never overwrites what changed since the seed

The user's tree is a place that other processes, and the user, can change while the transfer
runs, so the transfer is built on directory descriptors and re-checks at the moment of each
write. It assumes a POSIX host (`os.open` with `dir_fd`; the executor refuses to run where
`os.supports_dir_fd` lacks `open`, `stat`, `mkdir`, `rename` or `unlink`).

**Traversal.** Each delta path is resolved component by component from a descriptor opened on
the repository root, every component opened with `O_DIRECTORY | O_NOFOLLOW` relative to its
parent descriptor. A component that is a symlink, or is not a directory, is a refusal. A
missing parent directory is created with `mkdir` relative to its parent descriptor and
recorded in the journal. No step uses a path string that could be re-resolved.

**Fingerprint.** For each delta path the executor compares what the user's tree holds now
with what M0 recorded for it: kind, executable bit and SHA-256 of the bytes, or absence when
M0 has no entry. The user's file is opened relative to its parent descriptor with
`O_NOFOLLOW | O_NONBLOCK` and checked by `fstat` before it is read. Any difference means the
user's tree changed after the seed, and the attempt is `rejected` with that reason. The
content is also checked against the per-file transfer limit (Open point 3).

**Write.** The check runs for every path before any write, and again for that path immediately
before its own write. The executor writes a temporary file in the target directory by
descriptor, sets the executable bit from M1, and `rename`s it over the target relative to the
parent descriptor. A file the worker deleted is `unlink`ed the same way. Before each write it
records a journal entry: the path, whether it existed, its prior bytes, kind and executable
bit, the fingerprint it wrote, and any directories it created. If any write fails, the
entries written so far are undone by the restore procedure and the attempt is `error`.

**What this does not guarantee.** POSIX has no compare-and-swap for a file's content. A user
or tool that writes an owned file in the instant between the last check and the `rename` is
overwritten. The design narrows that window to the code between two system calls and does not
claim to close it. Autodev assumes it is the only writer to a unit's owned files while that
unit runs, and nothing in this change detects a violation of that assumption (Open point 5).

### D6. Restore after a failed verification

`restore()` undoes the last transfer from the journal, in reverse order, with the same
descriptor-relative, no-follow traversal and at-the-moment re-check as D5. For each path it
first checks that the user's tree still holds exactly what the transfer wrote: the same
kind, executable bit and SHA-256 (or still no file, for a delete). If it does not, the
executor leaves the path alone and reports it as not restored, with the reason, because the
user or another tool changed it, a `chmod` included. It never overwrites content it did not
write. It removes the directories it created if they are empty. It is idempotent, and the
journal is replaced by the next `execute_unit`.

Restore covers only the paths the transfer wrote. If the unit's verification command itself
edits a transferred file, restore leaves that file alone and names it. If the command creates
other files (a cache, a build output) they are not this executor's to remove and stay, exactly
as they do after a failed verification on the default path today. So "byte-identical after a
failed verification" holds for a verification command that does not write to the repository;
for one that does, the outcome reason lists the owned paths that were restored and the ones
that were not.

`_run_unit_once` calls `restore()` after `_verify` returns false for an `applied` attempt,
only for an executor that has a `restore` method. That is a rollback capability and nothing
more: it grants no transport authority (D7). It appends the result to the outcome reason
("restored N owned file(s)" or "NOT restored: <paths>: changed since the transfer"). The
outcome's `changed_files` stays the attempt's delta. `PatchExecutor` has no `restore`, so
its path is unchanged.

A crash between transfer and verification leaves the validated owned-file edits on disk.
That is the residue `PatchExecutor` leaves today after a failed verify, and it is never an
out-of-bounds edit.

### D7. `run_autodev` wiring

Every use of the executor in `run_autodev`, `_run_unit_once`, `_executor_lineage` and
`_record_executor_attempt` [main], and what the isolated executor provides:

| Use | Isolated executor |
|---|---|
| `executor.config.model`: model equality with the decision, retry-ladder filter, receipt `executor_model`, lineage | The selected route id. The model-identity check is kept. |
| `executor.config.base_url`: equality with the decision's gateway at injection | **Skipped** for an executor marked `isolated`. Its transport is serve, not the gateway the route was probed on. Gateway authority for this path is serve's own pinned admission [U]. |
| `executor.config.temperature`, `max_tokens`: `_executor_lineage` | `None`. OpenCode owns sampling. Unknown is null, not 0. If the attempt-record writer rejects a null, `_executor_lineage` omits the keys for this executor (task 4.2). |
| `executor.response_headers`: `served_headers` in the receipt and the `x-omniroute-model` identity check | `{}`. The identity check is therefore a no-op: **this change attests no served identity**. The receipt shows the requested route. What would attest it is serve's routing receipt, read by OC2b's live check. |
| `executor.execute_unit(unit)` | Returns a `PatchAttempt` (below). |
| `replace(executor.config, ...)` in the default retry factory | Never reached: retries are refused. |

`run_autodev`'s `executor` parameter becomes a `UnitExecutor` protocol (`config`,
`response_headers`, `execute_unit`). A `PatchExecutor` satisfies it unchanged. Two separate,
explicit markers sit on top of it, and neither implies the other:

- `isolated: Literal[True]`, set only by `OpenCodeUnitExecutor`: the executor's transport is
  serve through an attempt clone. The gateway base-URL skip and the retry refusal key on this
  marker and on nothing else, so a later executor that merely implements `restore()` over a
  gateway transport still goes through the existing gateway equality check.
- a `restore()` method: a rollback capability. `_run_unit_once` calls it after a failed
  `_verify`; it grants no transport authority.

**Retries are refused, not just omitted.** If the executor is marked `isolated` and `retry_routes` or
`retry_executor_factory` is non-empty, `run_autodev` raises `AutodevError` before the planner
call. Otherwise the default factory would build a `PatchExecutor` for it, which is defect 3.
The CLI passes `retry_routes=()` with the flag.

**`PatchAttempt` mapping.** `applied`: the delta was validated and transferred;
`changed_files` is the delta; `usage` comes from the terminal's token counts, with
`reported=False` when they are null; `resolved_model` is `None`. `rejected`: ownership,
kind, limit, empty-delta or changed-since-seed refusals; the user's tree is untouched.
`error`: the terminal was not ok (timeout, stall, serve refusal, exit status), the seed
failed, or a transfer failed after rollback. A not-ok terminal transfers nothing even if the
clone holds edits. Reasons come from the check that refused, or from the terminal's
already-redacted error text [U], never a default.

`execute_unit` drives OpenCode with `asyncio.run`, because `run_autodev` is synchronous and
has no running loop. If a loop is running, `asyncio.run` raises and the executor returns
`error` with the tree removed.

### D8. The flag and the readiness check

`verdict autodev --worker-harness opencode` (Open point 1). Absent, nothing changes. With
`--dry-run` the flag has no effect, since no worker runs (Open point 4).

With the flag, `cmd_autodev` runs a readiness check first, before `prepare_live_autodev`
and `prepare_live_autodev_selected`, both of which send probes, and before any planner call:

1. **opencode.** Resolve the binary with OC1's resolver [U] (`VERDICT_OPENCODE_BIN`, then
   `~/.opencode/bin/opencode`, then `PATH`) and run `<bin> --version` with a short timeout
   and the process group reaped on every path. Missing or not runnable is a failure.
2. **serve.** `GET <serve>/health` answers 200 with `"engine": "verdict"`. `/health` needs no
   credential [main]. Anything else is a failure.

A failure prints one message naming the failed check and the remedy and exits 1. It reports
through `_report_autodev_failure` extended with a label argument, because that function today
hard-codes the label "decomposition failed", which would misname a readiness failure; the
default label is unchanged for its existing callers. `--json` gets the same single error
object. It makes no probe, no planner call and no clone. There is no fallback. A version
different from the pinned one does not fail the check; the output names it, as `verdict
doctor` does (Open point 2).

What the check does not prove: that serve will admit the route autodev picked, or that the
worker credential is valid. Serve's admission is separate from autodev's probe-based
decision, so serve can still refuse a route autodev admitted [U]. That is an `error` attempt
for the unit; the repository is untouched and, with retries disabled, the unit stays failed.

### D9. The credential and the context file are seams, not dependencies

- **Credential.** `OpenCodeUnitExecutor` receives an already-built OpenCode executor (an
  object with `async run(prompt, *, route_id, cwd, timeout_seconds)`). It never reads,
  stores, logs or records the worker credential. One function in `autodev_opencode.py`
  builds that executor for the CLI. When #1313 swaps the credential for a signed per-attempt
  assignment, that function and the executor's own construction change; the unit executor
  does not. The executor's text that reaches a reason has already passed OC1's redaction
  [U]; the unit executor passes it through unchanged and a test echoes a secret through a
  fake terminal to prove nothing adds it back.
- **Context file.** If the OpenCode executor writes `.verdict-context.md` into `cwd` [U], it
  lands in the clone, never in the user's tree, and D4's byproduct rule keeps it out of the
  delta. The byproduct set is a constant holding that name. If #1313 removes the file the
  constant is dead and nothing breaks. The unit executor does not write, read or require the
  file; it passes the task as the prompt. A path of that name that existed at seed time is
  an ordinary path under D4 rule 2.

### Prompt

`build_opencode_prompt(unit)` states the objective, the owned files, the verification command
and the compiled context pack (`unit.context`, compiled once per unit by `run_autodev`), and
tells the worker to edit only the owned files in the current directory, run the command, and
not commit or push. It does not inline file contents (the worker can read them) and
contains no credential.

## What "byte-identical" means

Tests and the spec use this definition for "leaves the user's repository as it was":

1. every file under the working tree outside `.git`: path, kind, executable bit, bytes;
2. the output of `git status --porcelain=v1 -z --untracked-files=all`;
3. `HEAD` and the output of `git for-each-ref`;
4. the bytes of `.git/config`, `.git/info/exclude` and every file under `.git/hooks`.

`.git/index` is excluded because git read commands may refresh its stat cache. In the
scratch repository it did not change, but that is not a guarantee. The temp directory,
`~/.verdict` and the attempt-ledger store are outside the repository and not covered.

## Known gaps until OC2b

- Attempt-ledger rows for OpenCode units keep the hard-coded label
  `verdict-autodev-executor` [main]. The `--worker-harness` help text and
  `docs/CLI_REFERENCE.md` say so. The receipt shows the requested route in `model`, with
  `served_model` null and `served_headers` empty.
- A failed OpenCode unit is not retried on any route.
- `--worker-harness opencode` is not the default.

## Risks / Trade-offs

- **Weaker worker test loop.** The clone has no ignored files (`.venv`, caches, build
  output). The worker's own test runs may fail or be slow for that reason. The judge is
  unaffected because `_verify` runs in the user's repository. This is unmeasured and no fix
  is designed here; OC2b's live run is where it shows up.
- **Seed and manifest cost** grow with repository size (a clone, a copy and two hash walks
  per attempt). Limits make an oversized seed a named refusal rather than a hang.
- **A shell is still a shell.** A worker can write to an absolute path in the user's
  repository (ADR-053 Decision 4, accepted by the operator). "Byte-identical" covers edits
  made in the attempt tree, not that. Nothing here detects it (Open point 5).
- **Residue.** After SIGKILL between transfer and verify the owned-file edits stay (same as
  `PatchExecutor` after a failed verify), and an attempt directory holding a copy of the
  user's tree can remain in the temp directory (`0700`, not swept in OC2a, Open point 6).
- **Serve and autodev admit separately.** A route autodev probed can be refused by serve; the
  unit fails with serve's reason and, with retries disabled, is not retried (OC2b).
- **Version drift.** OpenCode's config and flags are OC1's concern. A version other than
  the pinned one is advisory, as for doctor; Verdict-side isolation and ownership do not
  depend on OpenCode honouring any config.

## Open points

None blocks the build. Each has a recommended default that the tasks implement.

| # | Question | Recommended default |
|---|---|---|
| 1 | Flag name. | `--worker-harness opencode`, with `opencode` the only choice. `--executor` would match orchestrate but reads like the existing `--executor-model`, which names a route here. |
| 2 | OpenCode version different from the pinned one. | Advisory, as in doctor. Block only if the operator wants autodev stricter than doctor. |
| 3 | Limits: seed file count and bytes, transferred file bytes. | Named constants chosen in the build (proposed: 50,000 files, 512 MiB, 1 MiB per transferred file), each exceeded limit a named refusal before the worker runs. |
| 4 | `--dry-run` with the flag. | Accepted and ignored, no readiness check. |
| 5 | Detect writes to the user's tree during an attempt (a whole-tree fingerprint before and after). | Not in OC2a: it only detects what ADR-053 Decision 4 already accepts, and a person editing during a run would fail the unit. |
| 6 | Sweep stale attempt directories. | Not in OC2a. |

## Migration / rollback

No data migration, no schema change, no new dependency. Without the flag behaviour is
identical. Roll back by removing the flag, `verdict/autodev_opencode.py` and the protocol
change; `PatchExecutor` never depended on them.

## Security / trust implications

- The worker's tree is a separate git directory. Hooks, config and excludes it writes stay
  in it, and Verdict never executes git there after launch.
- Only paths the unit owns, validated by the existing `WorkUnit` normalisation, are copied
  back, as regular files, through descriptor-relative no-follow traversal, over content and
  mode that have not changed since the seed (checked again at each write).
- The worker's output, the clone's contents and `.gitignore` edits are untrusted data.
- No credential is added to code, logs, events or receipts; the unit executor never holds
  one. No new dependency.

## Routing / context / memory implications

Route choice is unchanged: `ExecutionPathDecision` still selects the route and `run_autodev`
asserts the executor's model equals it. The context pack is compiled once per unit and passed
as part of the prompt. Nothing is written to memory.

## ADR impact

ADR-053 Decision 3 ("PatchExecutor is kept for mechanical units") does not match the code,
where the mechanical tier is ruff and `PatchExecutor` is the model tier. Correcting it is
OC2b's. ADR-053 Decision 4 (shell risk) is unchanged and this design leans on it. No new ADR.
