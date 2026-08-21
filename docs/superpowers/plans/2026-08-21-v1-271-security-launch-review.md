# V1-008 Security, Cleanup, and Launch Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make security checks fail closed and make launch readiness evidence-bound.

**Architecture:** Extend the existing GitHub security workflow and release checklist. Keep security policy declarative and protect both surfaces with focused repository tests; do not introduce runtime or credential changes.

**Tech Stack:** GitHub Actions YAML, Markdown, pytest, Bandit, pip-audit, Ruff, mypy.

## Global Constraints

- Do not modify runtime routing or provider credentials.
- Do not treat catalog/runtime presence or local results as hosted launch approval.
- Preserve the default `PENDING EVIDENCE` launch state.
- Stage only explicit story files.

### Task 1: Establish the security and evidence contracts

**Files:** `.github/workflows/security.yml`, `RELEASE_CHECKLIST.md`, `tests/test_launch_gates.py`

- [ ] Write tests asserting required non-advisory security commands, credential-file hygiene, and evidence-bound checklist language.
- [ ] Run focused tests and confirm they fail before the contract exists.
- [ ] Implement the smallest workflow/checklist changes.
- [ ] Run focused tests and inspect the diff.

### Task 2: Verify and review

**Files:** `docs/specs/271-security-launch-review-*.md`, `docs/superpowers/plans/2026-08-21-v1-271-security-launch-review.md`

- [ ] Run focused tests, full tests, Ruff, mypy, Bandit, pip-audit, package checks where available, and the credential-file scan.
- [ ] Record limitations in the checklist without fabricating hosted evidence.
- [ ] Have Sol review the completed branch and fix any critical/important findings.
- [ ] Inspect staged paths and commit with a conventional message.
