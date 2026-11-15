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
  changed: 12 failed, 46 passed in `tests/test_check_dod.py`. The 12 are every
  new test except the six `test_no_change_needs_the_exact_shape_and_a_reason`
  cases, which pass there as they must: the old schema rejects every object
  without `change`.

## 3. Implementation
- [x] 3.1 The schema takes `{"change": ...}` or `{"status": "not_applicable",
  "reason": ...}`, and `scripts/check_dod.py` checks the second form against
  the merged commit and the change names. After: 58 passed in
  `tests/test_check_dod.py`; 85 passed with `tests/test_check_openspec_tasks.py`
  and `tests/test_drill_gates.py`. Ruff, ruff format and
  `mypy scripts/check_dod.py` are clean.
- [x] 3.2 Each guard is needed. Temporary edits, each restored afterwards:
  no merged-commit rule, 2 failed; no name rule, 6 failed; no digit boundary
  after the ticket number, 1 failed; case-sensitive change names, 1 failed;
  no checks at all for the new form, 8 failed.

## 4. Docs
- [x] 4.1 AGENTS.md "Definition of done" part 5, CONTRIBUTING.md and the
  `scripts/check_dod.py` docstring describe the new form.
  `scripts/check_doc_links.py`: `doc links ok (510 files checked)`.

## 5. Review
- [ ] 5.1 Cross-family review of the diff; every finding is fixed or declined
  with a reason.

## 6. Live checks (pending; needs merged main)
Nothing below has run.
- [ ] (post-merge) 6.1 On merged main, an evidence file for a merged story
  with no OpenSpec change (BOD-384, BOD-397 or BOD-413), built from real runs
  and a real parent confirmation, passes `scripts/check_dod.py`. Record the
  date, SHA, command and raw output.
- [ ] (post-merge) 6.2 Build the definition-of-done evidence file for this
  change and commit it with the change archived, only when
  `scripts/check_dod.py` passes on it. That needs 6.1 ticked first.
