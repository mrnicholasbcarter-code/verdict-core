# Design

## Context
- `scripts/check_openspec_tasks.py` finds unticked tasks with
  `^\s*- \[ \]\s*(.+)$` (multi-line) in every active change the branch touched,
  and fails on any unless `--wip`. CI passes `--wip` when the pull request has
  the `wip` label. Archived changes are skipped.
- `scripts/check_dod.py` `_openspec()` fails when the change is still under
  `openspec/changes/`, and fails on any `^\s*- \[ \]` line in the archived
  `tasks.md`. `--all` also fails on a change archived after the gate that no
  evidence file names. So it already rejects an open post-merge task, both
  for "done" and for "archived".
- Nothing else checks archiving in this repository. The OpenSpec CLI's own
  `archive` command warns on incomplete tasks, counts post-merge tasks among
  them, and proceeds with `--yes`. `verdict.openspec_lifecycle.can_archive_openspec_change`
  only trusts its caller's flag and has no production caller
  (`docs/openspec/LIFECYCLE.md`).
- `scripts/drill_gates.py` (scheduled `gate-drills.yml`) proves daily that an
  appended plain open task fails the task check.

## Goals / Non-Goals

**Goals:**
- List post-merge live checks in `tasks.md` and still merge.
- Keep every other open task blocking, and keep the marker strict.
- Keep "done" and "archived" impossible while any task is open.

**Non-Goals:**
- Requiring a change to have at least one post-merge task. That is a separate
  operator decision.
- A new archive gate, or changes to `can_archive_openspec_change`.
- Relabelling tasks in other branches.

## Decisions
- **Exact line prefix.** A post-merge task matches
  `\s*- \[ \] \(post-merge\) \S` at the start of the unticked-task match. It
  is case sensitive and takes one space on each side of the marker. The marker
  goes before any task number: `- [ ] (post-merge) 4.1 Run ...`. Indentation
  before `- [ ]` is allowed because the existing pattern counts indented tasks
  as open too.
- **Classify, do not re-detect.** Open tasks are still found with the existing
  `UNTICKED` pattern, then each match is split into post-merge or blocking. A
  line the old pattern counted as open is never dropped, so no open task can
  disappear from the check.
- **Show the pending tasks.** Post-merge tasks are printed under their change
  as `N post-merge task(s) open until after the merge`, so they stay visible.
- **`check_dod.py` code stays as it is.** Its pattern already counts post-merge
  lines. Tests pin that, and its docstring says so.
- **One drill.** `openspec-near-miss-marker` appends
  `- [ ] (post merge) drill: near-miss marker` and expects the task check to
  name it, so the scheduled job catches a loosened marker.

Alternatives considered:
- A `## Post-merge` heading whose tasks are exempt. Rejected: a heading is
  easy to misplace, and the operator chose a per-task marker.
- A case-insensitive or spacing-tolerant marker. Rejected: the operator asked
  for an exact marker, and near misses must not pass.

## Test strategy
- `tests/test_check_openspec_tasks.py`, in a temporary git repository:
  post-merge only passes and is listed; a plain task beside it fails; each
  near miss fails (parametrized); `--wip` still passes.
- `tests/test_check_dod.py`: an archived change with an open post-merge task
  is not done; an active one is "not archived"; `--all` fails on an archive
  with an open post-merge task, with or without evidence. These pass on the
  current code. A temporary change to the `check_dod.py` pattern that skips
  post-merge lines must turn them red.
- `tests/test_drill_gates.py` covers the new drill's gate; `scripts/drill_gates.py`
  runs locally with the pinned OpenSpec CLI.

## Risks / Trade-offs
- An author could mark a pre-merge task post-merge to get past CI. Review
  catches that, and the story still cannot be done or archived until the task
  is ticked with real evidence.
- A change whose tasks are all post-merge passes the task check. Whether a
  change must have at least one live task is not decided here.
