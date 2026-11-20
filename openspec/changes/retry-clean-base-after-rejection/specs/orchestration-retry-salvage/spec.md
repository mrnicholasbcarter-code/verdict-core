## ADDED Requirements

### Requirement: REJECTED attempts start from the clean base on retry

When a DAG node attempt is rejected — either because an independent checker judged the
implementation wrong (`verification_failed`, `diff_rejected`) or because the terminal carries
`validated=True` — the runtime SHALL NOT restore salvaged files from the previous attempt into
the next attempt's worktree.  The salvage directory for that node SHALL be cleared (not saved) so
that stale rejected content cannot contaminate the next attempt.  The restore event emitted for
the next attempt SHALL carry `reason: "clean_base"` and an empty `restored` list.

`diff_rejected` failures are classified with `category="diff_rejected"`,
`action="CORRECT_IMPLEMENTATION"`, `cooldown_seconds=0`, and `scope="none"`.  The route is not
at fault; the fix must come from a corrected implementation.

#### Scenario: verification-rejected attempt, next attempt starts from clean base

- **WHEN** a node attempt returns `ok=True` but its verification command exits non-zero
- **THEN** the runtime records `validated=True` and sets `last_rejected=True` for that node run
- **AND** `_fail()` clears the node's salvage directory instead of saving partial work
- **AND** on the next attempt `_attempt()` emits a restore event with `reason="clean_base"`, `restored=[]`, `restored_count=0`
- **AND** the next attempt's worktree contains only the base-revision files, not the rejected implementation

#### Scenario: diff_rejected attempt, next attempt starts from clean base

- **WHEN** a node attempt's terminal has `error` starting with `"diff_rejected"`
- **THEN** the failure is classified as `category="diff_rejected"` and `last_rejected=True` is set for that node run
- **AND** no salvage is saved; the next attempt starts from the clean base
- **AND** the restore event carries `reason="clean_base"`

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
