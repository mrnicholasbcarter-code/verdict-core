# Spec Delta: autodev OpenCode isolated executor

"Byte-identical", for a user repository, means all four of: every file under the working
tree outside `.git` (path, kind, executable bit, bytes) and the set of directory paths there,
empty directories included; the output of `git status
--porcelain=v1 -z --untracked-files=all`; `HEAD` and the output of `git for-each-ref`; and
the bytes of `.git/config`, `.git/info/exclude` and every file under `.git/hooks`. "Before the
attempt" is the moment `execute_unit` is called, after any edit the mechanical tier made.

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

With `--worker-harness opencode`, live consent and no `--dry-run`, autodev SHALL check, before
any probe, planner call, attempt tree or model call and whether or not an execution-path
decision was supplied, that the opencode binary resolves and runs, that `verdict serve` answers
`GET /health` with status 200 and `"engine": "verdict"`, and that the credentials the
OpenCode executor signs with exist: the serve token resolves and the worker signing key is
readable. Without them every unit would fail after the probe and the planner call had been paid
for. The credential check only reads: the serve token (the environment, the credential store,
then `serve.env`) and the signing key file. It creates, removes and modifies nothing, whether
the credentials are there or not, and its message never contains a value.
When a check fails it SHALL print one message naming the failed check and the remedy, exit with
status 1, and SHALL NOT fall back to `PatchExecutor`. A version of OpenCode different from
the pinned one SHALL NOT fail the check and SHALL be named in the output.

#### Scenario: opencode binary missing
- **WHEN** the binary cannot be resolved or exits non-zero on `--version`
- **THEN** autodev exits 1 with one message naming the binary check, and the route probe, the decomposer, `run_autodev` and `PatchExecutor` are never called

#### Scenario: serve not answering
- **WHEN** the connection to serve is refused, times out, returns a non-200, or returns a body whose `engine` is not `verdict`
- **THEN** autodev exits 1 with one message naming the serve check, with no upstream request beyond that health request

#### Scenario: Credentials missing
- **WHEN** the serve token does not resolve, or the worker signing key file is absent or unreadable, and the binary and serve checks pass
- **THEN** autodev exits 1 with one message naming the credential check and the remedy, and the route probe, the decomposer, `run_autodev` and `PatchExecutor` are never called

#### Scenario: Credential check creates or modifies nothing
- **WHEN** the credential check runs with the credentials present, with no serve token, and with a token but no signing key
- **THEN** no path under the home directory is created, removed or changed (kind, mode, modification time and bytes), and the check reports the credentials as present or names the missing one

#### Scenario: Readiness runs even when a decision is supplied
- **WHEN** an execution-path decision is passed to the command and the binary check fails
- **THEN** autodev exits 1 with the binary message and `run_autodev` is never called

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
worker starts. The overlay SHALL follow no link in the clone: it opens every ancestor of a path it
removes or writes from a descriptor for the clone's root without following links, so a symlink committed
to the repository cannot send a removal or a write outside the clone. A removal below an ancestor that is
absent, a link or a file has nothing to remove; a write replaces such an ancestor by a directory.
The files the overlay reads from the user's tree are held in memory until the clone is written, so
they SHALL share one byte budget (`MAX_SEED_BYTES`) and one file budget (`MAX_SEED_FILES`) while
they are loaded: each read stops at what is left of the byte budget, the load stops at the file
that crosses either budget, and the seed is refused with the limit named before the rest are read.
Only a file or a symlink kept in memory counts; a path the user deleted keeps nothing and counts
for neither budget. The manifest of the finished clone is still checked against both limits.

The attempt directory SHALL be removed on every exit path in which the worker has left it where the
executor made it and the executor can empty it; on no exit path SHALL the executor remove an entry it did not
make, so its cleanup is best effort and identity-checked, and it leaves what it cannot safely remove. The
worker runs as the same user inside it, so it can rename the directory or leave a link where it was: the
executor SHALL hold the directory by descriptor from the moment it makes it, SHALL empty it through that
descriptor, descending by descriptor and following no link, and SHALL remove its name only when the entry
there still is the directory it made (device and inode). What it leaves, by design: a directory the worker
moved away is emptied and left where it was moved; whatever the worker put at the old name is not touched
(a replacement entry stays, a link is not followed); a directory the worker made unreadable is opened
again by changing its mode by name, relative to its parent's descriptor and without following a link,
where the host supports that, and where it does not, that directory and the attempt directory above it
are left; an entry that cannot be removed is left with the directory above it. Cleanup SHALL NOT raise an
`Exception`, so that it cannot replace an exception that is propagating, and a descriptor whose close fails
SHALL NOT stop the executor closing the others. A close that fails while a path's directories are being
opened and one of them is not a real directory SHALL NOT replace that refusal. Every descriptor the executor
opened for an attempt is closed on every exit path.

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

#### Scenario: Overlay bytes share one budget while they are loaded
- **WHEN** several dirty or untracked files each fit the seed's byte limit and together exceed it
- **THEN** the seed is refused with the limit, and no more than the byte limit plus one read chunk was read from the user's tree

#### Scenario: Overlay file count is bounded while the files are loaded
- **WHEN** the files to copy outnumber the seed's file limit
- **THEN** the seed is refused with the limit, and no file past the one that crossed the limit was read

#### Scenario: Deleted files do not count against the overlay limits
- **WHEN** the user deleted more tracked files than the seed's file limit and nothing else is dirty
- **THEN** the seed succeeds

#### Scenario: Attempt tree removed on every ordinary exit path
- **WHEN** an attempt ends applied, rejected, with a failed or timed-out worker, with an exception, or by cancellation, and the worker has not moved, replaced or locked the attempt directory or anything in it (the scenarios below cover those cases)
- **THEN** the attempt directory no longer exists

#### Scenario: Removal does not follow a symlinked ancestor
- **WHEN** the repository commits `d` as a symlink to a directory outside it, the user replaced `d` by a real directory, and the unit owns the absent `d/victim` while the outside directory holds a file or a directory named `victim`
- **THEN** the outside `victim` is untouched, `d` is a real directory in the attempt tree, and the user's repository is byte-identical after seeding

#### Scenario: Copy does not write through a symlinked ancestor
- **WHEN** the repository commits `d` as a symlink to a directory outside it and the user replaced `d` by a real directory holding the untracked `new.txt`
- **THEN** the attempt tree holds `d/new.txt` in a real directory `d` and the outside directory is still empty

#### Scenario: Removal of a directory
- **WHEN** the unit owns a path that is a directory in the clone and is absent in the user's tree
- **THEN** the directory and everything below it is absent from the attempt tree

#### Scenario: Cleanup does not follow a replaced attempt directory
- **WHEN** the worker renames the attempt directory and leaves a symlink to a directory outside it at the old name
- **THEN** the outside directory and everything below it keep their modes and contents, the renamed directory Verdict made is emptied, and the attempt is `error`

#### Scenario: Cleanup leaves a directory that replaced the attempt directory
- **WHEN** the worker renames the attempt directory and creates a different, empty directory at the old name
- **THEN** that directory is not removed, and the renamed directory Verdict made is emptied

#### Scenario: Cleanup does not follow a link inside the tree
- **WHEN** the worker leaves a symlink to an outside directory inside a read-only ignored directory of the attempt tree
- **THEN** the attempt directory is removed and the outside directory keeps its mode and contents

#### Scenario: Cleanup removes directories the worker made unusable
- **WHEN** the worker leaves a directory tree with mode 0o555 (readable), or with mode 0o000 on a host that can change a directory's mode by name without following a link
- **THEN** the attempt directory no longer exists

#### Scenario: No descriptor is left open
- **WHEN** an attempt ends applied, rejected, with a failed worker, with an exception, or by interrupt
- **THEN** the process has the same number of open descriptors as before the attempt

#### Scenario: Cleanup never raises when a close fails
- **WHEN** closing the attempt directory's descriptor fails with an I/O error while `remove` runs
- **THEN** `remove` does not raise, the temporary directory's descriptor is still closed, and the attempt directory is removed

#### Scenario: A failed construction does not replace its own error
- **WHEN** the attempt directory cannot be opened after it was made, and closing the descriptor already opened also fails
- **THEN** the error that propagates is the one from the failed open, and the directory that was made is removed

#### Scenario: A parent that is a file is still a conflict when a close fails
- **WHEN** a component of a path's parent directories is a file, and closing the directory descriptors already opened for the path fails
- **THEN** the path is reported as a conflict (a parent directory is not a real directory), not as an I/O error

#### Scenario: A refused seed keeps its own error when cleanup cannot close
- **WHEN** the seed is refused (the repository has no commit) and closing the descriptor the cleanup opened fails
- **THEN** the refusal propagates and not the close error

### Requirement: An owned file SHALL be tracked and unmodified, or absent, before a worker starts

The executor's transfer keeps its undo information in memory, so a process that dies after a
file was replaced cannot undo it, and git must hold the user's bytes. The executor SHALL
therefore refuse a unit when any of its owned paths:

- is reported by `git status` (modified, staged, deleted, renamed or untracked), or exists and
  is not tracked (an ignored file), or does not exist and is ignored by the repository's ignore
  rules (`git check-ignore`; `git status` never lists an ignored path, so neither the change
  the unit makes nor the receipt's `changed_files` would show it);
- is tracked and `git ls-files -v` tags it with anything but `H` (`h` assume-unchanged, `S` or
  `s` skip-worktree: git does not read the working-tree bytes, so an edit leaves `git status`
  clean); or
- is tracked and `git check-attr` gives `filter` or `ident` a value other than `unspecified` or
  `unset` (a clean filter or an `ident` keyword can map different working-tree bytes to the same
  blob, so `git status` reports clean while `git checkout` writes other bytes).

It SHALL also refuse when any of the four reads (`git status`, `git ls-files -v`, `git
check-attr`, and `git check-ignore` for an owned path that does not exist) fails. An owned path
that does not exist, that git does not report and that no ignore rule matches SHALL NOT be
refused, whatever attributes match its name, and no other file is checked. Line endings and
encoding are not checked: under a `text`, `eol`, `core.autocrlf` or `working-tree-encoding`
conversion `git checkout` writes the bytes git chooses. The reads are made at the start of the unit, before the mechanical tier; a
refusal is acted on only if the mechanical tier then declines the unit, so an edit the
mechanical tier makes to a clean owned file does not cause one. A refused unit SHALL fail with a
reason that names the path and the remedy, start no worker, create no attempt tree and count
as no model attempt (no attempt-ledger row, no unreported-usage entry, not among the model
units in the report summary). The OpenCode executor SHALL write nothing for a refused unit;
an edit the mechanical tier already made to an owned file stays, as on the default path, and
is attributed to the unit. The default path (`PatchExecutor`) SHALL keep running on a modified
owned file.

#### Scenario: Tracked and unmodified owned file
- **WHEN** every owned file is tracked, tagged `H`, unmodified and has no `filter` or `ident` attribute
- **THEN** the unit is not refused and the worker starts

#### Scenario: Modified owned file refused
- **WHEN** an owned file has an uncommitted change in the working tree and the mechanical tier makes no edit
- **THEN** the unit fails with a reason naming the file and saying to commit or stash it, no worker starts, no attempt record is written, and the repository is byte-identical

#### Scenario: Staged or deleted owned file refused
- **WHEN** an owned file has a staged change and an unchanged working tree, or was deleted in the working tree
- **THEN** the unit is refused as above

#### Scenario: Untracked or ignored owned file refused
- **WHEN** an owned file exists and is untracked, or exists and is ignored
- **THEN** the unit is refused as above

#### Scenario: Assume-unchanged owned file refused
- **WHEN** an owned file is marked assume-unchanged and was edited, so `git status` lists nothing
- **THEN** the unit is refused as above, naming the file and its tag

#### Scenario: Skip-worktree owned file refused
- **WHEN** an owned file is marked skip-worktree, whether it was edited or is absent from the working tree, so `git status` lists nothing
- **THEN** the unit is refused as above, naming the file and its tag

#### Scenario: Owned file with a filter or ident attribute refused
- **WHEN** a tracked owned file has a `filter` attribute whose clean command drops a line, or an `ident` attribute, and the user made a same-size edit that the filter or keyword maps to the same blob, so `git status` lists nothing
- **THEN** the unit is refused as above, naming the file and the attribute

#### Scenario: Absent owned file accepted
- **WHEN** an owned path does not exist, git does not report it, no ignore rule matches it, and the unit is to create it, even where a `filter` attribute matches its name
- **THEN** the unit is not refused

#### Scenario: Ignored absent owned file refused
- **WHEN** an owned path does not exist and an ignore rule of the user's repository matches it
- **THEN** the unit is refused as above, naming the file and saying that an ignored file is not shown by `git status`, and no worker starts

#### Scenario: Ignored and plain absent owned files refused
- **WHEN** a unit owns one absent path that an ignore rule matches and one absent path that none matches
- **THEN** the unit is refused naming the ignored path, no worker starts and nothing is created, so a repair cannot transfer only the plain file

#### Scenario: Unowned dirty file does not refuse
- **WHEN** a file the unit does not own has uncommitted changes
- **THEN** the unit is not refused and the file is carried into the seed

#### Scenario: Git read fails closed
- **WHEN** `git status`, `git ls-files -v`, `git check-attr` or `git check-ignore` exits with a failure (for `check-ignore`, any status other than 0 and 1)
- **THEN** the unit is refused with the git failure as the reason, and is not treated as clean

#### Scenario: Mechanical edit does not refuse
- **WHEN** the mechanical tier edits a clean owned file with `ruff --fix` and then declines the unit
- **THEN** the unit is not refused and the worker starts on the edited file

#### Scenario: Refused unit keeps a mechanical edit
- **WHEN** an owned file has an uncommitted change, the mechanical tier's `ruff --fix` edits it, and the unit's check still fails
- **THEN** the unit fails with the refusal reason, no worker starts, no attempt tree is made, the file holds the user's bytes with the mechanical edit, nothing else in the repository changed, and the file is in the unit's `changed_files`

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
swapping an ancestor directory for a symlink cannot redirect a read. It SHALL open the attempt tree once
for the walk and for every read, and accept only the directory it seeded (its device and inode), because
the directories above the tree are the worker's to rename or replace by a link. It SHALL stop reading a
file as soon as the tree's byte budget is spent, so a huge or still-growing file is never hashed to its end.

A change is a difference in a path's kind, executable bit or bytes between the seed and the end of the
attempt. Git's own state in the clone (a commit, a branch, the index, anything under `.git`) is not a
change, is never read and never copied back: a commit or an index-only edit to an unowned file that
leaves its working-tree bytes as seeded is therefore not seen, and reaches nothing in the user's
repository. A new path the user's ignore rules match is dropped as a byproduct (next requirement).

#### Scenario: Out-of-bounds edits are rejected
- **WHEN** a worker edits the bytes of an unowned file (and, in two variants, also stages or commits that edit), creates an unowned new file, or swaps a symlink into an owned path
- **THEN** the attempt is `rejected` naming the path and the user's repository is byte-identical

#### Scenario: Git-only changes to unowned files are not a change
- **WHEN** a worker edits an owned file, makes an empty commit, and stages and commits an edit to an unowned file while its working-tree bytes are put back to what was seeded
- **THEN** the delta contains the owned file alone

#### Scenario: A tree reached through a replaced attempt directory is not read
- **WHEN** the worker renames the attempt directory and leaves a symlink at the old name to a directory that holds a complete copy of the tree with an owned edit
- **THEN** the attempt is `error` with the reason that the attempt tree changed during validation, and the user's repository is byte-identical

#### Scenario: Manifest hashing stops at the byte budget
- **WHEN** a worker leaves one huge file, or several files that together exceed the tree's byte budget
- **THEN** the attempt is `rejected` for being over the limit, and no more than the budget plus one read chunk was read from the tree

#### Scenario: Seed walk stops at the byte budget
- **WHEN** the repository holds a tracked file larger than the byte budget
- **THEN** the seed is refused with the limit, and no more than the budget plus one read chunk was read from the clone

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

### Requirement: A new path ignored by the user's rules SHALL NOT count as an edit unless the unit owns it, and a path that existed at seed time SHALL NOT be a byproduct

A path absent from the seed manifest SHALL be dropped from the delta, and never transferred,
when it is ignored by the user's own ignore rules and the unit does not own it. A path the unit
owns SHALL NEVER be dropped as a byproduct: dropping it would transfer only part of a repair or
reject a whole repair as empty. A path present in the seed manifest SHALL always be subject to
the ownership check. A `.gitignore` written by the worker SHALL NOT be consulted. The executor
writes no context file into the attempt tree (the OpenCode executor delivers the task through its
own configuration directory), SHALL NOT require any executor-written file and SHALL treat a file
a worker or the OpenCode process writes that is neither ignored nor owned like any other
out-of-bounds path.

#### Scenario: No context file at all
- **WHEN** the OpenCode executor writes nothing into the attempt tree and the worker repairs an owned file
- **THEN** the attempt is `applied`, `changed_files` lists exactly the owned file and nothing else is in the user's repository

#### Scenario: Ignored byproduct
- **WHEN** a worker's test run creates `.pytest_cache/` and `__pycache__/` files that the user's `.gitignore` ignores
- **THEN** they are not in the delta and are not transferred

#### Scenario: Ignored owned new file
- **WHEN** a worker creates only a new owned file that the user's ignore rules match
- **THEN** the file is in the delta (it is not dropped as a byproduct and the delta is not empty)

#### Scenario: Ignored and plain owned new files
- **WHEN** a worker creates two new owned files, one that the user's ignore rules match and one that they do not
- **THEN** both are in the delta, so a transfer cannot carry only the plain one

#### Scenario: A worker cannot hide a path by editing .gitignore
- **WHEN** a worker adds a new unowned file and rewrites `.gitignore` in the attempt tree to ignore it
- **THEN** the attempt is `rejected` for the unowned file and for `.gitignore`

### Requirement: Transfer SHALL be all-or-nothing and SHALL NOT overwrite a file it sees changed after the seed

The seed SHALL record the device and inode of the repository directory it opens to read the
user's files, and the transfer SHALL be given that record. The transfer reopens the repository by
pathname; when the directory it opened is not the recorded one (the repository was renamed away
and another directory took its name since the seed) the executor SHALL refuse the whole attempt,
`rejected` with one reason (the repository was replaced since the seed), before it compares or
writes any path: nothing is created, nothing is written and nothing is journalled, whatever the
delta holds (a new file, which is absent on both sides of the comparison below, a changed file or a
deleted file) and whatever the replacement holds. A directory renamed away and renamed back is
the same directory and is not refused.

The executor SHALL resolve every transfer path component by component from a descriptor on
the repository root without following symlinks, and SHALL compare, for each path, the user's
current kind, executable bit and bytes with the seed manifest (or absence, when the manifest
has no entry), before any write, again immediately before that path's write, and a third time
after that path's temporary file is written and immediately before the rename. If any
comparison differs the attempt SHALL be `rejected` with the reason and nothing SHALL be written
for a pre-write difference. The checks before the first write SHALL be read-only and SHALL
create nothing; directories SHALL be created only during the write phase, each journalled
before it is created (journal order is in the restore requirement below). The second and third comparisons SHALL also confirm that every held directory
descriptor is still the directory reachable at its name, by device and inode. A refusal found
by the read-only checks, or by the third comparison when nothing was written or created before it,
SHALL make the attempt `rejected`: the third comparison refuses before the rename, so the path's
journal entry is withdrawn and the temporary file is unlinked. A refusal or failure after the write
phase has begun SHALL roll back every path and directory already written or created, SHALL make
the attempt `error`, and its reason SHALL name the refused path and list any path the rollback
could not restore. Every file the executor writes, in the transfer and in restore, SHALL go
through one helper that creates a uniquely named temporary file in the target directory by
descriptor, sets its mode, renames it over the target, and unlinks the temporary file by the
same descriptor on every failure before the rename succeeds. An unlink that itself fails SHALL
be named in the reason and SHALL NOT replace the original error. The executor SHALL refuse to
run on a host whose descriptor-relative file operations it needs are unavailable.

**What the third comparison guarantees, and what it does not.** A change to an owned path (its
kind, executable bit or bytes) or to a held directory that the comparison can see SHALL refuse that
path, whenever it was made before the comparison read it, including during the temporary file's
write: nothing is renamed and no temporary file is left. It is a look, not a lock: POSIX has no
rename that compares the target's content, and `renameat2` with `RENAME_EXCHANGE` was assessed
and not adopted (design D6). These stay outside the guarantee: a write that lands after the
comparison has read the file (including while it hashes the bytes) and before the rename returns;
a write through a descriptor that was open on the target before the rename, which the rename
detaches from its name; a change that leaves kind, executable bit and bytes equal (the other
permission bits, timestamps, ownership); and the deletion of an owned file, whose comparison
immediately precedes the `unlink` and has no later one. An edit that lands in those windows is
overwritten, and rollback cannot recover it, because the journal holds the seed's bytes and not
the edit. The executor assumes it is the only writer of an owned file while a unit runs.

#### Scenario: Owned file changed after the seed
- **WHEN** the user edits an owned file after the seed and before the transfer
- **THEN** the attempt is `rejected` with the reason that the file changed since the seed, and the user's edit is intact

#### Scenario: File appeared after the seed
- **WHEN** the seed had no file at an owned path and the user creates one before the transfer
- **THEN** the attempt is `rejected` and the user's file is intact

#### Scenario: Non-regular file in the user's tree
- **WHEN** an owned path in the user's tree is a FIFO or a directory
- **THEN** the attempt is `rejected`, nothing blocks, and nothing is written there

#### Scenario: Edit between the checks and the first write
- **WHEN** the user edits an owned file after the read-only checks and before the first write
- **THEN** the attempt is `rejected`, nothing was written or created, and the user's edit is intact

#### Scenario: Change while the temporary file is written is refused
- **WHEN** the user edits an owned file, changes only its executable bit, or removes it after the executor has written the temporary file and before the rename
- **THEN** the attempt is `rejected` with the reason (changed since the seed, its executable bit, removed after the seed), the user's state is intact, no temporary file is left and nothing is journalled for that path

#### Scenario: File created while the temporary file is written is refused
- **WHEN** the seed had no file at an owned path and the user creates one after the temporary file is written and before the rename
- **THEN** the attempt is `rejected` as appeared after the seed, the user's file is intact and no temporary file is left

#### Scenario: Change while a later temporary file is written rolls back
- **WHEN** a transfer of two owned files has written the first and the user edits the second while its temporary file is written
- **THEN** the first path is restored, the user's edit is intact, no temporary file is left and the attempt is `error` naming the second path

#### Scenario: Parent swapped while the temporary file is written
- **WHEN** a parent directory is renamed away and another directory takes its name after the temporary file is written and before the rename
- **THEN** nothing is renamed into the displaced directory or into the replacement, the displaced file is unchanged, no temporary file is left and the attempt is `rejected`

#### Scenario: Only the mode changed
- **WHEN** the user changes only the executable bit of an owned file after the seed
- **THEN** the attempt is `rejected` for that file and the user's mode is intact

#### Scenario: Symlinked parent
- **WHEN** an owned path's parent directory in the user's tree is a symlink
- **THEN** the attempt is `rejected` and nothing is written through the link

#### Scenario: Parent swapped for a symlink during the transfer
- **WHEN** a parent directory is replaced by a symlink after the first comparison and before the write
- **THEN** nothing is written through the link

#### Scenario: Repository replaced between the seed and the transfer
- **WHEN** the repository directory is renamed away after the seed and another directory takes its name, either a copy of the repository (so a changed or a deleted file holds the seeded bytes there) or an empty directory, and the delta is only a new file, a new file in new directories, a changed file or a deleted file (eight cases), and also when the replacement happens while the worker runs
- **THEN** the attempt is `rejected` with the one reason (the repository was replaced since the seed), the journal is empty, the replacement and the displaced repository are byte-identical to what they held, and no temporary file exists

#### Scenario: Repository renamed away and back is transferred into
- **WHEN** the repository directory is renamed away and renamed back after the seed and the transfer runs
- **THEN** the transfer writes into it and every journal entry's root is the seed's record

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

### Requirement: Every exit after a transfer other than a verified success SHALL restore the owned files, and restore SHALL NOT overwrite content it sees the transfer did not write

For an executor that has a `restore` method, every exit from `_run_unit_once` after
`execute_unit` was called, other than a verified success, SHALL call `restore()` exactly once
and SHALL NOT skip it for the exit being an exception. The exits are a false `_verify` result;
an exception from `execute_unit`, including one raised after its transfer; an exception from
`_touched_since` or `_verify`, including any error the command runner raises that `_run` does
not catch (for example `PermissionError`); and `KeyboardInterrupt`. `restore()` is a no-op
when nothing was transferred. For a false result `_run_unit_once` SHALL record the real
restore result in the outcome reason. An `Exception` from `execute_unit` keeps its behaviour on
main: it becomes an unverified outcome whose reason begins `executor error:`, after the one
`restore()` call, and is not re-raised. Every other exception (a `KeyboardInterrupt` from
anywhere, and any exception from `_touched_since` or `_verify`) SHALL be re-raised unchanged
after restore. A failure inside `restore()`, including a `KeyboardInterrupt` or `SystemExit` raised in
it, SHALL be caught and reported on standard error, as far as standard error can be written (a closed or
failing standard error ends the report, never the original exception), and SHALL NOT replace or hide the original
exception: an interrupt that arrives while an earlier exception is propagating is reported, not
obeyed, and the journal entries not yet restored stay journalled (git can put them back, as after a crash). The executor SHALL make the transfer the last step of
`execute_unit` that can fail: the attempt tree is removed before it. The executor's journal
SHALL belong to one `execute_unit` call: it is emptied as that call's first action and after a
rollback, so a later unit's failure cannot undo an earlier unit's verified change.

The transfer SHALL journal every mutation it makes of the user's tree (creating a directory,
renaming a file over its target, deleting a file) before it makes it: the entry holds the path,
its prior state (kind, executable bit, bytes, or absence) and its intended state. A
`KeyboardInterrupt` that arrives right after a mutation succeeded, before the transfer returns,
therefore finds the mutation journalled. Restore SHALL, for each entry, return a path that
holds the intended state to the prior state, using the same no-follow traversal and
at-the-moment comparison as the transfer; SHALL leave a path that already holds the prior state
as it is and not report it (the mutation was never made); SHALL leave alone and name a path in
any other state (kind, executable bit or bytes differ from both); and SHALL remove directories
it created when they are empty and are still the directory it created. The transfer SHALL record
the device and inode of each directory it creates as soon as the creation has returned, and
restore SHALL compare them with the directory now at that name before removing it: a directory
that was removed and replaced by another one (even an empty one) is left alone and named as not
restored. The transfer SHALL record the device and inode of the repository root it wrote through (the
directory the seed read: the transfer refuses any other) in
every journal entry (a directory creation, a write and a deletion alike), when the entry is made, and
restore, which reopens the repository by pathname, SHALL compare the directory it opened with that
record before it reads or writes any path: when the repository was renamed away and another directory
took its name, nothing is read or written, every journalled path is named as not restored with one
reason (the repository was replaced), and the journal is empty; when any entry's root was not recorded,
nothing is read or written either and every journalled path is named as not restored (not recorded). For every file it writes or deletes the transfer SHALL also record, in the journal entry and
before that entry is appended, the device and inode of each directory above the path, below the root,
as the transfer opened it, and restore SHALL open the same path by descriptor and compare them before it reads the
file: a directory that is not the one the transfer wrote through (moved away and absent, or
replaced by a real directory, whatever bytes the replacement's file holds) makes that path not
restored and named, and nothing is written there. An entry whose recorded directories do not match
the path's depth is not restored and named. A device and inode pair can be reused once the original
directory has been deleted; a replacement that reuses the pair of a deleted original is outside this
guarantee (the file the transfer wrote went with that directory). Restore's own write SHALL take the
same last look as the transfer's third comparison, immediately before its rename (the held
directories and the target's state against the state restore read), and a change found there is that
path's not-restored entry, with the same stated limits; the removal of a file the transfer created has,
like a deletion, only the comparison immediately before its `unlink`. A journal entry whose directory identity was never recorded (a `KeyboardInterrupt`
arrived between the creation returning and the identity being read) cannot be compared; restore
removes that directory only if it exists, is a real directory and is empty. That window is a
stated limit. A read or write failure on one path during restore (a permission error on reading its
state, say) SHALL be that path's not-restored entry, with the failure named, and SHALL NOT end the
restore: the entries still in the journal are restored. An entry leaves the journal only after it has
been dealt with, so an interrupt in the middle of one leaves it (and those before it) journalled and a
second restore covers them. Restore SHALL cover only paths the transfer wrote. A verified unit SHALL
NOT be restored. The `PatchExecutor` path SHALL be unchanged.

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

#### Scenario: Created directory replaced
- **WHEN** a directory the transfer created is removed and replaced by another empty real directory before restore runs
- **THEN** restore leaves the replacement in place and names it as not restored

#### Scenario: Created files and directories are removed
- **WHEN** the transferred change created an owned file in a new directory and verification fails
- **THEN** the file and the directory restore created are gone

#### Scenario: Earlier verified unit is not undone
- **WHEN** unit A is verified and unit B on the same executor is then rejected, errors or raises
- **THEN** `restore()` for unit B leaves unit A's transferred files as they are

#### Scenario: Repository replaced between transfer and restore
- **WHEN** after a transfer that wrote an existing top-level file, created a top-level file, or deleted one (three cases), and also created a nested file in new directories, the repository directory is renamed away and a different directory takes its name, holding at the top level what restore would act on (the worker's bytes, the created file, or no file for the deleted one), and restore runs
- **THEN** restore writes nothing in the replacement or in the displaced repository, names every journalled path as not restored with the one reason (the repository was replaced), `restored` is empty and the journal is empty

#### Scenario: Repository renamed away and back is the same directory
- **WHEN** the repository directory is renamed away and renamed back after a transfer, and restore runs
- **THEN** restore returns the owned file to its prior bytes, and nothing is named as not restored

#### Scenario: Entry without a recorded repository is named
- **WHEN** one entry of a journal of two has no recorded repository directory
- **THEN** restore writes nothing for either and names both as not restored (not recorded)

#### Scenario: Parent replaced by a real directory between transfer and restore
- **WHEN** a transferred file's parent directory is renamed away and a real directory takes its name, holding either the worker's bytes or the file's prior bytes, and restore runs
- **THEN** restore writes nothing in the replacement or the displaced directory, names the path as not restored (replaced), and the journal is empty

#### Scenario: Parent renamed away and not replaced is named
- **WHEN** the directory of a transferred file is renamed away, for an existing owned file and for one the transfer created, and nothing takes its name
- **THEN** restore names the path as not restored (its directory is gone) and the displaced file is unchanged

#### Scenario: Entry without recorded parent directories is named
- **WHEN** a journal entry for a path below a directory has no recorded parent directories
- **THEN** restore writes nothing for it and names it as not restored (not recorded)

#### Scenario: Change while restore writes the prior bytes is kept
- **WHEN** the user edits a transferred file after restore has written its temporary file and before the rename
- **THEN** the edit is intact, the path is named as not restored (changed since the transfer) and no temporary file is left

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
- **WHEN** `execute_unit` raises an `Exception` after the transfer was written
- **THEN** `restore()` is called once, the owned files hold their prior content, and the unit is unverified with a reason beginning `executor error:`

#### Scenario: Interrupt inside the executor
- **WHEN** a `KeyboardInterrupt` is raised inside `execute_unit` right after a mutation of its transfer, with the real `OpenCodeUnitExecutor`
- **THEN** `_run_unit_once` calls `restore()` once, the repository is byte-identical to before the attempt, and the `KeyboardInterrupt` propagates unchanged

#### Scenario: Restore fails while an exception propagates
- **WHEN** `_verify` raises and `restore()` then raises a different exception
- **THEN** the exception that propagates is the original, and the restore failure is on standard error

#### Scenario: The restore diagnostic cannot replace the original
- **WHEN** `restore()` fails while an exception propagates and standard error is closed or its write fails (`OSError`, `BrokenPipeError` or `ValueError`)
- **THEN** the exception that propagates is the original object, and `restore()` was called once

#### Scenario: Restore is interrupted while an exception propagates
- **WHEN** `_verify` raises (or a `KeyboardInterrupt` is raised) and `restore()` then raises a `KeyboardInterrupt` or a `SystemExit`
- **THEN** the exception that propagates is the original object, `restore()` was called once, and the interrupt is on standard error

#### Scenario: Restore continues after a state read error
- **WHEN** reading the state of one journalled path raises a `PermissionError` during `restore_transfer`
- **THEN** that path is in `not_restored` with the failure named, the other journalled paths are restored, and the journal is empty

#### Scenario: Rollback continues after a state read error
- **WHEN** a transfer fails on its third path and reading the state of its second path then fails during the rollback
- **THEN** the transfer raises `TransferFailedError` naming the second path as not restored, the first path is restored, and the journal is empty

#### Scenario: Interrupted restore keeps the unprocessed entry
- **WHEN** a `KeyboardInterrupt` arrives while `restore_transfer` reads the state of a path
- **THEN** that path's entry and those before it stay in the journal, and a second `restore_transfer` leaves the repository byte-identical to before the transfer

#### Scenario: Restore without a transfer
- **WHEN** the executor's attempt was rejected or errored, so nothing was transferred, and a later step raises
- **THEN** `restore()` changes nothing

#### Scenario: Restore write failure leaves no temporary file
- **WHEN** a restore write fails for one path
- **THEN** that path is named as not restored, no temporary file remains and the other paths are restored

#### Scenario: Interrupt right after a rename
- **WHEN** the real rename of an owned file over its target has returned and a `KeyboardInterrupt` is raised before the transfer returns, and `restore()` is then called
- **THEN** the repository is byte-identical to before the attempt, and the exception that left the transfer is that `KeyboardInterrupt`

#### Scenario: Interrupt right after a deletion
- **WHEN** the real deletion of an owned file has returned and a `KeyboardInterrupt` is raised before the transfer returns, and `restore()` is then called
- **THEN** the deleted file holds its prior bytes and mode, the repository is byte-identical to before the attempt, and the exception that left the transfer is that `KeyboardInterrupt`

#### Scenario: Interrupt right after a directory creation
- **WHEN** the real creation of a directory for a new owned file has returned and a `KeyboardInterrupt` is raised before the transfer returns, and `restore()` is then called
- **THEN** the directory is gone, the repository is byte-identical to before the attempt, and the exception that left the transfer is that `KeyboardInterrupt`

#### Scenario: Interrupt before the mutation
- **WHEN** a `KeyboardInterrupt` is raised after a journal entry was appended and before its mutation was made, and `restore()` is then called
- **THEN** the path is unchanged and is not named as not restored

### Requirement: After a process crash during the executor's writes every owned path SHALL hold its old or its new bytes, and git SHALL be able to undo it

The guarantee covers the executor's own writes. For a unit the executor accepted, a process that
dies at any point from the first temporary file to the last rename (SIGKILL, SIGTERM or an
uncaught crash; power loss is not claimed) SHALL leave every owned path holding either its
complete prior bytes or the worker's complete bytes. The only other residue the executor leaves
SHALL be files the transfer created, empty directories it created and at most one temporary
file named `.verdict-transfer-*`, all in the owned files' own paths and directories; it changes
no other path. After the last rename the executor writes nothing until verification ends.

The verification command runs in the user's tree and may write any file, as it does on the
default path. What it writes, torn bytes from a command killed while writing included, any file
it creates and any change it makes to the index or `.git`, are outside this guarantee, and no
restore or recovery covers them.

Every pre-existing owned path was tracked and unmodified when the unit started (provided
nothing but the mechanical tier wrote it since, the single-writer assumption), so, while the
index is as it was at that moment, `git checkout -- <path>` restores it and also drops any edit
the mechanical tier made. It writes line endings and encoding as git chooses. No automatic
recovery runs after a crash.

#### Scenario: Kill after the temporary write
- **WHEN** the process is killed after the temporary file of the first path is written and before its rename
- **THEN** every owned path holds its prior bytes, at most one `.verdict-transfer-*` file remains, and `git status` lists nothing else

#### Scenario: Kill after the first of two renames
- **WHEN** the process is killed after the first of two owned files was renamed into place
- **THEN** the first holds the worker's bytes, the second its prior bytes, neither is torn, and `git checkout -- <owned paths>` followed by removing the created files and empty directories leaves the repository byte-identical to its state when the unit started

#### Scenario: Kill after the last rename
- **WHEN** the process is killed after the last rename and before verification
- **THEN** every owned path holds the worker's bytes and the same two steps leave the repository byte-identical to its state when the unit started

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

### Requirement: Each OpenCode attempt SHALL be signed under the autodev run id and the unit id

The OpenCode executor signs a per-attempt worker assignment bound to a run id and a node id and
launches nothing without them. `run_autodev` SHALL give an executor that has a `begin_run`
method the attempt-ledger run id (`autodev-<uuid>`) once, before the first unit, and the
isolated executor SHALL pass that run id and the unit's `unit_id` as `run_id` and `node_id` on
every OpenCode attempt, so `verdict serve`'s receipts and the autodev attempt ledger name the
same ids. An isolated executor that was not given a run id SHALL return an `error` attempt
without creating an attempt tree. The attempt timeout SHALL be above the time OpenCode spends
retrying an upstream 429 or 503 before it prints the status (about 100 s) and short enough for
the assignment lifetime (the timeout rounded up plus 60 s, at most 1,800 s).

#### Scenario: Run id and unit id reach the OpenCode executor
- **WHEN** `run_autodev` runs a unit on the isolated executor
- **THEN** the OpenCode executor receives the same `run_id` the unit's receipt records as `attempt_run_id` and the unit's `unit_id` as `node_id`

#### Scenario: No run id
- **WHEN** the isolated executor's `execute_unit` is called before `begin_run`
- **THEN** it returns an `error` attempt naming the missing run id, the OpenCode executor is not called and no attempt tree is created

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
