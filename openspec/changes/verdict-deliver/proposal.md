# Verdict delivers a finished run as a draft PR, marked ready when green

## Why

`verdict orchestrate` ends with a run-internal integration commit and a digest-bound receipt. Everything after that is done by the controller by hand: pushing a branch, opening a PR, getting a cross-family review and watching CI. On 2026-11-23 the operator decided that Verdict builds GitHub issues through to a PR that is ready for the operator to merge ([ADR-052](../../../docs/adr/ADR-052-github-issue-to-ready-pr.md), D37), and that delivery is built first. This advances the mission: each story Verdict delivers itself costs the operator two touches (approve the design, merge the build) instead of a manual push, PR and review loop.

## What Changes

- New `verdict deliver --run <run> --issue N` command. It runs only for a finished run whose receipt verifies (`verify_run_receipt`) and whose outcome is `COMPLETE`.
- It pushes the integration commit to `verdict/issue-N-<slug>` with a plain `git push`. The pre-push vgate runs; a rejection is a named failure. Verdict never sets `VERDICT_PREPUSH_SKIP`.
- It opens a **draft** PR with `gh pr create --draft`, using the existing `gh` login. The body links the issue (`Refs #N`, never a closing keyword), the run id, the events digest and the OpenSpec change id.
- **Ready gate.** The PR is marked ready (`gh pr ready`) only when CI is green on the exact head SHA and a review from another model family has passed. If the run's in-run review fell back to route-level independence (same family), `deliver` first runs an extra OpenCodeReview on the PR diff with the implementer families excluded. If no reviewer from another family is available, the PR stays draft with a named reason.
- `deliver` never approves, merges or closes anything.
- It writes a `delivery.json` record next to the run, with its own SHA-256. It never modifies `events.jsonl` or `receipt.json`.
- `verdict doctor` reports whether the `gh` login can push to and open PRs on the repository.

## Capabilities

### New Capabilities

- `story-delivery`: turn a verified, complete orchestrate run into a draft PR on a Verdict branch, and mark it ready only after cross-family review and green CI.

## Impact

Affected:
- a new delivery module and CLI subcommand (`verdict/commands/parsers_*.py`, `dispatch.py`);
- reads of `verdict/orchestration/receipt.py` (`verify_run_receipt`) and run events (reviewer level);
- reuse of the OpenCodeReview invocation in `verdict/orchestration/review.py`;
- a `gh` client modelled on `verdict/pr_watcher_github.py`;
- doctor;
- docs/CLI_REFERENCE.md.

No change to orchestrate, receipts, vgate or merge policy.

## Evidence boundary

- **KNOWN:**
  - `verify_run_receipt(run_dir, repair=False)` returns an empty list when the receipt is valid (`verdict/orchestration/receipt.py`).
  - The runtime tries family-level reviewer independence first, then falls back to route-level (`verdict/orchestration/runtime.py`).
  - `pr_watcher_github.py` has a read-only GitHub client with an `assert_no_mutation` guard.
  - PR #1191 (2026-11-20) was pushed and opened by hand after a COMPLETE run whose reviewer was from the same family.
  - GitHub's Copilot cloud agent uses the same model: draft PR, no approve or merge (docs.github.com, fetched 2026-11-23).
- **INFERRED:** most red CI on agent PRs comes from lint and environment issues (orchestration-adoption findings, attempts 7–11). The CI fix loop is a separate story.
- **NOT AVAILABLE:** a measured operator-time saving.

## Acceptance criteria

- `deliver` refuses with a named reason when:
  - the receipt does not verify;
  - the outcome is not COMPLETE;
  - the issue does not exist;
  - `gh` cannot push or create PRs.

  In all of these cases it makes no push and no PR.
- On success it pushes `verdict/issue-N-<slug>` through vgate and opens a draft PR whose body carries `Refs #N`, the run id, the events digest and the change id.
- A vgate rejection ends with `vgate_rejected` and the gate tail. No PR is created.
- The PR is marked ready only when every required check passes on the exact head SHA and a reviewer from a different family than every implementer has passed it. Otherwise it stays draft with one of: `ci_red`, `ci_pending`, `review_failed`, `no_cross_family_reviewer`.
- No code path calls merge, approve, or close for an issue or a PR, and a test enforces this.
- `delivery.json` records branch, head SHA, PR number and URL, reviewer evidence, CI result, ready state and reason. `events.jsonl` and `receipt.json` are byte-identical before and after.
