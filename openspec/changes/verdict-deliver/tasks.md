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

- [x] 5.1 Run the full suite, ruff, mypy strict, reachability, the lint gates and both OpenSpec validators, and get an independent cross-family review. Verify: all commands green.
  Evidence: local gates on code commit 2e8d32ea (the tick commit after it changes only this file and was re-checked with `check_lint_gates.py --wip`, `check_doc_links.py` and `git diff --check`): `uv run --frozen ruff check .` all checks passed; `uv run --frozen ruff format --check .` 1902 files already formatted; `uv run --frozen mypy verdict --strict` no issues in 387 source files; `uv run --frozen python scripts/check_doc_links.py` doc links ok (891 files); `uv run --frozen python scripts/check_reachability.py --ratchet-base origin/main` 0 NEW findings, 0 NEW orphan modules; `uv run --frozen python scripts/check_lint_gates.py --wip` all lint gates passed, including `openspec validate` (change/verdict-deliver) and the vendored OpenSpec validator (verdict-deliver); `python -m pytest -q tests/test_deliver_*.py tests/test_doctor_github_delivery.py tests/test_actions_exclusions.py` 289 passed. Full suite: CI on the pull request head. Independent cross-family review: Codex (`cx/gpt-6.1-sol`) record for the head, verified by the controller before merge.
- [x] (post-merge) 6.1 On installed main, `verdict deliver` turns a real COMPLETE orchestrate run into a draft PR on a `verdict/issue-N` branch. The PR becomes ready only after cross-family review and green CI, and `delivery.json` records the outcome.
  Evidence: run by the controller on the operator's real setup on 2026-11-26, installed main `28c7f888` (the `96402975` merge of #1355 plus the push-timeout fix of #1371). A first attempt at 16:13 UTC on `96402975` ended `push_failed` (`git push failed: command timed out after 120s`), created nothing, and became #1369; the task stayed open until the retry. The retry delivered a real run: `verdict orchestrate` run `20261126T190409Z` (self-build of #1367 items 2 and 9: one OpenCode worker node on `cc/claude-sonnet-5-5`, then integration; in-run review PASS, 0 findings, route-level on `cc/claude-opus-5-5`) was COMPLETE at 19:12:55 UTC. Then `verdict deliver /tmp/claude-takeover/selfbuild-runs/20261126T190409Z --issue 1367 --issue-title "deliver fail-closed gaps" --ci-deadline 40 --json`, started 19:13:29 UTC, exit 0 at 19:25:58 UTC, printed `"status": "delivered"`, `"ok": true`, `"message": "created draft pull request"`, `"pr_url": "https://github.com/mrnicholasbcarter-code/verdict-core/pull/1373"`, `"branch": "verdict/issue-1367-deliverfail-closedgaps"`, `"integration_commit": "e1fa24a25fdd8abaccd86971b8e1319fc1538516"`, `"created": true`, `"ready": true`, `"ready_reason": ""`, `"review_level": "route"`, `"reviewer_route": "cc/claude-opus-5-5"`, `"extra_review_ran": true` and `"ci_polls": 17`. It pushed the integration commit through the pre-push gate, opened pull request #1373 as a draft with the generated body, ran the extra cross-family review because the in-run review was route-level, waited for the checks on that head, then ran `gh pr ready`. It did not merge, approve or label. `delivery.json` sits next to the run; its `delivery.sha256` holds `sha256:ef43f2099bb9f5368e7b3b2ee62c01ce6734976e83acf8e3dce56c2035fcd4ae`, which `sha256sum` reproduces from the file. Per the controller's record on issue #1269, pull request #1373 had 38 of 38 checks green on that head and was merged as `5933e807` after a separate Codex review of `e1fa24a2` (5 files, 0 findings). By hand, the controller still wrote the run constraints (the node plan), fast-forwarded the checkout to the integration commit before delivering (#1369 item 3) and scoped routes with `--scope cc/,cx/` (#1366). Known gap: `delivery.json` records that the extra review ran, not which route did it or its result (#1367).
