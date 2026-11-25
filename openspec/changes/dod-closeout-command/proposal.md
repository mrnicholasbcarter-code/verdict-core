# DOD1: `verdict closeout`, the closeout command for the scheduled Definition of Done

## Why

Issue #1308: none of the 14 stories merged 2026-11-20 to 11-24 is done, and the one measurable case took 66
hours from merge to evidence; hand-run closeouts twice recorded wrong evidence. Free capacity becomes accepted
work only when a merged story is proven on the real setup ([ETHOS.md](../../../ETHOS.md)). This is **story 1 of
3** of the research's split, design only: the closeout command. Story 2 is the timer, setup step, doctor row
and reinstall; story 3 is GitHub status and the evidence pull request.

## What Changes

- New `verdict closeout run | status | reset` (`verdict/closeout/`). For each declared merged story it checks
  installed main (real SHA, clean, anchored on the acceptance unit), resolves the merge commit by a read-only
  `gh` call and refuses one that is not an ancestor, runs the story's allowlisted `argv` from installed main,
  stores scrubbed raw output, re-runs after the freshness window and assembles a draft evidence file outside
  the repository. It never marks a story done, waives the window or writes `parent_confirmation`, and never
  writes evidence for a run that did not happen.
- New `proof/closeout.schema.json` for declarations at `proof/closeout/<story>.yaml` (decision 7): `argv` list,
  `expect`, window of at least 1 hour (decision 9), author as an agent identity (decision 3).
- `scripts/check_dod.py --draft --evidence-root <dir>` skips only the confirmation and archive checks (decision
  10); strict mode and `--all` are unchanged.
- Everything written is under `$VERDICT_HOME/closeout/` (depends on #1306; its guard is PR #1311).

## Capabilities
### New Capabilities
- `dod-closeout`: declarations, installed-main identity, allowlist, run records, ledger, window, drafts, writes.

### Modified Capabilities
- `repository-tooling`: `check_dod.py` gains a draft mode.

## Impact

Not designed here. **Story 2** (decisions 2, 5): user timer, setup step, doctor row, reinstall with guard and
rollback, skipping a tick while a lock is held. **Story 3** (decision 4, second half of 10, recording decision
3): label, comment, confirmation record, evidence pull request. **No story yet**: live runs (decisions 1b, 6);
`live: true` is refused. **Unassigned**: backfilling declarations for the open #1289 rows. The command runs by
hand until story 2.

## Evidence boundary

- **KNOWN [main]:** `check_dod.py` returns at the first schema error, reads outputs under its root, has no draft
  mode; `redact_text` and `verdict_home()` exist. **KNOWN [host]:** the acceptance unit's working directory is
  `~/.local/share/verdict/installed-main`, clean and detached.
- **INFERRED:** `doctor` and `eligibility --no-refresh` write nothing (task 1.2); `gh pr view --json
  state,mergeCommit` gives the squash commit (verified on #1238 in the research).
- **NOT AVAILABLE:** a real run of the command; #1311's guard API (unmerged); reading a live run's routes back.

## Acceptance criteria

- With a fixture story, fake installed main and temporary home: the first run records the installed SHA, exit
  code and output digest; a run before the window spawns nothing; one after it records a fresh run; a failing
  command leaves the story open with its output; a missing or non-ancestor merge commit is refused.
- The draft passes `check_dod.py --draft`, fails it when the fresh run is too early, and fails the strict check
  on exactly the confirmation and the archive; a run that did not execute yields no draft.
- Only allowlisted argument lists run, only five kinds of process spawn, and every file written is under the
  closeout home. Both OpenSpec validators, lint gates and reachability pass.
- (post-merge) On installed main one real merged story has a first run, a fresh run and a draft that passes
  `--draft`, waiting for confirmation.
