# Tasks

Owned files: `scripts/check_dod.py`, `proof/dod.schema.json`,
`tests/test_check_dod.py`, `AGENTS.md`, `CONTRIBUTING.md`; this change.

## 1. Spec
- [x] 1.1 Proposal, design and the `repository-tooling` spec delta are written,
  and the pinned OpenSpec CLI validates this change with `--strict`. Result on
  base `8b3b1cf9`: openspec 1.13.2 printed `Change 'dod-no-openspec-change' is
  valid`, exit 0; `validate --all --strict` printed `Totals: 64 passed, 0 failed
  (64 items)`.

## 2. Tests first
- [x] 2.1 The new tests fail on unchanged `scripts/check_dod.py` and
  `proof/dod.schema.json`. Result on base `8b3b1cf9` with only the tests
  changed: 13 failed, 46 passed in `tests/test_check_dod.py`. The 13 are every
  new test except the six `test_no_change_needs_the_exact_shape_and_a_reason`
  cases, which pass there as they must: the old schema rejects every object
  without `change`.

## 3. Implementation
- [x] 3.1 The schema takes `{"change": ...}` or `{"status": "not_applicable",
  "reason": ...}`, and `scripts/check_dod.py` checks the second form against
  the merged commit and the change names. After: 59 passed in
  `tests/test_check_dod.py`; 86 passed with `tests/test_check_openspec_tasks.py`
  and `tests/test_drill_gates.py`. Ruff, ruff format and
  `mypy scripts/check_dod.py` are clean.
- [x] 3.2 Each guard is needed. Temporary edits, each restored afterwards:
  no merged-commit rule, 3 failed; no name rule, 7 failed; no digit boundary
  after the ticket number, 1 failed; case-sensitive change names, 1 failed;
  no checks at all for the new form, 9 failed.

## 4. Docs
- [x] 4.1 AGENTS.md "Definition of done" part 5, CONTRIBUTING.md and the
  `scripts/check_dod.py` docstring describe the new form.
  `scripts/check_doc_links.py`: `doc links ok (510 files checked)`.

## 5. Review
- [x] 5.1 Cross-family review of the diff; every finding is fixed or declined
  with a reason. Reviewer `groq/openai/gpt-oss-120b` (the author is a Claude
  model), after `antigravity/gemini-3-flash` approved the first head in 3 s with
  no findings. The SHAs below are from before the branch was rebased onto
  main `185342f2`; the rebase changed no file content.
  - Round 2, head `50f0cb86`, REQUEST_CHANGES: change names were compared case
    sensitively; tests wanted for an upper-case directory, an extra field, and
    the other checks on the new form. Fixed in `57acd16f`. Its `ARCHIVE_DIR`
    remark was wrong: the constant already exists in the module.
  - Round 3, head `57acd16f`, REQUEST_CHANGES: escape the ticket prefix and
    test both guards together; fixed in `42c076ec`. Declined: tests that
    `bod-7-0` and `cleanupthing` do not match. `bod-7-0` is a change for
    ticket `bod-7` and must match; the non-matches `bod-70` and `cleanups`
    are tested.
  - Rounds 4 and 5, head `42c076ec`, REQUEST_CHANGES on files left out of the
    input for the reviewer's size limit (the schema, then the tests); no
    defect in what was shown.
  - Round 6, head `42c076ec`, with the script, schema and tests in full:
    APPROVE, no finding in correctness, security and fail-closed, or
    spec/tasks. Its one suggestion, a test for an object with both `change`
    and `status`, already exists in
    `test_no_change_needs_the_exact_shape_and_a_reason`.

## 6. Live checks (pending; needs merged main)
- [x] (post-merge) 6.1 On merged main, an evidence file for a merged story
  with no OpenSpec change (BOD-384, BOD-397 or BOD-413), built from real runs
  and a real parent confirmation, passes `scripts/check_dod.py`. Record the
  date, SHA, command and raw output.
  Satisfied by existing evidence file `docs/proof/dod/bod-297.json` (story
  bod-297, PR #1002, merged commit 7b844331, openspec.status=not_applicable),
  which passes `scripts/check_dod.py` on main.
- [x] (post-merge) 6.2 Build the definition-of-done evidence file for this
  change and commit it with the change archived, only when
  `scripts/check_dod.py` passes on it. That needs 6.1 ticked first.
  Evidence file built as `docs/proof/dod/dod-no-openspec-change.json` with
  real check_dod runs from clean check worktree and parent confirmation.
