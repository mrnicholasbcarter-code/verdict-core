# Spec Delta

## ADDED Requirements

### Requirement: Delivery starts only from a verified, complete run

`verdict deliver` SHALL push or open a PR only when all of the following hold: the run's receipt verifies; the outcome is `COMPLETE`; the integration commit exists; the named issue exists; and the `gh` login can push to the repository and create PRs on it. If any condition fails, it SHALL refuse with a named reason, with no push and no PR.

#### Scenario: Tampered receipt
- **WHEN** `events.jsonl` no longer matches the receipt digest
- **THEN** deliver refuses and nothing is pushed

### Requirement: Push only through the pre-push gate to a Verdict branch

Delivery SHALL push the receipt-verified integration commit to `verdict/issue-N-<slug>` with a normal push, so that the pre-push vgate runs. It SHALL NOT set `VERDICT_PREPUSH_SKIP` and SHALL NOT force-push. A gate rejection SHALL end delivery with `vgate_rejected` and the gate output tail.

#### Scenario: Gate rejects the push
- **WHEN** the pre-push hook exits non-zero
- **THEN** delivery ends with `vgate_rejected` and no PR exists

### Requirement: Draft PR, ready only after cross-family review and green CI

Delivery SHALL open a draft PR whose body carries `Refs #N`, the run id, the events digest and the OpenSpec change id, and SHALL NOT use a closing keyword. It SHALL mark the PR ready only when two conditions hold. First, every required check passes on the exact head SHA. Second, a reviewer from a model family different from every implementer has passed the change. If the in-run review used route-level independence, delivery SHALL run an extra cross-family review on the PR diff first. Otherwise the PR SHALL stay draft with one of `ci_red`, `ci_pending`, `review_failed` or `no_cross_family_reviewer`.

#### Scenario: Same-family in-run reviewer
- **WHEN** the in-run reviewer shared a model family with an implementer and no other family is admissible
- **THEN** the PR stays draft with `no_cross_family_reviewer`

#### Scenario: All green
- **WHEN** a cross-family review has passed and all required checks pass on the head SHA
- **THEN** the PR is marked ready, and delivery neither approves nor merges it

### Requirement: Delivery never merges, approves or closes

The delivery GitHub client SHALL expose no merge, approve, close or label operation. Delivery SHALL record its outcome in `delivery.json` next to the run and SHALL NOT modify `events.jsonl` or `receipt.json`.

#### Scenario: Record without touching the receipt
- **WHEN** delivery finishes in any state
- **THEN** `delivery.json` records branch, head SHA, PR, review evidence, CI result, ready state and reason, and the receipt still verifies
