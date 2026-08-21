# Implementation Plan

**Goal:** Make security checks fail closed and launch readiness evidence-bound.

**Architecture:** Extend the existing GitHub security workflow and release
checklist. Keep policy declarative and protect both surfaces with focused tests;
do not introduce runtime or credential changes.

**Tech Stack:** GitHub Actions YAML, Markdown, pytest, Bandit, pip-audit, Ruff,
mypy.

## Constraints

- Do not modify runtime routing or provider credentials.
- Do not treat local results as hosted launch approval.
- Preserve the default `PENDING EVIDENCE` state.
- Stage only explicit story files.

## Steps

1. Test non-advisory security commands, bypass resistance, credential-file
   hygiene, and evidence-bound checklist fields.
2. Extend workflow and checklist with the smallest changes that satisfy the
   contracts.
3. Run focused tests, repository quality/security gates, a Sol review, then
   commit, push, verify CI, and merge only when green.
