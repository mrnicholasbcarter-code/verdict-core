# Spec Delta: autodev OpenCode isolated executor

"Byte-identical", for a user repository, means all four of: every file under the working
tree outside `.git` (path, kind, executable bit, bytes) and the set of directory paths there,
empty directories included; the output of `git status
--porcelain=v1 -z --untracked-files=all`; `HEAD` and the output of `git for-each-ref`; and
the bytes of `.git/config`, `.git/info/exclude` and every file under `.git/hooks`.

## ADDED Requirements

### Requirement: OpenCode SHALL run autodev units only behind an explicit opt-in flag

`verdict autodev` SHALL run a unit on OpenCode only when `--worker-harness opencode` is
given. Without the flag, autodev SHALL behave as before: the model tier is `PatchExecutor`,
no readiness check runs, no attempt tree is made and no OpenCode process starts. With
`--dry-run` the flag SHALL have no effect.

#### Scenario: Flag absent
- **WHEN** `verdict autodev --allow-live` runs without `--worker-harness`
- **THEN** units run on `PatchExecutor` exactly as before, no readiness check runs, and no OpenCode process or attempt tree exists

#### Scenario: Flag with dry run
- **WHEN** `verdict autodev --dry-run --worker-harness opencode` runs
- **THEN** it plans and prints the plan, runs no readiness check and starts no worker

### Requirement: With the flag, autodev SHALL stop with one message when opencode or serve is unavailable

With `--worker-harness opencode` and live consent, autodev SHALL check, before any probe,
planner call, attempt tree or model call, that the opencode binary resolves and runs, and
that `verdict serve` answers `GET /health` with status 200 and `"engine": "verdict"`. When a
check fails it SHALL print one message naming the failed check and the remedy, exit with
status 1, and SHALL NOT fall back to `PatchExecutor`. A version of OpenCode different from
the pinned one SHALL NOT fail the check and SHALL be named in the output.

#### Scenario: opencode binary missing
- **WHEN** the binary cannot be resolved or exits non-zero on `--version`
- **THEN** autodev exits 1 with one message naming the binary check, and the route probe, the decomposer, `run_autodev` and `PatchExecutor` are never called

#### Scenario: serve not answering
- **WHEN** the connection to serve is refused, times out, returns a non-200, or returns a body whose `engine` is not `verdict`
- **THEN** autodev exits 1 with one message naming the serve check, with no upstream request beyond that health request

#### Scenario: One message in JSON mode
- **WHEN** a readiness check fails and `--json` is given
- **THEN** exactly one JSON error object is printed, its label names the readiness check and not a decomposition failure, and the exit status is 1

#### Scenario: Version differs from the pinned version
- **WHEN** the installed OpenCode reports a version other than the pinned one and both checks otherwise pass
- **THEN** the run continues and the output names the installed and pinned versions

### Requirement: Each OpenCode attempt SHALL run in an unshared clone seeded from the current tree

For each attempt the executor SHALL create a new directory outside the repository and make
an unshared git clone of the user's repository there, with no alternates, no hardlinked
objects and no remote. It SHALL overlay the user's modified, deleted, staged, renamed and
untracked non-ignored files onto the clone by copying files, and SHALL NOT write any git
object, ref, config or hook into the user's repository. A rename or copy reported by
`git status -z` is two records, destination then source; the executor SHALL consume both,
copy the destination's current working-tree bytes, and remove the source from the clone when
the user's tree no longer has it. It SHALL record a manifest of the seeded tree before the
worker starts. The attempt directory SHALL be removed on every exit path.

#### Scenario: Clone shares nothing with the repository
- **WHEN** an attempt tree is seeded from a repository that has its own hook, local config value and `info/exclude` entry
- **THEN** the tree has no `objects/info/alternates`, no object file with a link count above one and no remote, and contains none of the user's hook, config value or exclude entry

#### Scenario: Dirty state is carried over
- **WHEN** the user's tree has a modified tracked file, a deleted tracked file, a staged file, an untracked file and an ignored file
- **THEN** the attempt tree holds the modified, staged and untracked contents, lacks the deleted file and lacks the ignored file, and the user's repository is byte-identical after seeding

#### Scenario: Staged rename
- **WHEN** the user ran `git mv old.py new.py` and nothing else
- **THEN** the attempt tree holds `new.py` with the user's bytes and no `old.py`, no record is misread as a path, and the user's repository is byte-identical after seeding

#### Scenario: Staged rename whose destination was then modified
- **WHEN** the user ran `git mv old.py new.py` and then edited `new.py`
- **THEN** the attempt tree holds `new.py` with the edited working-tree bytes and no `old.py`, and the user's repository is byte-identical after seeding

#### Scenario: Owned files hold working-tree bytes
- **WHEN** an owned file is unmodified in the user's tree but its checkout bytes differ from the working-tree bytes (for example through an `eol` attribute)
- **THEN** the attempt tree holds the user's working-tree bytes for that file, and the transfer check compares against those bytes

#### Scenario: Seed refused before any worker starts
- **WHEN** the repository has no HEAD commit, contains a submodule or a nested repository, or exceeds the seed limit
- **THEN** the attempt is `error` with a reason naming which, no worker starts and no attempt directory remains

#### Scenario: Attempt tree removed on every exit path
- **WHEN** an attempt ends applied, rejected, with a failed or timed-out worker, with an exception, or by cancellation
- **THEN** the attempt directory no longer exists

### Requirement: An owned file SHALL be tracked and unmodified, or absent, before a worker starts

The executor's transfer keeps its undo information in memory, so a process that dies after a
file was replaced cannot undo it. To make that loss harmless, the executor SHALL refuse a unit
when any of its owned paths is reported by `git status` (modified, staged, deleted, renamed or
untracked) or exists and is not tracked (an ignored file), or when either of the two git reads
(`git status` and `git ls-files`) fails. An owned path that does not exist and that git does not
report SHALL NOT be refused, and no other file is checked. The reads are made at the
start of the unit, before the mechanical tier; a refusal is acted on only if the mechanical tier
then declines the unit, so an edit the mechanical tier makes to an owned file does not cause
one. A refused unit SHALL fail with a reason that names the path and the remedy, start no
worker, create no attempt tree, write nothing to the repository and be recorded as no model
attempt (no attempt-ledger row, no unreported-usage entry). The default path
(`PatchExecutor`) SHALL keep running on a modified owned file.

#### Scenario: Tracked and unmodified owned file
- **WHEN** every owned file is tracked and unmodified
- **THEN** the unit is not refused and the worker starts

#### Scenario: Modified owned file refused
- **WHEN** an owned file has an uncommitted change in the working tree
- **THEN** the unit fails with a reason naming the file and saying to commit or stash it, no worker starts, no attempt record is written, and the repository is byte-identical

#### Scenario: Staged or deleted owned file refused
- **WHEN** an owned file has a staged change and an unchanged working tree, or was deleted in the working tree
- **THEN** the unit is refused as above

#### Scenario: Untracked or ignored owned file refused
- **WHEN** an owned file exists and is untracked, or exists and is ignored
- **THEN** the unit is refused as above

#### Scenario: Absent owned file accepted
- **WHEN** an owned path does not exist and the unit is to create it
- **THEN** the unit is not refused

#### Scenario: Unowned dirty file does not refuse
- **WHEN** a file the unit does not own has uncommitted changes
- **THEN** the unit is not refused and the file is carried into the seed

#### Scenario: Git read fails closed
- **WHEN** `git status` or `git ls-files` exits non-zero
- **THEN** the unit is refused with the git failure as the reason, and is not treated as clean

#### Scenario: Mechanical edit does not refuse
- **WHEN** the mechanical tier edits an owned file with `ruff --fix` and then declines the unit
- **THEN** the unit is not refused and the worker starts on the edited file

#### Scenario: Mechanical tier fixes a modified owned file
- **WHEN** an owned file has an uncommitted change and the mechanical tier fixes and verifies the unit
- **THEN** the unit is verified by the mechanical tier as it is today and the refusal is never acted on

#### Scenario: Default path unaffected
- **WHEN** `PatchExecutor` runs a unit whose owned file has an uncommitted change
- **THEN** the unit runs as today, with no refusal

### Requirement: Verdict SHALL NOT run git in an attempt tree after the worker has started

After launching the worker, the executor SHALL determine what changed only by comparing file
contents against the seed manifest, SHALL read the user's ignore rules from the user's
repository and not from the attempt tree, and SHALL NOT execute any git command whose
configuration or hooks the worker could have written.

#### Scenario: Planted git configuration is inert
- **WHEN** a worker writes `core.fsmonitor` into the attempt tree's `.git/config` and a `pre-commit` hook into its `.git/hooks`
- **THEN** neither command runs during validation or transfer, and the marker file each would create does not exist

#### Scenario: Git state does not change the result
- **WHEN** a worker edits an owned file and then stages it, commits it, or reverts it
- **THEN** the delta contains the owned file for the first two and is empty for the third, decided only from file contents

### Requirement: Only owned-file changes SHALL be transferred, and any other change SHALL leave the user's repository untouched

The executor SHALL transfer an attempt only when every changed path is owned by the unit
(`WorkUnit.owns`), is a regular file or a deletion, and the change is not empty. Otherwise
the attempt SHALL be `rejected` with a reason naming the offending paths, and the user's
repository SHALL be byte-identical to before the attempt. An attempt whose worker did not end
successfully SHALL transfer nothing even if the attempt tree holds edits.

Ownership SHALL be decided on file and link entries only, never on directories. The executor
SHALL read the attempt tree without following links and without opening anything that is not a
regular file, resolving every directory component relative to a held descriptor, so that
swapping an ancestor directory for a symlink cannot redirect a read.

#### Scenario: Out-of-bounds edits are rejected
- **WHEN** a worker edits an unowned file by a plain edit, by staging it, by committing it, by creating an unowned new file, or by swapping a symlink into an owned path
- **THEN** the attempt is `rejected` naming the path and the user's repository is byte-identical

#### Scenario: A new owned file in a new directory
- **WHEN** a worker creates the owned file `newdir/file.py` and nothing else
- **THEN** the changed path is `newdir/file.py` alone, the attempt is `applied`, and `newdir` is not reported as out of bounds

#### Scenario: Directory and file swapped
- **WHEN** a worker replaces the directory holding an owned file with a plain file of the same name as the directory
- **THEN** the attempt is `rejected` for the new unowned path

#### Scenario: Ancestor directory swapped during validation
- **WHEN** a surviving worker process replaces a directory of the attempt tree by a symlink to a path outside the tree while the tree is being read
- **THEN** nothing outside the tree is read, and the attempt is `error` with the reason that the attempt tree changed during validation

#### Scenario: A regular file replaced by a FIFO during validation
- **WHEN** the attempt tree's metadata reports a regular file and the worker's surviving process replaces it with a FIFO before the open
- **THEN** the executor does not block, and the attempt is `error` with the reason that the attempt tree changed during validation, not `rejected`

#### Scenario: Special files in the attempt tree
- **WHEN** a worker leaves a FIFO, or a symlink pointing outside the tree, where a regular file was
- **THEN** it is recorded by the kind its metadata reports, never opened or followed, and the attempt is `rejected` if the path is in the delta

#### Scenario: A valid owned repair is applied
- **WHEN** a worker changes only an owned file
- **THEN** the attempt is `applied`, `changed_files` lists exactly the owned paths, and the user's tree holds the new content

#### Scenario: Worker makes no change
- **WHEN** the worker ends successfully without changing any owned file
- **THEN** the attempt is `rejected` with the reason "worker made no change to the owned files"

#### Scenario: Worker failed
- **WHEN** the worker times out, stalls, exits non-zero, or serve refuses the route, and the attempt tree holds edits
- **THEN** the attempt is `error` with the worker's redacted reason and the user's repository is byte-identical

### Requirement: Worker byproducts SHALL NOT count as edits, and a path that existed at seed time SHALL NOT be a byproduct

A path absent from the seed manifest SHALL be dropped from the delta, and never transferred,
when it is in the executor-byproduct set or is ignored by the user's own ignore rules. A path
present in the seed manifest SHALL always be subject to the ownership check. A `.gitignore`
written by the worker SHALL NOT be consulted. The executor SHALL NOT require any
executor-written context file and SHALL work when none is written.

#### Scenario: Context file created by the executor
- **WHEN** the OpenCode executor writes `.verdict-context.md` into the attempt tree and the worker repairs an owned file
- **THEN** the attempt is `applied`, the context file is absent from the user's repository, and the unit is not rejected for it

#### Scenario: No context file at all
- **WHEN** the OpenCode executor writes no context file
- **THEN** the same repair is `applied` with the same result

#### Scenario: A tracked file with the byproduct name
- **WHEN** `.verdict-context.md` is tracked in the user's repository and a worker edits it while the unit does not own it
- **THEN** the attempt is `rejected` for that path

#### Scenario: Ignored byproduct
- **WHEN** a worker's test run creates `.pytest_cache/` and `__pycache__/` files that the user's `.gitignore` ignores
- **THEN** they are not in the delta and are not transferred

#### Scenario: A worker cannot hide a path by editing .gitignore
- **WHEN** a worker adds a new unowned file and rewrites `.gitignore` in the attempt tree to ignore it
- **THEN** the attempt is `rejected` for the unowned file and for `.gitignore`

### Requirement: Transfer SHALL be all-or-nothing and SHALL NOT overwrite a file that changed after the seed

The executor SHALL resolve every transfer path component by component from a descriptor on
the repository root without following symlinks, and SHALL compare, for each path, the user's
current kind, executable bit and bytes with the seed manifest (or absence, when the manifest
has no entry), both before any write and again immediately before that path's write. If any
comparison differs the attempt SHALL be `rejected` with the reason and nothing SHALL be written
for a pre-write difference. The checks before the first write SHALL be read-only and SHALL
create nothing; directories SHALL be created only during the write phase, each recorded for
rollback as it is created. The second comparison SHALL also confirm that every held directory
descriptor is still the directory reachable at its name, by device and inode. A refusal found
by the read-only checks SHALL make the attempt `rejected`. A refusal or failure after the write
phase has begun SHALL roll back every path and directory already written or created, SHALL make
the attempt `error`, and its reason SHALL name the refused path and list any path the rollback
could not restore. Every file the executor writes, in the transfer and in restore, SHALL go
through one helper that creates a uniquely named temporary file in the target directory by
descriptor, sets its mode, renames it over the target, and unlinks the temporary file by the
same descriptor on every failure before the rename succeeds. An unlink that itself fails SHALL
be named in the reason and SHALL NOT replace the original error. The executor SHALL refuse to
run on a host whose descriptor-relative file operations it needs are unavailable.

#### Scenario: Owned file changed after the seed
- **WHEN** the user edits an owned file after the seed and before the transfer
- **THEN** the attempt is `rejected` with the reason that the file changed since the seed, and the user's edit is intact

#### Scenario: Only the mode changed
- **WHEN** the user changes only the executable bit of an owned file after the seed
- **THEN** the attempt is `rejected` for that file and the user's mode is intact

#### Scenario: Symlinked parent
- **WHEN** an owned path's parent directory in the user's tree is a symlink
- **THEN** the attempt is `rejected` and nothing is written through the link

#### Scenario: Parent swapped for a symlink during the transfer
- **WHEN** a parent directory is replaced by a symlink after the first comparison and before the write
- **THEN** nothing is written through the link

#### Scenario: Parent directory renamed away during the transfer
- **WHEN** a parent directory is renamed and a symlink or another directory is put at its old name after the first comparison and before the write
- **THEN** the second comparison finds the held descriptor is no longer the directory at that name, nothing is written into the displaced directory, and the attempt is `rejected` if no write had happened and `error` otherwise

#### Scenario: A refusal on a later path leaves no directory behind
- **WHEN** a transfer has two owned paths, the first in a directory that does not exist yet and the second changed by the user since the seed
- **THEN** the attempt is `rejected`, nothing is written, and the directory set of the user's tree is unchanged, with no new empty directory

#### Scenario: Late conflict after earlier paths were written
- **WHEN** a transfer of three owned files has written the first and the user edits the second before its write
- **THEN** the second comparison refuses that write, the first path is restored, the user's edit is intact, and the attempt is `error` naming the second path

#### Scenario: Rollback blocked by a later user edit
- **WHEN** a late conflict triggers rollback and the user has also changed an earlier written path
- **THEN** that path is left as the user left it and the `error` reason lists it as not restored

#### Scenario: Failure part-way
- **WHEN** a transfer of three owned files fails on the second
- **THEN** all three paths hold their prior content and the attempt is `error`

#### Scenario: Temporary file removed after a failed write
- **WHEN** the temporary-file write, the mode change or the rename fails (for example with `ENOSPC`) for an existing owned file, or for a new owned file in a directory the transfer created
- **THEN** the attempt is `error`, and the complete user tree (every path with kind, mode and bytes) and the set of directories equal what they were before the attempt, with no temporary file and no directory left behind

#### Scenario: Temporary file removal fails too
- **WHEN** a failed write is followed by a failing unlink of its temporary file
- **THEN** the reason names the original failure first and the leftover temporary file second

### Requirement: Every exit after a transfer other than a verified success SHALL restore the owned files, and restore SHALL NOT overwrite content it did not write

For an executor that has a `restore` method, every exit from `_run_unit_once` after
`execute_unit` was called, other than a verified success, SHALL call `restore()` exactly once
and SHALL NOT skip it for the exit being an exception. The exits are a false `_verify` result;
an exception from `execute_unit`, including one raised after its transfer; an exception from
`_touched_since` or `_verify`, including any error the command runner raises that `_run` does
not catch (for example `PermissionError`); and `KeyboardInterrupt`. `restore()` is a no-op
when nothing was transferred. For a false result `_run_unit_once` SHALL record the real
restore result in the outcome reason. For an exception it SHALL re-raise the original
exception unchanged after restore; a failure inside `restore()` SHALL be caught and reported
on standard error and SHALL NOT replace or hide the original exception. A second interrupt
during `restore()` is not covered. The executor SHALL make the transfer the last step of
`execute_unit` that can fail: the attempt tree is removed before it. Restore SHALL return each
transferred path to its prior bytes, mode or absence, using the same no-follow traversal and
at-the-moment comparison as the transfer, remove directories it created when empty, and SHALL
leave alone and name any path whose kind, executable bit or bytes are no longer what the
transfer wrote. Restore SHALL cover only paths the transfer wrote. A verified unit SHALL NOT
be restored. The `PatchExecutor` path SHALL be unchanged.

#### Scenario: Verification fails after an applied attempt
- **WHEN** an owned-file change is transferred and the unit's verification command, which writes nothing to the repository, exits non-zero
- **THEN** the repository is byte-identical to before the attempt, a pre-existing dirty unowned file is untouched, and the reason says the owned files were restored

#### Scenario: Verification command writes to the repository
- **WHEN** the verification command rewrites a transferred file or creates a new file and then exits non-zero
- **THEN** restore leaves the rewritten file as it is and names it as not restored, the new file is not removed, and the reason lists which owned paths were and were not restored

#### Scenario: User edit after the transfer is kept
- **WHEN** the user changes a transferred file after the transfer and verification then fails
- **THEN** restore leaves that file as the user left it and the reason names it as not restored

#### Scenario: User mode change after the transfer is kept
- **WHEN** the user changes only the executable bit of a transferred file and verification then fails
- **THEN** restore leaves that mode as the user left it and the reason names the file as not restored

#### Scenario: Created files and directories are removed
- **WHEN** the transferred change created an owned file in a new directory and verification fails
- **THEN** the file and the directory restore created are gone

#### Scenario: Parent swapped between transfer and restore
- **WHEN** a transferred file's parent directory is replaced by a symlink after the transfer and restore runs
- **THEN** nothing is written through the link and the path is named as not restored

#### Scenario: Restore twice
- **WHEN** `restore()` is called a second time after a complete restore
- **THEN** it changes nothing and reports nothing as restored

#### Scenario: Verified unit is not restored
- **WHEN** verification passes
- **THEN** restore is not called and the change stays

#### Scenario: Verification raises
- **WHEN** an owned-file change is transferred and the command runner raises `PermissionError` during `_verify`
- **THEN** `restore()` is called once, the repository is byte-identical to before the attempt, and the `PermissionError` propagates unchanged

#### Scenario: Attribution raises
- **WHEN** `_touched_since` raises after a transfer
- **THEN** `restore()` is called once and the original exception propagates

#### Scenario: Interrupt after the transfer
- **WHEN** a `KeyboardInterrupt` arrives during `_verify` after a transfer
- **THEN** `restore()` is called once, the repository is byte-identical to before the attempt, and the `KeyboardInterrupt` propagates

#### Scenario: Executor raises after its transfer
- **WHEN** `execute_unit` raises after the transfer was written
- **THEN** `restore()` is called once and the owned files hold their prior content

#### Scenario: Restore fails while an exception propagates
- **WHEN** `_verify` raises and `restore()` then raises a different exception
- **THEN** the exception that propagates is the original, and the restore failure is on standard error

#### Scenario: Restore without a transfer
- **WHEN** the executor's attempt was rejected or errored, so nothing was transferred, and a later step raises
- **THEN** `restore()` changes nothing

#### Scenario: Restore write failure leaves no temporary file
- **WHEN** a restore write fails for one path
- **THEN** that path is named as not restored, no temporary file remains and the other paths are restored

### Requirement: After a process crash every owned path SHALL hold its old or its new bytes, and git SHALL be able to undo it

For a unit the executor accepted, a process that dies at any point from the first temporary
file to the end of verification (SIGKILL, SIGTERM or an uncaught crash; power loss is not
claimed) SHALL leave every owned path holding either its complete prior bytes or the
worker's complete bytes. The only other residue SHALL be files the transfer created, empty
directories it created and at most one temporary file named `.verdict-transfer-*`, all in the
owned files' own paths and directories. No other path SHALL change. Every pre-existing owned
path was tracked and unmodified when the unit started, so `git checkout -- <path>` restores
it. No automatic recovery runs after a crash.

#### Scenario: Kill after the temporary write
- **WHEN** the process is killed after the temporary file of the first path is written and before its rename
- **THEN** every owned path holds its prior bytes, at most one `.verdict-transfer-*` file remains, and `git status` lists nothing else

#### Scenario: Kill after the first of two renames
- **WHEN** the process is killed after the first of two owned files was renamed into place
- **THEN** the first holds the worker's bytes, the second its prior bytes, neither is torn, and `git checkout -- <owned paths>` followed by removing the created files and empty directories leaves the repository byte-identical to before the attempt

#### Scenario: Kill after the last rename
- **WHEN** the process is killed after the last rename and before verification
- **THEN** every owned path holds the worker's bytes and the same two steps leave the repository byte-identical to before the attempt

### Requirement: Retries SHALL be refused for the isolated executor, and PatchExecutor SHALL never run in its place

The isolated executor SHALL carry an explicit `isolated` marker that is separate from having a
`restore` method. When the executor is marked `isolated` and `retry_routes` or
`retry_executor_factory` is non-empty, `run_autodev` SHALL raise `AutodevError` before any planner call. The base-URL equality check for an injected executor SHALL be skipped only for an executor
marked `isolated`, and the model-identity check SHALL still apply to it. A failed unit on the
isolated executor SHALL NOT be retried on any executor and SHALL NOT cause `PatchExecutor`
to be constructed. With the flag the CLI SHALL pass no retry routes.

#### Scenario: Retry routes with the isolated executor
- **WHEN** `run_autodev` is given the isolated executor and one retry route
- **THEN** it raises `AutodevError` before the decomposer is called

#### Scenario: Restore capability alone grants nothing
- **WHEN** an executor has a `restore` method but is not marked `isolated` and its base URL differs from the decision's gateway
- **THEN** `run_autodev` refuses it through the existing gateway equality check, as it does today

#### Scenario: Failed unit is not retried
- **WHEN** a unit's attempt is rejected or errors on the isolated executor
- **THEN** the unit is reported unverified once, no second attempt runs, and `PatchExecutor` patched to raise is never constructed

### Requirement: Unknown values SHALL be recorded as unknown, and the unit executor SHALL NOT hold the worker credential

The isolated executor's `config` SHALL carry the selected route and `None` for sampling
parameters, its `response_headers` SHALL be empty, and `resolved_model` SHALL be `None`, so
no served identity is attested and no sampling value is invented. The unit executor SHALL
receive an already-built OpenCode executor and SHALL NOT read, store, log or record the
worker credential or add it to any reason, event or receipt.

#### Scenario: No served identity is claimed
- **WHEN** an isolated unit completes
- **THEN** its outcome has `served_model` null and the receipt's served headers are empty, and the model recorded is the selected route

#### Scenario: Credential never appears in a record
- **WHEN** the process environment holds a worker credential value and the stub OpenCode executor returns a failed terminal whose error text is already redacted
- **THEN** the attempt reason, the outcome, the attempt record and the receipt payload contain the redacted text and do not contain the credential value
