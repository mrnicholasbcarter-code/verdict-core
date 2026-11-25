# Design

Tags: **[main]** read at main `c51cd994` (`_run_unit_once`, `_touched_since`, `_verify`, `_run`,
`_try_mechanical` and the decomposer re-read at `2d83e6b0`). **[U]** read on the unmerged OC1 branch at
`f684a7df`, re-read by task 1.1. **[exp]** observed in a scratch repository, git 2.56.1. Behaviour is
in the spec delta; descriptor mechanics, seams and test names are in tasks.md. This file is the why.

## Context

`_run_unit_once` runs each work unit in the user's own tree [main]. Units own disjoint files
(decomposer). It snapshots the tree, tries the mechanical tier (`ruff check --fix` on the owned files,
which can leave a partial fix when it declines), calls `execute_unit` (`PatchExecutor`: one gateway
call applied by `git apply`), then runs `_touched_since` and `_verify` (a path outside `owned_files`
fails, then the unit's command runs in the user's repo). `PatchExecutor` cannot stage, commit or
half-edit the tree; an agent editing in place can. OpenCode with `cwd=<user repo>` is the source of
defects 1 and 2 of #1301. This change adds an executor that never lets the worker touch the user's
tree; `_run_unit_once` and `_verify` stay the single judge.

## Goals / Non-Goals

**Goals.** Nothing a worker does in its tree reaches the user's repository except validated owned-file
content. Failed, out-of-bounds or refused attempts leave it byte-identical. Every exit after a transfer
other than a verified success restores the owned files; a process crash leaves them old-or-new and
git-recoverable. Opt-in, one-message fail-closed, no fallback.

**Non-Goals.** OC2b's retries, lineage, default selection and live run. Sandboxing the worker's shell
(ADR-053 Decision 4). `packet-execute`. Isolating the mechanical tier. Crash-durable recovery and
power-loss safety (D5, D7). Changing the default path or any admission rule.

**Settled by the operator (2026-11-24, #1301):** the tree is a separate clone, not a worktree sharing
the user's `.git` (D2, D3); opt-in flag first, fail closed, no fallback (D9); `packet-execute` is out;
the story is split, this is OC2a with retries off (D8). Everything else is this design's proposal.
**Divergences from the research (Option B):** the delta is computed from file contents, not git (D4);
the gateway check skips only the base-URL equality (D8); credential and lineage stay in OC2b (D10).

## Verified git behaviour [exp]

- `git clone --no-local` has no alternates or hardlinked objects and none of the user's hook, local
  config or `info/exclude`; its `origin` is the user's path (removed); its tree is HEAD, so ignored
  files are absent and the dirty state must be overlaid.
- A hook or `core.fsmonitor` planted in the clone's `.git` ran under host git; `-c core.hooksPath=/dev/null
  --no-verify` did not run the hook. `git status` and `check-ignore` left `.git/index` bytes unchanged.
- `git status --porcelain=v1 -z --untracked-files=all` lists modified, deleted, staged, renamed and
  untracked paths, never ignored ones; a rename is two records, destination first (`RM new.py\0old.py\0`).

## Decisions

### D1. A separate executor; autodev stays the judge

`OpenCodeUnitExecutor` (`verdict/autodev_opencode.py`) offers what autodev uses on `PatchExecutor`
(`config`, `response_headers`, `execute_unit`) plus `restore()` and `preflight()`. After `applied`,
`_run_unit_once` runs `_touched_since` and `_verify` unchanged. Verifying inside the clone (research
Option A) would make an environment without `.venv` the judge; it stays rejected.

### D2. The attempt tree is an unshared clone

In a new `0700` temp directory: `git clone --no-local -c core.hooksPath=/dev/null`, then `remote remove
origin`. It is removed on every exit path, and before the transfer (D7). After SIGKILL one can remain.

### D3. Seed by file copy, and take manifest M0

The user's dirty state is overlaid by copying files, never by writing git objects, from `git status
--porcelain=v1 -z --untracked-files=all` in the user's repository. A record with `R` or `C` is followed
by a source record: both are consumed, the destination's current working-tree bytes are copied, and the
source is removed from the clone when the user's tree no longer has it. Symlinks are copied as symlinks,
never followed. Owned files are always copied from the user's tree, dirty or not: checkout bytes can
differ (an `eol` attribute, a filter) and M0 must hold the user's bytes for every path that could be
transferred. The seed fails closed with a named reason, before any worker, on no HEAD, a submodule or
nested repository, an exceeded limit (Open point 3) or a failed copy. M0 records each non-directory
path's kind, executable bit and SHA-256.

### D4. The delta is a filesystem comparison; no git runs in the tree after launch

A worker-planted `core.fsmonitor` or hook runs under host git [exp], so after the worker exits Verdict
walks the tree again (M1) and compares with M0: a path is changed when its kind, executable bit or hash
differs, added and removed paths included. Staging, committing and checkout change only git state, so
they do not matter; a reverted edit leaves no delta. In order:

1. A new path is a byproduct, dropped, if it is in the executor-byproduct set (D10) or ignored by the
   user's rules (`git check-ignore --no-index -z --stdin` in the user's repository). A worker-edited
   `.gitignore` is not consulted; a path in M0 is never a byproduct.
2. Each remaining path must satisfy `WorkUnit.owns`, else `rejected`. Ownership is decided on leaf
   entries only (`newdir/file.py`, not `newdir`).
3. Each must be a regular file or a deletion; a symlink, device or FIFO is `rejected`.
4. An empty delta is `rejected`.

The tree is worker-controlled, so the walk and byte reads are descriptor-relative, follow no link and
block on no special file (task 2.2). A path that changes kind between the metadata read and the open is
`error`, never `rejected`; each delta file is read once and hashed against M1. A descendant that
outlives the attempt and writes elsewhere is the shell risk ADR-053 Decision 4 accepts.

### D5. An owned file must be recoverable by git, or absent

The transfer's undo information lives in memory (D6), so a process that dies after a rename cannot use
it, and the worker's replacement of an owned file need not contain what the user had there. An on-disk
journal (prior bytes and a directory list written before any write, kept until verification, with a
recovery entry point and a rule for a crash after a passing verify) would fix that, but it is a feature
of its own. This change removes the case instead: `preflight(unit)` reads the user's repository (`git
status -z`, `git ls-files -z -- <owned>`) and refuses a unit when an owned path is listed by status
(modified, staged, deleted, renamed, untracked) or exists untracked (ignored), or when either read
fails; `_tree_state` returns `{}` on failure, so it is not reused. Unowned dirty files are still seeded.

`_run_unit_once` calls `preflight` before the mechanical tier, because `ruff --fix` may leave a partial
edit in an owned file and a later check would refuse every ruff unit the mechanical tier could not
finish. It acts on the reason only if the mechanical tier declines and the model tier would run: the
unit fails with the path and "commit or stash it, or run without the flag", no worker starts, nothing is
written. Its outcome has tier `refused`, not `model`, because `run_autodev` writes an attempt-ledger row
and an unreported-usage entry for every `model` outcome and no model ran. A dirty owned file the
mechanical tier fixes is still verified mechanically.

**Behaviour change for the operator.** On the default path autodev applies a patch to an owned file
with uncommitted changes; with the flag it does not. This is the narrowing; the pull request asks.

### D6. Transfer is all-or-nothing and never overwrites what changed since the seed

The user's tree can change while the transfer runs, so it works on directory descriptors (POSIX only;
task 2.3) and re-checks at the moment of each write. The read-only phase resolves every path without
following a symlink and compares the user's current kind, mode and SHA-256 with M0; any difference is
`rejected` and nothing was created. The write phase creates directories one at a time, each journalled,
repeats the comparison and a device-and-inode check of the held directories before each write (a
renamed-away parent is refused), and writes through one helper shared with restore: a unique
`.verdict-transfer-*` file in the target directory, renamed over the target, unlinked by the same
descriptor on any failure before the rename (a failing unlink is named, never replaces the original
error). A failure after the first write rolls back through restore and is `error`; a partial transfer
is never `rejected`. POSIX has no compare-and-swap for content, so a write by the user between the last
check and the `rename` is overwritten; autodev assumes it is the only writer of the unit's owned files
(Open point 5).

### D7. After the transfer: restore on every failing exit, and what a crash leaves

On main `_touched_since` and `_verify` run outside any handler and `_run` catches only
`FileNotFoundError` and `TimeoutExpired`, so a `PermissionError` from the runner or a
`KeyboardInterrupt` escapes `_run_unit_once` after the owned files were replaced. The rule is therefore
not "restore after a false verify" but: for an executor with `restore`, every exit after `execute_unit`
was called other than a verified success calls `restore()` once (false result, any exception including
one from `execute_unit` after its transfer, `KeyboardInterrupt`). `restore()` is a no-op when nothing
was transferred. A false result gets the real outcome in the reason; an exception is re-raised
unchanged, and a failure inside `restore()` is caught and printed to standard error (Python 3.10 has no
`add_note`), never replacing it. A second interrupt during `restore()` is not covered. So that
`execute_unit` itself is covered, the transfer is its last fallible step and the clone is removed first.

Restore walks the journal in reverse with D6's traversal and write helper and leaves alone, and
reports, any path that is no longer what the transfer wrote (a user edit or `chmod`), so it never
overwrites content it did not write. It covers only paths the transfer wrote: a verification command
that edits or creates other files leaves them, as on main after a failed verify.

A crash (SIGKILL, SIGTERM's default action, an uncaught crash) runs no handler and the journal is lost;
D5 makes that harmless. A `rename` is atomic, so each owned path holds its complete old or new bytes;
the residue is created files and empty directories and at most one `.verdict-transfer-*` file. Every
pre-existing owned path was tracked and unmodified at the unit's start, so `git checkout -- <path>`
restores it. Nothing runs automatically after a crash; power loss is not claimed.

### D8. `run_autodev` wiring

`run_autodev`'s `executor` becomes a `UnitExecutor` protocol (`config`, `response_headers`,
`execute_unit`) that `PatchExecutor` satisfies, with three optional capabilities, none implying another:
`isolated` (only `OpenCodeUnitExecutor`; the base-URL skip and the retry refusal key on it alone),
`restore()` (D7) and `preflight(unit)` (D5). An executor with `restore()` over a gateway transport still
gets the gateway equality check. For the isolated executor `config.model` is the selected route and the
model-identity check stays; `config.base_url` equality is skipped because its transport is serve, whose
pinned admission is the authority [U]; sampling fields are `None` (`_executor_lineage` omits the keys if
the attempt record rejects null, task 4.2); `response_headers` is `{}`, so the served-identity check is a
no-op and **no served identity is attested**.

Retries are refused, not omitted: an `isolated` executor with `retry_routes` or a
`retry_executor_factory` makes `run_autodev` raise `AutodevError` before the planner call, because the
default factory would build a `PatchExecutor` (defect 3). The CLI passes `retry_routes=()`.

`PatchAttempt`: `applied` when validated and transferred (`changed_files` is the delta, `usage`
`reported=False` when tokens are null, `resolved_model=None`); `rejected` for ownership, kind, limit,
empty-delta and changed-since-seed refusals, tree untouched; `error` for a not-ok terminal (it transfers
nothing even if the clone holds edits), a failed seed, or a transfer that failed after rollback.
Reasons come from the check that refused or OC1's already-redacted error text [U]. `execute_unit` uses
`asyncio.run` and returns `error` inside a running loop.

### D9. The flag and the readiness check

`verdict autodev --worker-harness opencode` (Open point 1). With it, `cmd_autodev` first checks, before
`prepare_live_autodev`, `prepare_live_autodev_selected` (both probe) and any planner call, that the
opencode binary resolves (OC1's resolver [U]) and runs `--version`, and that `GET <serve>/health` is 200
with `"engine": "verdict"`. A failure prints one message and exits 1 through `_report_autodev_failure`
with a new label argument (it hard-codes "decomposition failed"; the default is unchanged), with one JSON
object under `--json`; nothing is probed, planned or cloned and there is no fallback. A different
OpenCode version is named, not failed (Open point 2). The check does not prove serve will admit the
route: a refusal is an `error` attempt that leaves the repository untouched.

### D10. The credential, the context file and the prompt are seams

The unit executor receives an already-built OpenCode executor (`async run(prompt, *, route_id, cwd,
timeout_seconds)`) and never reads, stores or records the credential; one function builds it for the
CLI, so #1313 changes only that. If the OpenCode executor writes `.verdict-context.md` into `cwd` [U] it
lands in the clone and D4's byproduct constant keeps it out of the delta; the unit executor neither
needs nor reads it. `build_opencode_prompt(unit)` carries the objective, owned files, command and
context pack, and tells the worker to edit only owned files and not commit or push.

## Risks / Trade-offs

- **Dirty owned files are refused** (D5): a user mid-edit on an owned file must commit or stash it to
  use the flag, a real loss against the default path.
- The clone has no ignored files (`.venv`, caches), so the worker's own test loop may fail or be slow;
  the judge is unaffected. Unmeasured; OC2b's live run shows it. Cost grows with repository size;
  limits make an oversized seed a named refusal.
- A shell is still a shell: a worker can write an absolute path in the user's repository (ADR-053
  Decision 4). A crash leaves D7's residue, and an attempt directory (`0700`, no sweep) can remain.
- Known gaps until OC2b: ledger rows for OpenCode units keep the label `verdict-autodev-executor`
  [main], the receipt shows the requested route with no served identity, and a failed unit is not
  retried. Serve and autodev admit separately, so serve can refuse a route autodev probed.

## Open points

None blocks the build; the tasks implement each default. (1) Flag name `--worker-harness opencode`
(`--executor` reads like `--executor-model`). (2) A version other than the pinned one is advisory.
(3) Limits as named constants (proposed 50,000 files, 512 MiB, 1 MiB per transferred file). (4)
`--dry-run` with the flag is ignored. (5) Detecting writes to the user's tree during an attempt, and (6)
sweeping stale attempt directories, are not in OC2a.

## Migration / rollback

No data migration, schema change or new dependency. Without the flag behaviour is identical. Roll back
by removing the flag, `verdict/autodev_opencode.py` and the protocol change.

## Security / trust implications

The worker's tree is a separate git directory and Verdict never runs git in it after launch. Only
owned paths validated by `WorkUnit` are copied back, as regular files, by descriptor-relative no-follow
traversal, over content unchanged since the seed. The worker's output, the clone and `.gitignore` edits
are untrusted data. No credential in code, logs, events or receipts; no new dependency.

## Routing / context / memory implications

Route choice is unchanged: `ExecutionPathDecision` selects the route and `run_autodev` asserts the
executor's model equals it. The context pack is compiled once per unit and sent in the prompt. Nothing
is written to memory.

## ADR impact

ADR-053 Decision 3 ("PatchExecutor is kept for mechanical units") does not match the code, where the
mechanical tier is ruff and `PatchExecutor` is the model tier; OC2b corrects it. Decision 4 is unchanged.
