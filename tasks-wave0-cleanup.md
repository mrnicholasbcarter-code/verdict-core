# Wave 0 Cleanup Tasks (Items 4 & 5)

## Phase 1: Setup

- [x] T001 Create working directory for cleanup artifacts in /home/nick/dev/cleanup-wave0/
- [x] T002 Fetch all GitHub issues #218-#248 with full details (title, body, labels, comments)
- [x] T003 Enumerate all local branches in verdict-core (git branch -a)
- [x] T004 Enumerate all worktrees across /home/nick/dev/verdict-core-worktrees, /home/nick/dev/.worktrees, /home/nick/dev/worktrees, /home/nick/dev/verdict-core-autodev

## Phase 2: Issue Triage (Item 4)

- [x] T005 [P] [US1] Audit issue #218 VER-001 Versioned TaskSpec intake contract — check if superseded by current implementation
- [x] T006 [P] [US1] Audit issue #219 VER-002 Deterministic DecisionKernel facade — check if superseded
- [x] T007 [P] [US1] Audit issue #220 VER-003 Universal immutable ExecutionEnvelope contract — check if superseded
- [x] T008 [P] [US1] Audit issue #221 VER-004 Provider adapter protocols and conformance harness — check if superseded
- [x] T009 [P] [US1] Audit issue #222 VER-005 Governed Ruflo agent-runtime and swarm adapter — check if superseded
- [x] T010 [P] [US1] Audit issue #223 VER-006 RuVector and OpenViking intelligence adapters — check if superseded
- [x] T011 [P] [US1] Audit issue #224 VER-007 OmniRoute and local execution-provider adapters — check if superseded
- [x] T012 [P] [US1] Audit issue #225 VER-008 Native runtime enforcement kernel — check if superseded
- [x] T013 [P] [US1] Audit issue #227 VER-010 Verification orchestrator and policy profiles — check if superseded
- [x] T014 [P] [US1] Audit issue #229 MEM-001 Memory governance, redaction, and verified-write gate — check if superseded
- [x] T015 [P] [US1] Audit issue #230 MOD-001 Policy-bounded model and slice assignment — check if superseded
- [x] T016 [P] [US1] Audit issue #231 PLG-001 Versioned plugin manifest and lifecycle contract — check if superseded
- [x] T017 [P] [US1] Audit issue #232 PLG-002 Plugin runtime host and sandbox enforcement — check if superseded
- [x] T018 [P] [US1] Audit issue #233 AUT-001 Autonomous Dev stage contracts and compiler — check if superseded
- [x] T019 [P] [US1] Audit issue #234 AUT-002 Governed Autonomous Dev executor — check if superseded
- [x] T020 [P] [US1] Audit issue #235 CTX-001 Provider-neutral shared Context Plane — check if superseded
- [x] T021 [P] [US1] Audit issue #236 NOD-001 Core DecisionKernel API for Node delegation — check if superseded
- [x] T022 [P] [US1] Audit issue #237 DX-001 CLI explain, init, local defaults, and integration harness — check if superseded
- [x] T023 [P] [US1] Audit issue #238 LAUNCH-001 Cross-repository security and privacy launch gate — check if superseded
- [x] T024 [P] [US1] Audit issue #248 feat(probing): qualify concrete models individually — check if superseded
- [x] T025 [US1] Bulk-close superseded issues via GitHub API with rationale comments
- [x] T026 [US1] Re-label actionable issues with updated priority/area labels

## Phase 3: Branch Pruning (Item 5)

- [x] T027 [P] [US2] Classify all 178 branches by: last commit date, merge status (merged/abandoned/active), purpose
- [x] T028 [P] [US2] Delete local merged branches (git branch -d)
- [x] T029 [P] [US2] Delete remote merged branches (git push origin --delete)
- [x] T030 [P] [US2] Delete abandoned branches (no commits in 90+ days, no open PR)
- [x] T031 [US2] Clean stale worktrees: remove directories for deleted branches
- [x] T032 [US2] Run `git worktree prune` to clean up orphaned worktree references

## Phase 4: Documentation

- [x] T033 [US3] Create BRANCH_REGISTRY.md in verdict-core root documenting retained branches with owner, purpose, last activity
- [x] T034 [US3] Update con-001-tasks/STATE.md to reflect completion of items 4 & 5

## Verification

- [x] T035 Verify: `gh issue list --state open --repo mrnicholasbcarter-code/verdict-core | grep -E "21[89]|2[2-4][0-9]"` returns only actionable issues
- [x] T036 Verify: `git branch -a | wc -l` < 50 (from 178)
- [x] T037 Verify: `ls /home/nick/dev/verdict-core-worktrees | wc -l` < 10 (from 38)
- [x] T038 Verify: BRANCH_REGISTRY.md exists with all retained branches documented

## Parallel Opportunities

- All issue audits (T005-T024) are fully parallel — different issues, no dependencies
- Branch classification (T027) can run in parallel with issue triage
- Branch deletions (T028-T030) parallelizable once classification complete

## MVP Scope

User Story 1 (Issue Triage) + User Story 2 (Branch Pruning) — both independent, can run concurrently. User Story 3 (Documentation) depends on both.