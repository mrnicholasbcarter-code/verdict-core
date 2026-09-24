# Branch, worktree and PR reconciliation (BOD-190)

- **Date:** 2026-09-24
- **Method:** a read-only audit (`gh`, git ancestry and patch equivalence). Every removal was then
  re-verified by the controller.
- **Backup before removal:** a verified `git bundle` of all 69 local branch tips, plus patches for
  every dirty worktree, in `~/.verdict/archive/20260924T105006Z-bod190/`. Nothing unmerged was
  deleted.

## Result

| | Before | After |
|---|---:|---:|
| Local branches | 69 | 8 |
| Worktrees | 29 | 8 |
| Open PRs | 8 | 8 (decisions below) |

Removed:
- **52 local branches:** each one's PR was `MERGED` on GitHub, or the branch is an ancestor of
  `origin/main`. Their 11 worktrees were clean.
- **8 `wip/golden-n*` worktrees and branches:** each is an ancestor of
  `feat/interview-golden-path` (PR #590).
- **`feat/bod-124-bootstrap-rollback`:** superseded. Its PR #558 was closed unmerged, and rollback
  shipped in #557.
- **2 detached verification worktrees:** both at commits that are ancestors of `origin/main`.

## Remaining refs

| Ref | Status | Evidence / next step |
|---|---|---|
| `main`, `wip/pre-wave-existing-state` | KEEP | protected |
| `feat/interview-golden-path` | ACTIVE | PR #590 |
| `fix/omniroute-cx-models-list` | ACTIVE | Applies cleanly to main. Adds `cx/*` models to the Prime harness picker. Candidate PR. |
| `chore/core-usability-cleanup` | ACTIVE, overlaps #589 | Both delete `scripts/demo-routing.py` and `verdict/git_hooks.py` and rewrite `CLI_REFERENCE.md`. Folded into BOD-189. |
| `feat/bod-133-confinement` | SUPERSEDED (pending review) | Main confines reads with `O_NOFOLLOW` (#571), and #570 closed BOD-134/135. The cherry-pick conflicts only with the newer code. 3 small deltas remain to review: secret-regex quoting, gates-report acceptance, proxy outbound. |
| `feat/bod-136-live-execution` | MERGED (#570), dirty worktree | Patch archived. The operator reviews it, then the worktree is removed. |
| `feat/bod-17-ruflo-experimental` | MERGED (#554) | Its worktree holds only untracked `.serena/` and `.venv/`. |

## Open PRs

| PR | Recommendation |
|---|---|
| #574-#579 (dependabot) | All checks pass. Merge after owner approval, in one batch. |
| #589 (cursor docs/cleanup) | Keep `scripts/terminal_preview.py`. Its certification doc is self-reported. Superseded in scope by BOD-189; reconcile into it. |
| #590 (golden path) | CI in progress. Merge after checks and owner approval. |

## Remote branches

101 remote-tracking refs. Most are merged or superseded feature branches (see the audit tables).
Deleting remote branches is an owner decision and has not been done.


## Remote cleanup (2026-09-24, operator-authorized)

- A verified bundle of every remote branch was written first:
  `~/.verdict/archive/20260924T105006Z-bod190/all-remote-branches.bundle`.
- **79 remote branches deleted.** Each was re-verified: its PR was `MERGED` on GitHub and the
  head is an ancestor of `origin/main`, or it had no PR and is an ancestor of `origin/main`.
  List: `remote-deleted.json`.
- **Kept:** `main`, `feat/interview-golden-path`, and open Dependabot branches. Also kept are
  branches of closed-unmerged PRs and unmerged checkpoints. They hold unique history, and
  deleting them is a separate owner decision: `238-security-privacy-launch-gate`,
  `338-readme-pitch-accuracy`, `checkpoint/337-*`, `chore/runtime-daemon-consolidation-129`,
  `cursor/interview-hardening-bod-178-2d40` (PR #589, closed and folded into #590),
  `design-bod124-terminal-ui-spec`, `feat/106-*`, `feat/bod-124-bootstrap-rollback`,
  `feat/v1-002-route-selection`, `feat/ver-011-*`, `feature/004-*`, `portfolio/core-credibility`,
  `wip/guidance-control-plane-20260727`, `dependabot/pip/pytest-cov-gte-7.1.0` (its PR closed).

## PR outcomes

| PR | Outcome |
|---|---|
| #574, #575, #576, #579 | merged (all checks green, owner-authorized) |
| #577, #578 | conflicted after the other merges; `@dependabot rebase` requested |
| #589 | closed. Content merged into #590 at `0134485`; `scripts/terminal_preview.py` kept |
| #590 | open. Golden path plus hardening |
