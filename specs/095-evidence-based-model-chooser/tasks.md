# Tasks: BOD-95 evidence-based model chooser

> Historical implementation checklist, reconciled after delivery. The items
> below are complete in the shipped code and tests; this file is retained as
> planning provenance rather than an active execution queue.

## Phase 1: Contract and ranker

- [x] T001 Add `verdict/chooser.py` with resource classes, protected task classes, receipt dataclass, and production ranker. Ranker must receive only admitted candidates.
- [x] T002 Keep `select_eligible_route` as the only selection entry; pass ranker in. Do not reimplement EligibilityGate.
- [x] T003 Encode explicit-eligible win and explicit-ineligible fail-closed before any substitute.

## Phase 2: Tests (proof cases)

- [x] T004 Add `tests/test_chooser.py` covering BOD-95 proof cases 1-10 with fixture CandidateEvidence.
- [x] T005 Assert UNKNOWN quota/headroom remains UNKNOWN and identities include gateway+provider+resource_pool+model.

## Phase 3: CLI

- [x] T006 Add `verdict choose` in `verdict/cli.py` with `--task-class`, `--requires`, `--model`, `--json`, optional `--candidates-json`.
- [x] T007 Human output includes `selected because` and top fallback/exclusion reasons without internal telemetry dumps.
- [x] T008 Add `tests/test_choose_cli.py` for JSON contract and fail-closed paths.

## Phase 4: Proof

- [x] T009 Run STATIC/UNIT/INTEGRATION commands and record exact output in worker return.
- [x] T010 Do not open a PR until proof is complete. Parent will review, then PR, CI, merge.

## Constraints

- One worktree: `/home/nick/dev/.worktrees/bod-95-model-chooser`
- One branch: `feat/bod-95-model-chooser`
- Do not edit `/home/nick/dev/verdict-core` checkout
- Do not add SONA, Dagger, Phoenix, PWA, or slash-command infrastructure
