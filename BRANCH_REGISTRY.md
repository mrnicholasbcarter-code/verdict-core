# Branch Registry

Maintained as part of Wave 0 cleanup (items 4 & 5), last updated 2026-08-20.

Prune policy applied: local/remote branches whose PR merged (103 merged PRs
audited via `gh pr list`) or that were ancestors of `origin/main` were deleted;
no branch under 90 days old without a merged/closed PR was deleted. 52 stale
worktrees were removed (tracked-change salvage in
`/home/nick/dev/cleanup-wave0/worktree-salvage/`). Starting state: 178
`git branch -a` entries, 65 registered worktrees. End state: 30 entries,
13 worktrees.

## Active branches (do not prune)

| Branch | Owner / context | Worktree | Last activity |
|---|---|---|---|
| `main` | default branch (local ref lags `origin/main`; repo dir is flagged `bare = true` — see anomalies) | — (bare) | 2026-08-19 (origin) |
| `feat/v1-002-route-selection` | agent wave1-v1-002 (Wave 1 #265 V1-002) — explicitly reserved by owner, DO NOT PRUNE | `/home/nick/dev/wt-265-v1-002` | 2026-08-19 |
| `feat/nod002-envelope-parity` | NOD-002 envelope parity (PR merged; worktree still active) | `/home/nick/dev/verdict-core-nod002` | 2026-08-20 |
| `feat/ver-003-execution-envelope` | VER-003 execution envelope follow-on | `/home/nick/dev/worktrees/core-ver-003` | 2026-08-19 |
| `feat/mem-infinite-context` | memory infinite-context work | `/home/nick/dev/verdict-core-worktrees/mem-infinite-context` | 2026-08-18 |
| `feat/core-220-execution-envelope` | issue #220 envelope work (PR merged; worktree recently active) | `/home/nick/dev/verdict-core-worktree` | 2026-08-18 |
| `feat/ver-001-taskspec` | VER-001 TaskSpec (PR merged; worktree recently active) | `/home/nick/dev/verdict-core-ver001` | 2026-08-15 |
| `portfolio/core-credibility` | portfolio track (also on origin) | `/home/nick/dev/verdict-portfolio-worktrees/core-credibility` | 2026-08-15 |
| `portfolio/trusted-change-contract` | portfolio track | `/home/nick/dev/verdict-portfolio-worktrees/trusted-change-contract` | 2026-08-15 |
| `feat/autodev-v0.1` | Autonomous Dev v0.1 (unmerged) | `/home/nick/dev/verdict-core-autodev` | 2026-08-06 |
| `port/264-v1-001-foundation` | V1-001 foundation port (#264) | `/home/nick/dev/verdict-core-v1` | 2026-08-04 |

## Dormant unmerged branches (review before next prune)

No PR, no merge into main; retained because they are under 90 days old.
Candidates for deletion at the next cleanup pass if still untouched.

| Branch | Purpose (inferred) | Worktree | Last commit |
|---|---|---|---|
| `feat/115-evidence-contracts` | evidence contracts spike (issue #115 landed via `codex/115-evidence-authority-origin`) | — | 2026-07-30 |
| `chore/runtime-daemon-consolidation-129` | runtime daemon consolidation (#129 landed via `codex/129-runtime-owner`) | — | 2026-07-27 |
| `feat/document-ingestion-adapter-130` | document ingestion adapter (#130 landed via `codex/130-memoryplane-adapters`) | `/home/nick/dev/verdict-core-document-adapter` | 2026-07-27 |
| `feat/memory-plane-126` | memory plane spike (#126 landed via `codex/126-memorygate-evidence`) | — | 2026-07-27 |
| `codex/issue-107-guidance-boundary` | guidance boundary (#107, ADR-003 landed) | `/home/nick/dev/verdict-core-guidance-implementation` | 2026-07-27 |
| `wip/guidance-control-plane-20260727` | same head as `codex/issue-107-guidance-boundary` (9a035e8) | — | 2026-07-27 |
| `chore/generated-artifact-hygiene` | generated-artifact hygiene | — | 2026-07-27 |
| `codex/capacity-planner` | capacity planner experiment | — | 2026-07-18 |
| `codex/continuation-runbook` | continuation runbook experiment | — | 2026-07-18 |
| `codex/availability-hardening` | availability hardening experiment | — | 2026-07-18 |
| `codex/eligibility-invariant` | eligibility invariant experiment | — | 2026-07-18 |
| `codex/static-quality` | static quality experiment | — | 2026-07-18 |

## Remote-only branches retained

| Branch | Reason retained |
|---|---|
| `origin/feat/ver-011-evidence-chain` | VER-011 (#228) closed complete 2026-08-16; no local checkout; verify landed content before deleting |
| `origin/feat/106-capability-passport-v1` | PR closed unmerged; superseded by `codex/167-capability-passport-v1`; delete at next pass |
| `origin/portfolio/core-credibility` | active portfolio track |
| `origin/chore/runtime-daemon-consolidation-129` | matches dormant local branch above |
| `origin/wip/guidance-control-plane-20260727` | matches dormant local branch above |

## Anomalies

- `/home/nick/dev/verdict-core/.git/config` has `bare = true` even though the
  directory contains a full working tree. `git status`/commits fail from the
  main checkout; worktrees function normally. Left untouched during cleanup —
  needs a deliberate fix (`core.bare=false`) after confirming no tooling
  depends on it.
- Local `main` ref (92bd364, 2026-07-28) lags `origin/main` (d6a6cf8,
  2026-08-19); not fast-forwarded during cleanup because `main` is not checked
  out anywhere and updating refs was out of scope.
- `/home/nick/dev/verdict-memory-native` is a detached-HEAD worktree of this
  repo outside the cleanup scope list; left untouched.
