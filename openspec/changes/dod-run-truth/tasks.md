# Tasks

Owned files: `scripts/check_dod.py`, `tests/test_check_dod.py`, `AGENTS.md`; this change.

## 1. Spec
Tests/verification: run both pinned OpenSpec validators and require exit 0.
- [x] 1.1 Proposal, design and the `repository-tooling` spec delta are written. Verification: both pinned validators exit 0.
- [ ] (post-merge) Validate with both repository and pinned OpenSpec validators on merged main. Verification: command exit codes and output are recorded.

## 2. Tests first
- [ ] 2.1 Add failing and passing tests for outcome and exit-code agreement. Verification: focused pytest shows red before implementation and green after.
- [ ] 2.2 Add failing and passing tests for the five-minute result window. Verification: focused pytest shows red before implementation and green after.
- [ ] 2.3 Add failing and passing tests for PASS review status. Verification: focused pytest shows red before implementation and green after.
- [ ] 2.4 Add failing and passing tests for duplicate raw-output digests. Verification: focused pytest shows red before implementation and green after.
- [ ] 2.5 Add a non-JSON raw-output regression test. Verification: focused pytest passes before and after implementation.

## 3. Implementation
- [ ] 3.1 Parse structured orchestration results, require them for `verdict orchestrate --json`, and enforce run truth. Verification: focused pytest and strict mypy pass.
- [ ] 3.2 Reject duplicate raw outputs across distinct evidence runs. Verification: focused pytest passes with the exact required error.
- [ ] 3.3 Validate structured parent confirmations without an exit-code field. Verification: focused pytest passes for BLOCKED and out-of-window confirmations.

## 4. Docs
Tests/verification: inspect the exact AGENTS.md sentence and run the repository documentation checks.
- [ ] 4.1 Document orchestration run-truth validation in AGENTS.md. Verification: doc-link and diff checks pass.

## 5. Verification
Tests/verification: execute every acceptance command from the worktree root and record each exit code.
- [ ] (post-merge) 5.1 Run the full definition-of-done and repository gates on merged main. Verification: all acceptance commands exit 0 and raw summaries are recorded.
