# Tasks — verdict deliver

## 1. Preconditions and record

- [x] 1.1 Delivery preconditions: receipt verifies, outcome COMPLETE, integration commit exists, issue exists, `gh` can push and create PRs. Each failure has a named reason, with no push and no PR. Verify: unit tests for each refusal (tampered receipt, BLOCKED run, missing issue, missing `gh` login), with git and `gh` faked.
  Evidence: tests/test_deliver_gate.py, tests/test_deliver_publish.py, tests/test_deliver_service.py.
- [x] 1.2 Write `delivery.json` atomically, plus `delivery.sha256`, leaving `events.jsonl` and `receipt.json` byte-identical. Verify: test comparing digests before and after.
  Evidence: tests/test_deliver_service.py.

## 2. Branch and push

- [x] 2.1 Slug from the issue title (sanitised, at most 40 characters), branch `verdict/issue-N-<slug>`, push of the integration SHA through the pre-push hook with argument-list subprocess calls. `vgate_rejected` carries the gate tail. `branch_exists` is raised on a conflict. No force-push. Verify: tests with a local bare remote and a fake pre-push hook that rejects.
  Evidence: verdict/orchestration/deliver_publish.py, tests/test_deliver_push_branch.py (`python -m pytest -q tests/test_deliver_push_branch.py`).

## 3. Draft PR and ready gate

- [x] 3.1 Draft PR body built by Verdict, containing `Refs #N`, run id, events digest, change id and reviewer evidence, and never a closing keyword. Verify: a test on the rendered body.
  Evidence: tests/test_deliver_render.py.
- [x] 3.2 Ready gate. Read the in-run review level. Run the extra cross-family OpenCodeReview when the in-run review was route-level. Stop with `no_cross_family_reviewer` or `review_failed` when that review is unavailable or fails. Wait for checks on the exact head SHA within the deadline (`ci_red`, `ci_pending`). Run `gh pr ready` only when everything is green. Verify: a test for each outcome with the review and the checks faked.
  Evidence: verdict/orchestration/ready_gate.py, tests/test_deliver_ready_gate.py (`python -m pytest -q tests/test_deliver_ready_gate.py`).
- [x] 3.3 GitHub client surface: no merge, approve, close or label method. Verify: an `assert_no_mutation`-style test.
  Evidence: tests/test_deliver_github_surface.py (`python -m pytest -q tests/test_deliver_github_surface.py`).

## 4. Wiring, doctor and docs

- [x] 4.1 Add the `verdict deliver` subcommand, a doctor row `github_delivery` that never prints a token, and an entry in docs/CLI_REFERENCE.md. Verify: CLI and doctor tests, the doc-link check, and reachability showing nothing new.
  Evidence: tests/test_deliver_cli.py, tests/test_deliver_e2e.py, tests/test_doctor_github_delivery.py, tests/test_actions_exclusions.py.

## 5. Verification

- [ ] 5.1 Run the full suite, ruff, mypy strict, reachability, the lint gates and both OpenSpec validators, and get an independent cross-family review. Verify: all commands green.
- [ ] (post-merge) 6.1 On installed main, `verdict deliver` turns a real COMPLETE orchestrate run into a draft PR on a `verdict/issue-N` branch. The PR becomes ready only after cross-family review and green CI, and `delivery.json` records the outcome.
