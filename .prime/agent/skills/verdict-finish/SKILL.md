---
name: verdict-finish
description: Use when Verdict proof is complete and work needs review, PR/CI tracking, merge verification, or durable closeout.
---

# Verdict finish

Read `docs/guides/prime-workflow.md`. All transitions bind the exact source; source changes
invalidate proof/review/CI and require revalidation. Never trust a stale checkpoint's DONE.

1. Obtain independent review of acceptance compliance and code quality for the proof head.
   Resolve actionable findings. Require reviewer identity, APPROVED decision, head SHA and
   review artifact. Use `documentation-and-adrs` for new/changed architectural decisions and
   shipped behavior; verify linked ADR/spec/docs changes. Advance to REVIEW_APPROVED only then.
2. Push only the reviewed issue branch. Call the proof-gated `open-pr` helper with proof,
   review, title, body file and `--state-dir`; it additionally enforces `require_lease_for_pr`,
   so a fenced or stale writer cannot open or update the PR. Reuse an existing PR for that branch. Include AC evidence,
   validation limits and Linear issue link. Verify GitHub's returned base/head and PR URL;
   then checkpoint PR_OPEN. A failed/uncertain API write must be reconciled before retry.
3. Query GitHub required checks and all check runs/statuses for the exact PR head. Paginate.
   Classify with `classify_ci` / `classify_merge_state` and route with `ci_recovery_action`:
   CODE_FAILURE returns to IMPLEMENTING in the same owned worktree, INFRA_FAILURE/CANCELLED retry
   CI without code changes, CONFLICT/BEHIND rebase and reprove, and exhausted attempts become
   BLOCKED. Wait only within the packet's CI budget; zero checks or missing required checks are
   not green. Require all required results and the repo's stricter checks policy; failing,
   pending, cancelled or stale results block CI_GREEN. Detect new pushes and reset validation.
4. Apply current repo merge authority and branch protection. Verify mergeability and approved
   exact head immediately before merging, use head-matched merge, and re-fetch the resulting
   GitHub merge state/commit. If authorization is absent, persist an actionable approval need;
   do not repeatedly ask where authorization already exists. Only observed merge enters MERGED.
5. Fetch main. Verify the merge commit is an ancestor of fetched origin/main, then verify the
   intended behavior on an isolated clean main checkout at the observed SHA. Record commands,
   results and that SHA; CI on the feature branch alone is insufficient. Main failure stays
   MERGED with a repair blocker. Do not undo unrelated main changes or mark Linear Done.
6. After MAIN_VERIFIED, update Linear with PR, merge/main SHAs and evidence references; re-read
   it to confirm. Atomically checkpoint DONE and next_action=verdict-resume. If the update
   fails, retry only the idempotent closeout after reconciliation, never implementation.
7. Release the owned lease (`release_lease` with its generation), preserve evidence and worktrees
   per repo policy, and invoke verdict-resume for the next dependency-READY issue. Compact between completed issues after
   checkpointing when context budget requires it. Stop only at a recorded run boundary.
