# Spec Delta

## ADDED Requirements

### Requirement: Delivery starts only from a verified, complete run

`verdict deliver` SHALL push or open a PR only when all of the following hold: the run's receipt verifies; the outcome is `COMPLETE`; the integration commit exists; the named issue exists; and the `gh` login can push to the repository and create PRs on it. If any condition fails, it SHALL refuse with a named reason, with no push and no PR.

#### Scenario: Tampered receipt
- **WHEN** `events.jsonl` no longer matches the receipt digest
- **THEN** deliver refuses and nothing is pushed

### Requirement: Push only through the pre-push gate to a Verdict branch

Delivery SHALL push the receipt-verified integration commit to `verdict/issue-N-<slug>` with a create-only push, so that the pre-push vgate runs and an existing remote branch is never moved. It SHALL NOT set `VERDICT_PREPUSH_SKIP` and SHALL NOT overwrite a branch. A branch that exists at another commit, or that another writer creates while the push is in flight, SHALL end delivery with `branch_exists`. A gate rejection SHALL end delivery with `vgate_rejected` and the gate output tail. Before pushing, delivery SHALL check that the directory it pushes from has the integration commit checked out with a clean working tree, because the pre-push gate tests the checkout and not the pushed commit, and SHALL end with `worktree_not_at_integration_commit` otherwise.

#### Scenario: The checkout is not at the integration commit
- **WHEN** the directory delivery pushes from is at another commit, or has uncommitted or untracked changes
- **THEN** delivery ends with `worktree_not_at_integration_commit` and nothing is pushed or created

#### Scenario: Gate rejects the push
- **WHEN** the pre-push hook exits non-zero
- **THEN** delivery ends with `vgate_rejected` and no PR exists

#### Scenario: Another writer creates the branch during the push
- **WHEN** the branch appears on the remote, even at an ancestor of the integration commit, after delivery checked that it was absent
- **THEN** delivery ends with `branch_exists` and the other writer's branch is unchanged

### Requirement: Draft PR, ready only after cross-family review and green CI

Delivery SHALL open a draft PR whose body carries `Refs #N`, the run id, the events digest and the OpenSpec change id, and SHALL NOT use a closing keyword. It SHALL mark the PR ready only when two conditions hold. First, every required check passes on the exact head SHA. Second, a reviewer from a model family different from every implementer has passed the change. The in-run review SHALL count as cross-family only when the receipt names the reviewer route and the reviewer's family is none of the implementer families; a passing review that names no reviewer route SHALL be refused. If the in-run review used route-level independence, delivery SHALL run an extra cross-family review on the PR diff first. Delivery SHALL read every page of check runs, SHALL re-read the PR head immediately before marking it ready, and SHALL stop with `head_moved` if it is no longer the reviewed SHA. It SHALL reuse an open PR for the branch only while that PR is a draft. Otherwise the PR SHALL stay draft with one of `ci_red`, `ci_pending`, `review_failed`, `no_cross_family_reviewer` or `head_moved`.

#### Scenario: Same-family in-run reviewer
- **WHEN** the in-run reviewer shared a model family with an implementer and no other family is admissible
- **THEN** the PR stays draft with `no_cross_family_reviewer`

#### Scenario: Reviewer in an implementer family despite a recorded family level
- **WHEN** the run recorded family-level independence but the reviewer route is in an implementer family
- **THEN** delivery treats the review as route-level and runs the extra cross-family review, or leaves the PR draft with `no_cross_family_reviewer`

#### Scenario: Head moves while checks are pending
- **WHEN** another commit is pushed to the PR branch while delivery waits for checks
- **THEN** the PR stays draft with `head_moved` and `gh pr ready` is not run

#### Scenario: Open PR is already ready
- **WHEN** an open PR for the branch is not a draft
- **THEN** delivery ends with `pr_not_draft`, records nothing as delivered and changes nothing on that PR

#### Scenario: All green
- **WHEN** a cross-family review has passed and all required checks pass on the head SHA
- **THEN** the PR is marked ready, and delivery neither approves nor merges it

### Requirement: Delivery never merges, approves or closes

The delivery GitHub client SHALL expose no merge, approve, close or label operation. Delivery SHALL record its outcome in `delivery.json` next to the run and SHALL NOT modify `events.jsonl` or `receipt.json`.

#### Scenario: Record from another run
- **WHEN** `delivery.json` is valid for the same issue but names a different run id, integration commit, receipt digest or events digest
- **THEN** delivery ends with `delivery_record_mismatch` and runs no command

#### Scenario: Record without touching the receipt
- **WHEN** delivery finishes in any state
- **THEN** `delivery.json` records branch, head SHA, PR, review evidence, CI result, ready state and reason, and the receipt still verifies
