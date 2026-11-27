## ADDED Requirements

### Requirement: REJECTED attempts start from the clean base on retry

When a DAG node attempt is rejected — either because an independent checker judged the
implementation wrong (`verification_failed`, `diff_rejected`) or because the terminal carries
`validated=True` — the runtime SHALL NOT restore salvaged files from the previous attempt into
the next attempt's worktree.  The salvage directory for that node SHALL be cleared (not saved) so
that stale rejected content cannot contaminate the next attempt.  The restore event emitted for
the next attempt SHALL carry `reason: "clean_base"` and an empty `restored` list.

Exception (GitHub issue #1372, operator decision 2026-11-26): a `verification_failed` attempt of a
worker that edits files in place (terminal `harness` is `opencode`) is covered by the requirement
"A verification-failed attempt of an in-place worker continues from its own files" below, not by
this one.

`diff_rejected` failures are classified with `category="diff_rejected"`,
`action="CORRECT_IMPLEMENTATION"`, `cooldown_seconds=0`, and `scope="none"`.  The route is not
at fault; the fix must come from a corrected implementation.

#### Scenario: verification-rejected attempt, next attempt starts from clean base

- **WHEN** a node attempt returns `ok=True` but its verification command exits non-zero
- **AND** the attempt's terminal `harness` is not `opencode`
- **THEN** the runtime records `validated=True` and sets `last_rejected=True` for that node run
- **AND** `_fail()` clears the node's salvage directory instead of saving partial work
- **AND** on the next attempt `_attempt()` emits a restore event with `reason="clean_base"`, `restored=[]`, `restored_count=0`
- **AND** the next attempt's worktree contains only the base-revision files, not the rejected implementation

#### Scenario: diff_rejected attempt, next attempt starts from clean base

- **WHEN** a node attempt's terminal has `error` starting with `"diff_rejected"`
- **THEN** the failure is classified as `category="diff_rejected"` and `last_rejected=True` is set for that node run
- **AND** no salvage is saved; the next attempt starts from the clean base
- **AND** the restore event carries `reason="clean_base"`

### Requirement: A verification-failed attempt of an in-place worker continues from its own files

When a node attempt of a worker that edits files in place (the attempt's terminal `harness` is
`opencode`) fails with category `verification_failed`, the runtime SHALL keep that attempt's owned
files under the existing salvage rules (owned files only, size limits, credential check) and SHALL
restore them into the next attempt's worktree.  The restore event SHALL carry
`reason: "verification_failed"` and the restored paths, the cached research pack SHALL be deleted
as for an interrupted attempt, and the retry text given to the worker SHALL say that the previous
attempt's files are already in the worktree and name them.  `ownership_violation`,
`diff_rejected` and every other harness SHALL keep the clean base.

The clean-base rule was written for workers that emit diffs against the base revision; a worker
that edits in place reads the files it finds, and a failed tree is usually the nearest state to a
passing one.

#### Scenario: in-place worker fails verification, next attempt continues from its files

- **WHEN** a node attempt whose terminal `harness` is `opencode` returns `ok=True` and its verification command exits non-zero
- **THEN** the attempt's owned files are saved to the salvage directory
- **AND** on the next attempt they are restored and the restore event carries `reason="verification_failed"` and the restored paths
- **AND** the worker's retry text names the restored files

#### Scenario: in-place worker changes a file outside its ownership

- **WHEN** a node attempt whose terminal `harness` is `opencode` ends with `ownership_violation`
- **THEN** no salvage is saved and the next attempt's restore event carries `reason="clean_base"`

### Requirement: INTERRUPTED attempts restore salvage with reason field and pack invalidation

When a DAG node attempt fails due to an interruption (timeout, transport error, any failure with
`validated=False` and `category` not in `_JUDGED_WORK_CATEGORIES`), the existing salvage
behaviour is preserved: partial owned files are saved and restored on retry.  The restore event
SHALL carry `reason: "interrupted"`.  When files are actually restored, the runtime SHALL delete
the cached research pack (`run_dir/context_artifacts/{node_id}.json`) if it exists, so the next
attempt's context hydrator re-reads the restored worktree state rather than the base-revision
snapshot.

#### Scenario: interrupted attempt, salvage restored and research pack invalidated

- **WHEN** a node attempt fails with `validated=False` and a non-judged category
- **AND** the node had written owned files before failing
- **THEN** those files are saved to the salvage directory
- **AND** on the next attempt the files are restored into the fresh worktree
- **AND** the restore event carries `reason="interrupted"` and the restored file paths
- **AND** if `run_dir/context_artifacts/{node_id}.json` exists it is deleted after the restore

#### Scenario: interrupted attempt with no prior salvage produces no restore event

- **WHEN** a node attempt fails but writes no owned files
- **THEN** no salvage event is emitted and no restore event is emitted on the next attempt
