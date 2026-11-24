# Spec Delta: autodev OpenCode isolated executor

"Byte-identical", for a user repository, means all four of: every file under the working
tree outside `.git` (path, kind, executable bit, bytes); the output of `git status
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
objects and no remote. It SHALL overlay the user's modified, deleted, staged and untracked
non-ignored files onto the clone by copying files, and SHALL NOT write any git object, ref,
config or hook into the user's repository. It SHALL record a manifest of the seeded tree
before the worker starts. The attempt directory SHALL be removed on every exit path.

#### Scenario: The clone shares nothing with the user's repository
- **WHEN** an attempt tree is seeded from a repository that has its own hook, local config value and `info/exclude` entry
- **THEN** the tree has no `objects/info/alternates`, no object file with a link count above one and no remote, and contains none of the user's hook, config value or exclude entry

#### Scenario: Dirty state is carried over
- **WHEN** the user's tree has a modified tracked file, a deleted tracked file, a staged file, an untracked file and an ignored file
- **THEN** the attempt tree holds the modified, staged and untracked contents, lacks the deleted file and lacks the ignored file, and the user's repository is byte-identical after seeding

#### Scenario: Owned files hold the user's bytes
- **WHEN** an owned file is unmodified in the user's tree but its checkout bytes differ from the working-tree bytes (for example through an `eol` attribute)
- **THEN** the attempt tree holds the user's working-tree bytes for that file, and the transfer check compares against those bytes

#### Scenario: Seed refused before any worker starts
- **WHEN** the repository has no HEAD commit, contains a submodule or a nested repository, or exceeds the seed limit
- **THEN** the attempt is `error` with a reason naming which, no worker starts and no attempt directory remains

#### Scenario: Attempt tree removed on every exit path
- **WHEN** an attempt ends applied, rejected, with a failed or timed-out worker, with an exception, or by cancellation
- **THEN** the attempt directory no longer exists

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

#### Scenario: Out-of-bounds edits are rejected
- **WHEN** a worker edits an unowned file by a plain edit, by staging it, by committing it, by creating an unowned new file, or by swapping a symlink into an owned path
- **THEN** the attempt is `rejected` naming the path and the user's repository is byte-identical

#### Scenario: A valid owned repair is applied
- **WHEN** a worker changes only an owned file
- **THEN** the attempt is `applied`, `changed_files` lists exactly the owned paths, and the user's tree holds the new content

#### Scenario: No change
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

Before writing, the executor SHALL verify for each path that existing parents are real
directories under the repository root, that an existing target is a regular file whose bytes
match the seed manifest, and that a target absent from the manifest does not exist. If any
check fails the attempt SHALL be `rejected` with the reason and nothing SHALL be written. If
a write fails part-way the executor SHALL undo every write already made and the attempt SHALL
be `error`.

#### Scenario: Owned file changed after the seed
- **WHEN** the user edits an owned file after the seed and before the transfer
- **THEN** the attempt is `rejected` with the reason that the file changed since the seed, and the user's edit is intact

#### Scenario: Symlinked parent
- **WHEN** an owned path's parent directory in the user's tree is a symlink
- **THEN** the attempt is `rejected` and nothing is written through the link

#### Scenario: Failure part-way
- **WHEN** a transfer of three owned files fails on the second
- **THEN** all three paths hold their prior content and the attempt is `error`

### Requirement: A failed verification after a transfer SHALL restore the owned files, and restore SHALL NOT overwrite content it did not write

When `_verify` fails for an applied attempt of an executor that can restore, `_run_unit_once`
SHALL call `restore()` and SHALL record its real result in the outcome reason. Restore SHALL
return each transferred path to its prior bytes or absence, remove directories it created
when empty, and SHALL leave alone and name any path whose content is no longer what the
transfer wrote. A verified unit SHALL NOT be restored. The `PatchExecutor` path SHALL be
unchanged.

#### Scenario: Verification fails after an applied attempt
- **WHEN** an owned-file change is transferred and the unit's verification command exits non-zero
- **THEN** the repository is byte-identical to before the attempt, a pre-existing dirty unowned file is untouched, and the reason says the owned files were restored

#### Scenario: The user edits after the transfer
- **WHEN** the user changes a transferred file after the transfer and verification then fails
- **THEN** restore leaves that file as the user left it and the reason names it as not restored

#### Scenario: Created files are removed
- **WHEN** the transferred change created an owned file in a new directory and verification fails
- **THEN** the file and the directory restore created are gone

#### Scenario: Verified unit
- **WHEN** verification passes
- **THEN** restore is not called and the change stays

### Requirement: Retries SHALL be refused for the isolated executor, and PatchExecutor SHALL never run in its place

When the executor is isolated and `retry_routes` or `retry_executor_factory` is non-empty,
`run_autodev` SHALL raise `AutodevError` before any planner call. A failed unit on the
isolated executor SHALL NOT be retried on any executor and SHALL NOT cause `PatchExecutor`
to be constructed. With the flag the CLI SHALL pass no retry routes.

#### Scenario: Retry routes with the isolated executor
- **WHEN** `run_autodev` is given the isolated executor and one retry route
- **THEN** it raises `AutodevError` before the decomposer is called

#### Scenario: Failed unit
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

#### Scenario: The credential never appears in a record
- **WHEN** the process environment holds a worker credential value and the stub OpenCode executor returns a failed terminal whose error text is already redacted
- **THEN** the attempt reason, the outcome, the attempt record and the receipt payload contain the redacted text and do not contain the credential value
