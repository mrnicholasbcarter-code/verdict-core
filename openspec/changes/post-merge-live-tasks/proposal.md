# Post-merge live tasks

## Why
The definition of done (AGENTS.md, parts 1 and 5) needs live checks that run
on merged main, and it needs `tasks.md` to list and tick them. The required CI
step "Touched OpenSpec changes have no open tasks"
(`scripts/check_openspec_tasks.py`) fails any pull request whose touched change
has an unticked task. A live check that can only run after the merge therefore
blocks the merge. Earlier pull requests got round this by leaving the live
checks out of `tasks.md`, so "every task is ticked" passed without them.
The operator decided on 2026-11-06: "Add a post-merge marker".

## What Changes
- A task line written exactly `- [ ] (post-merge) <text>` may stay open when
  the pull request merges. `scripts/check_openspec_tasks.py` passes on it and
  lists it as pending. Every other unticked task still fails unless `--wip`.
- The marker is exact. `(post merge)`, `(postmerge)`, `(Post-merge)`, a marker
  after a task number, and a marker with no task text all count as ordinary
  open tasks.
- `scripts/check_dod.py` already fails an evidence file whose change is
  unarchived or has any unticked task. New tests pin that it does so for
  post-merge tasks too. Its code does not change.
- Archiving is checked only by `scripts/check_dod.py --all`: a change archived
  after the gate needs an evidence file, and that file fails while any task is
  open. Nothing new is added for archiving.
- `scripts/drill_gates.py` gets a drill that appends a near-miss marker, so
  the scheduled gate drills prove the marker stays strict.
- AGENTS.md "Definition of done", CONTRIBUTING.md and both scripts' docstrings
  describe the marker.

## Capabilities
### New Capabilities
- `repository-tooling`: the CI gates that keep OpenSpec task state and the
  definition of done honest. No capability under `openspec/specs/` owned
  these checks; the change that added them (`m1-stop-false-done`) used
  `skip_specs: true`.

### Modified Capabilities
None.

## Impact
`scripts/check_openspec_tasks.py`, `scripts/drill_gates.py`, the
`scripts/check_dod.py` docstring, their tests, AGENTS.md and CONTRIBUTING.md.
No product code under `verdict/` changes. Pull requests that list post-merge
live checks can merge. A story still cannot be done, or its change archived,
until those checks are ticked.
