# Spec Delta

## ADDED Requirements

### Requirement: A node's commit contains the git-ignored files the node owns

When the runtime stages a node attempt's changes, it SHALL also stage every untracked, git-ignored regular file that lies under one of the node's owned paths (a file or a directory), so that the node's commit and every dependent node's worktree contain it. It SHALL NOT stage a byproduct file found under an owned directory, nor a file that matches a byproduct file pattern (`*.pyc`, `.coverage`) even when that file is named explicitly as an owned file, and SHALL NOT stage an ignored file outside the node's owned paths. An owned path that itself lies under a byproduct directory (for example a file under `.verdict/`) is staged by its literal path, as before this change. When the listing of candidate files may be truncated, the runtime SHALL refuse to stage from it.

#### Scenario: owned ignored file reaches the dependent node

- **WHEN** a node owns `fixtures/x.jsonl`, the repository ignores `*.jsonl`, and the node's worker creates that file
- **THEN** the node's commit contains `fixtures/x.jsonl`
- **AND** a node that depends on it finds the file in its worktree

#### Scenario: byproducts under an owned directory

- **WHEN** a node owns the directory `fixtures` and it holds `fixtures/x.jsonl` and many byproduct files such as `fixtures/__pycache__/m.pyc`
- **THEN** `fixtures/x.jsonl` is staged and no byproduct file is
- **AND** the byproduct files do not make the listing too long to use

#### Scenario: ignored file outside the owned paths

- **WHEN** an ignored file exists outside every owned path of the node
- **THEN** it is not staged

#### Scenario: listing that may be truncated

- **WHEN** the listing of ignored files under the owned paths is 4,000 characters or longer
- **THEN** staging raises an error and no file from that listing is staged

### Requirement: An owned ignored file that cannot be committed fails the attempt

An owned git-ignored file that is a symlink, is not a regular file, cannot be read, or whose content looks like a credential SHALL NOT be staged. For an implement node the attempt SHALL fail: the `ownership` barrier event SHALL be not ok and carry the refused paths with a reason for each, and the worker SHALL receive feedback that starts with `verification_failed: owned_file_not_committable:`. Neither an event nor the feedback SHALL contain file content.

#### Scenario: credential-shaped owned file

- **WHEN** an implement node's worker writes an owned ignored file whose content looks like a credential
- **THEN** the file is not staged and the node is not VALIDATED on that attempt
- **AND** the `ownership` barrier event has `ok` false and `refused_paths` naming the file with the reason `credential_shaped`
- **AND** the failure is recorded as `verification_failed` with `owned_file_not_committable` in its evidence

#### Scenario: the worker corrects the file on the next attempt

- **WHEN** the next attempt writes the owned ignored file without credential-shaped content
- **THEN** the node is VALIDATED and the last `ownership` barrier event of the run is ok

### Requirement: The VALIDATED event names the force-staged paths

When at least one git-ignored file was staged for a node attempt that is then VALIDATED, the `node_state` event for that state SHALL carry a `force_staged` list of those paths. When none was, the event SHALL NOT carry the field.

#### Scenario: node with an ignored fixture

- **WHEN** a node is VALIDATED after `fixtures/x.jsonl` was force-staged
- **THEN** its VALIDATED `node_state` event has `force_staged` equal to `["fixtures/x.jsonl"]`

#### Scenario: node with ordinary files only

- **WHEN** a node is VALIDATED and no ignored file was staged
- **THEN** its VALIDATED `node_state` event has no `force_staged` field

### Requirement: A committed ignored file is made visible to ignore-aware tools

When a node is VALIDATED with force-staged files, the runtime SHALL, before it commits, add one negation line (`!<path>`) to the repository's root `.gitignore` for each force-staged file that such a line un-ignores, and SHALL stage `.gitignore` with the commit, so that the committed file no longer matches an ignore rule. It SHALL add the line only after the ownership check and the verification command have passed, SHALL NOT add a line that is already present, and SHALL NOT count its own edit of `.gitignore` as a change by the node. A file that is still ignored after its negation line (for example because a parent directory is ignored), or whose path cannot be written safely as a `.gitignore` pattern, SHALL stay force-staged without a negation line. The VALIDATED `node_state` event SHALL list the paths that received a negation line in `unignored`, and the paths that stayed without one remain visible as the difference from `force_staged`.

While the run merges node commits, `.gitignore` SHALL be merged with a union merge, configured in the repository's local git attributes and never committed, so that two nodes that each added a negation line do not conflict.

A worker's own edit of `.gitignore` is unaffected: it is a changed file like any other and is subject to the ownership check.

#### Scenario: fixture matched by a pattern

- **WHEN** a node owns `tests/fixtures/a/sample.jsonl`, the repository ignores `*.jsonl`, and the node is VALIDATED
- **THEN** the node's commit contains the file and a `.gitignore` line `!tests/fixtures/a/sample.jsonl`
- **AND** `git check-ignore` no longer reports the file
- **AND** the VALIDATED event has the path in both `force_staged` and `unignored`

#### Scenario: file under an ignored directory

- **WHEN** a node owns `build/deep/out.txt` and the repository ignores `build/`
- **THEN** the file is committed, no negation line is added, and the VALIDATED event has the path in `force_staged` only

#### Scenario: two nodes each add a fixture

- **WHEN** two nodes that do not depend on each other each commit an ignored fixture with its negation line
- **THEN** the run's integration merge succeeds and `.gitignore` contains both lines

#### Scenario: worker edits .gitignore itself

- **WHEN** a worker changes `.gitignore` and the node does not own it
- **THEN** the attempt fails with `ownership_violation`, as before

