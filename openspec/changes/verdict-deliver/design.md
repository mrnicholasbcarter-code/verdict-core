# Design — verdict deliver

## Context

See [ADR-052](../../../docs/adr/ADR-052-github-issue-to-ready-pr.md), decisions 3, 6, 7 and 10. `verdict orchestrate` stops at an integration commit plus a receipt. This change adds the step that turns that into a PR ready for the operator to merge. Intake (`build-issue`), spec drafting and the CI fix loop are separate stories.

## Goals / Non-Goals

Goals:
- one command from a verified run to a draft PR;
- a ready gate that enforces cross-family review and green CI on the exact head;
- named fail-closed reasons;
- a delivery record.

Non-goals:
- fixing red CI (the CI-loop story);
- choosing or reading issue text beyond its number and title (the intake story);
- merging;
- closing issues;
- changing vgate.

## Decisions

1. **Preconditions.** All of these must hold:
   - `verify_run_receipt(run_dir, repair=False) == []`;
   - the run outcome is `COMPLETE`;
   - the integration commit exists in the repo;
   - `gh issue view N` succeeds;
   - `gh auth status` shows a login that can push to and create PRs on the repository.

   Any failure stops before a push.
2. **Branch.** The branch is `verdict/issue-<N>-<slug>`. The slug is made from the issue title: lower-case `[a-z0-9-]`, at most 40 characters, with any other character dropped. The title is untrusted data and is never passed to a shell. Verdict pushes with `git push --force-with-lease=refs/heads/<branch>: origin <integration-sha>:refs/heads/<branch>`, through subprocess with an argument list. The lease names the one ref and gives it an empty expected value, which git defines as "the ref must not already exist" (git-push documentation), so the push is create-only. It is checked against the ref advertisement and again by the remote when it applies the update, which closes the window between the existence check and the push: a branch another writer created in that window, even at an ancestor of our commit, is never moved. It is not a force-push of anything, because a lease expecting an absent ref can only create it. The pre-push vgate runs; a non-zero exit is `vgate_rejected`, recorded with the tail of the gate output. If the branch already exists remotely and points at a different commit, or appears during the push, Verdict refuses with `branch_exists`. A branch already at the same commit is reused without a push. It never overwrites a branch.
3. **Draft PR.** Verdict runs `gh pr create --draft --base main --head <branch>`. An open PR for the branch is reused only when it is still a draft. One that is already ready, or whose draft state cannot be read, stops delivery (`pr_not_draft`, `pr_lookup_failed`), so a PR that is ready is never recorded as not ready. The body is built by Verdict, not by a model, and contains:
   - the OpenSpec change summary;
   - `Refs #N` (never a closing keyword: the operator decided issues are not closed automatically);
   - the run id;
   - the events digest;
   - the change id;
   - the reviewer evidence.
4. **Ready gate.** The gate works in this order:
   1. Derive the in-run review level. The run events record family-level or route-level, but that record is a claim: it counts as family-level only when the receipt also names the reviewer route and the reviewer's family is none of the implementer families. Otherwise the review is route-level. A passing review whose reviewer route is empty is refused (`review_failed`).
   2. If it was route-level, run OpenCodeReview on the PR diff (base...head) with every implementer family excluded. This uses the existing review invocation in `review.py`, so the same isolation applies.
   3. If no reviewer from another family is admissible, the PR stays draft with `no_cross_family_reviewer`. A failed or empty review gives `review_failed`, because a skipped review is an error.
   4. Wait for the required checks on the exact head SHA, with a bounded deadline (`--ci-deadline`, a finite number of minutes above zero, default 60). Every page of check runs is read; a server that reports more runs than it served is never green. Check runs and legacy commit statuses are two mechanisms: one with no evidence is neutral when the other is green, and no evidence in both stays pending. Red gives `ci_red`; still pending at the deadline gives `ci_pending`.
   5. Read the PR head again. If it is no longer the reviewed SHA, stop with `head_moved`.
   6. If all checks are green and the review passed, run `gh pr ready`.
5. **No mutation beyond that.** The delivery GitHub client exposes only `issue_view`, `pr_create_draft`, `pr_checks`, `pr_ready` and `pr_diff`. A test, modelled on `assert_no_mutation` in `pr_watcher_github.py`, asserts that it has no merge, approve, close or label method.
6. **Record.** Verdict writes `delivery.json` atomically in the run directory, with branch, head SHA, PR number and URL, reviewer level and route, extra review result, CI summary, ready flag, reason and timestamps. A `delivery.sha256` file holds its digest. `events.jsonl` and `receipt.json` are never written. A stored record is reused (`already_delivered`) only when its run id, integration commit, receipt digest, events digest and issue number all match the current run; any difference is `delivery_record_mismatch`.
7. **Doctor.** A `github_delivery` row reports `gh` presence, login state and push/PR permission on the configured repository. It never prints a token.

## Risks / Trade-offs

- The existing `gh` login carries the operator's full scope; the operator chose this. The mitigations are a minimal client surface (decision 5), vgate on every push, and no merge, approve or close.
- Waiting for CI inside the command can take up to an hour. The deadline is bounded, and `ci_pending` leaves the PR draft for the CI-loop story to pick up.
- The extra review uses model routes and must follow admission and cost policy (ADR-049). It uses the review path, which already does.

## Security / trust implications

- Issue title and body are untrusted: only the number and the sanitised slug are used here.
- There is no shell interpolation: subprocess is called with argument lists only.
- No token is read, printed or stored.
- The pushed content is exactly the receipt-verified integration commit.

## Routing / context / memory implications

The extra cross-family review is one review call routed through the existing reviewer selection. Nothing else in delivery calls a model.

## Migration / rollback

The command is additive. Removing it returns delivery to the controller. Existing runs and receipts are unaffected.

## ADR impact

This implements ADR-052 decisions 3, 6, 7 and 10. No ADR changes.
