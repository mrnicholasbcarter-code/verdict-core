# Spec Delta

## Purpose
CI gates for this repository that keep OpenSpec task state and the executable
definition of done honest, so a change cannot merge, be called done or be
archived on unfinished work.

## ADDED Requirements

### Requirement: Post-merge tasks may stay open at merge time
The OpenSpec task check MUST treat an unticked task as post-merge only when its
line, after optional indentation, starts with exactly `- [ ] (post-merge) `
followed by task text that does not start with whitespace. The marker MUST be
lower case and directly after the checkbox, with one space on each side. Any
other unticked task, including one with a near-miss marker, is an ordinary open
task. A touched active change whose only unticked tasks are post-merge MUST
pass, and the check MUST list those tasks in its output. A touched active
change with any ordinary open task MUST still fail unless the pull request is
marked work in progress.

#### Scenario: Only post-merge tasks are open
- **WHEN** a pull request touches a change whose only unticked task is
  `- [ ] (post-merge) run the live check on merged main`
- **THEN** the task check exits 0 and lists that task as post-merge
- **TEST** `tests/test_check_openspec_tasks.py::test_open_post_merge_task_passes_and_is_listed`

#### Scenario: An ordinary open task still fails
- **WHEN** the same change also has an unticked task without the marker
- **THEN** the task check exits 1 and names that task as open
- **TEST** `tests/test_check_openspec_tasks.py::test_plain_open_task_beside_a_post_merge_task_fails`

#### Scenario: Near-miss markers do not count
- **WHEN** an unticked task carries `(post merge)`, `(postmerge)`,
  `(Post-merge)`, `(post-merge)` after a task number or elsewhere in the line,
  extra spaces around the marker, or the marker with no task text
- **THEN** the task check exits 1 and names the task as open
- **TEST** `tests/test_check_openspec_tasks.py::test_near_miss_marker_is_an_open_task`

#### Scenario: Work in progress still skips the failure
- **WHEN** the pull request is marked work in progress
- **THEN** the task check exits 0 whatever tasks are open
- **TEST** `tests/test_check_openspec_tasks.py::test_touched_change_with_open_task_fails`

### Requirement: Done and archive need every task ticked, post-merge included
The definition-of-done check MUST fail an evidence file whose OpenSpec change
is still active, or whose archived `tasks.md` has any unticked task, post-merge
tasks included. When it checks every evidence file, it MUST also fail on a
change archived after the gate that no evidence file names. A change archived
with an open post-merge task therefore fails the definition-of-done check.

#### Scenario: An open post-merge task blocks done
- **WHEN** an evidence file names an archived change whose `tasks.md` still has
  `- [ ] (post-merge) run the live check on merged main`
- **THEN** the definition-of-done check reports the change has unticked tasks
- **TEST** `tests/test_check_dod.py::test_open_post_merge_task_is_not_done`

#### Scenario: An active change with a post-merge task is not done
- **WHEN** an evidence file names a change that is still active, with only a
  post-merge task open
- **THEN** the definition-of-done check reports the change is not archived
- **TEST** `tests/test_check_dod.py::test_open_post_merge_task_is_not_done`

#### Scenario: Archiving with an open post-merge task fails the full check
- **WHEN** a change is archived with an open post-merge task, with or without
  an evidence file that names it
- **THEN** `check_dod.py --all` exits 1
- **TEST** `tests/test_check_dod.py::test_archive_with_open_post_merge_task_fails_all`
