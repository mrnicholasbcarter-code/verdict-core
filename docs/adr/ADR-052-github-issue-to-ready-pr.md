# ADR-052: Verdict builds a GitHub issue through to a ready PR

**Status:** Accepted — every decision agreed with the operator (2026-11-23, three rounds); not yet implemented
**Date:** 2026-11-23
**Deciders:** Operator
**Relates to:** [ADR-036](ADR-036-goal-to-receipt-orchestration.md) (goal-to-receipt orchestration), [ADR-049](ADR-049-cost-policy-and-unknown-handling.md) (cost policy), [ADR-051](ADR-051-planner-frontier-from-ratings.md) (frontier planner), [AGENTS.md](../../AGENTS.md) (release train, Definition of Done)

## Context

`verdict orchestrate --openspec-change ID` runs plan → task graph → workers → review → receipt, and stops at a run-internal integration commit. Today the controller pushes the branch, opens the PR, watches CI and does the post-merge work.

One real story finished this way on 2026-11-20 (PR #1191), after the operator hand-tuned the goal over attempts 7–15.

The backlog is GitHub Issues, labelled `type/story`, `size:*`, `horizon:*` and `operator-only`. Research (2026-11-23) compared GitHub Copilot cloud agent controls from primary sources: docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent and docs.github.com/en/copilot/concepts/security-governance-and-network-settings/risks-and-mitigations. Under those docs, only users with write access trigger the agent; it pushes to its own branch under branch protection; it opens draft PRs and cannot approve or merge; hidden characters in issue text are filtered; fixing red CI ("Watch and fix") is opt-in. OpenHands and SWE-agent were compared from secondary sources only.

## Decisions

The operator agreed every decision below on 2026-11-23, in three rounds of researched options.

1. **The operator names the issue (agreed, round 1).** `verdict build-issue N` accepts an issue only if:
   - it is labelled `type/story` and not `operator-only`;
   - the operator opened it;
   - it has no open dependencies;
   - it has `## Goal` and `## Acceptance check` sections.

   Otherwise it refuses with a named reason. Verdict never picks an issue on its own.
2. **Spec draft first, then operator approval (agreed, round 1).** Verdict drafts an OpenSpec change from the issue (proposal, design, tasks, spec delta) and opens it as a design PR. Building starts only after the operator approves or merges that PR.
3. **Draft PR, marked ready when green (agreed, round 1).** Verdict pushes through vgate to `verdict/issue-N-<slug>` and opens a draft PR that links the receipt. It marks the PR ready only when two things hold: a cross-family review has passed, and CI is green. The operator merges. Verdict never approves or merges.
4. **Red CI gets a bounded fix loop, 2 tries (agreed, round 1).** Verdict reruns known-flaky jobs. For a real failure, it feeds the failing log to a repair run and pushes the fix. It stops after 2 fix attempts and hands over with the evidence.
5. **Issue text is untrusted data (derived from decision 1 and AGENTS.md).** Only parsed sections (Goal, Outcome, Acceptance check) form the goal. Hidden content (HTML comments, zero-width characters) is stripped before anything reaches a model, as GitHub's filter does. Issue text is never interpolated into a shell command.
6. **Credential: the existing `gh` login (agreed, round 2).** Verdict uses the `gh` authentication already on this host. It needs no new token. Its actions show as the operator. Every push still goes through vgate, and Verdict never merges.
7. **Reviewer: a cross-family review before ready (agreed, round 2).** If the in-run reviewer fell back to the same model family, Verdict runs an extra OpenCodeReview pass on the PR from another family. If no other family is available, the PR stays draft and Verdict tells the operator.
8. **Trial story: #1214 L1g (agreed, round 2).** The first story through the pipeline is #1214: a pre-dispatch check for test runner, scope and owned files, the failure class behind real-story attempts 7–11.

9. **Post-merge: reinstall, check, draft the DoD file (agreed, round 3).** Verdict reinstalls installed main outside the :00/:20 runs, runs the story's acceptance check live, drafts the DoD evidence file from the raw output, and reports what passed. Confirmation by a non-author (DoD part 7) and committing the DoD file stay with the operator or another agent.
10. **Build delivery first (agreed, round 3).** Order: (1) `deliver` (push, draft PR, the ready gate with the extra cross-family review); (2) `build-issue` intake; (3) spec drafting; (4) the CI fix loop; (5) the trial on #1214.
11. **pr-watch moves to GitHub in the CI-loop story (agreed, round 3).** Status goes to the GitHub issue and PR instead of Linear.
12. **Verdict builds the pipeline itself (agreed, round 3).** Once a story's design is merged, Verdict builds it with `verdict orchestrate --openspec-change`. Until `deliver` exists, the controller pushes the branch and opens the PR by hand.

## Consequences

- Verdict takes work only when the operator names an issue. Merging stays with the operator.
- Each story needs two operator touches: approving the design PR and merging the build PR.
- Using the existing `gh` login means Verdict's GitHub actions run with the operator's full token scope, so they appear as the operator's own. The guardrails are vgate on every push, never approving or merging, and the bounded fix loop.
- The first stories are built through `verdict orchestrate` while delivery is still manual, so gaps in Verdict's own build path show up early.
