# Autonomous Development Loop — Operational Contract

**Status:** Approved (operational contract)
**Date:** 2026-08-03 (revised for BOD-17: swarm/Ruflo coordination removed)
**Applies to:** Every backlog issue worked by the Verdict autonomous development
workflow (see `verdict/workflows/autodev.py`, `FEATURE_LIFECYCLE_GATE.md`).

> Historical note: earlier revisions of this contract described a
> queen/worker "swarm" coordination step backed by Ruflo/RuVector. That
> coordination model was removed from Core (BOD-17); see
> [ADR-023](../adr/ADR-023-governed-swarm-supervision.md) (superseded) and
> [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) (current
> goal-to-receipt orchestration).

## The Loop

For each issue, the autonomous system runs the following cycle. It is a **loop**:
after merge, it finds the next available issue and repeats.

```
┌─────────────────────────────────────────────────────────────┐
│ 1. UNDERSTAND current system/project state                   │
│    - what exists today (COMPLETE / PARTIAL / MISSING)        │
│    - read ALL relevant docs: ADRs, specs, READMEs, TODOs     │
│    - derive an implementation path from what's documented    │
├─────────────────────────────────────────────────────────────┤
│ 2. RETRIEVE context before deciding                          │
│    - memory plane / ADRs / prior sessions / evidence         │
├─────────────────────────────────────────────────────────────┤
│ 3. PARALLEL RESEARCH (best available solution)               │
│    - a research subagent benchmarks public/open-source       │
│      contenders before choosing build-vs-adopt-vs-skip       │
├─────────────────────────────────────────────────────────────┤
│ 4. WEIGH all context → ARCHITECTURAL PLAN + ADR (if durable) │
├─────────────────────────────────────────────────────────────┤
│ 5. SPLIT into vertical slices of atomic work                 │
│    - disjoint file scopes, one writer per shared file        │
├─────────────────────────────────────────────────────────────┤
│ 6. EXECUTE until finished                                    │
│    - the 12-stage autodev workflow, or the ADR-036           │
│      goal-to-receipt orchestrator (verdict orchestrate)      │
├─────────────────────────────────────────────────────────────┤
│ 7. REVIEW + VERIFY results                                   │
│    - run full suite; confirm nothing regressed              │
├─────────────────────────────────────────────────────────────┤
│ 8. IMPLEMENT + VERIFY                                        │
│    - ruff, mypy --strict, pytest, git diff --check           │
│    - all correct, unaffected, evidence recorded              │
├─────────────────────────────────────────────────────────────┤
│ 9. FEATURE BRANCH + PR                                       │
│    - open branch, push, open PR                              │
│    - confirm CI/CD builds; fix until it passes; merge        │
├─────────────────────────────────────────────────────────────┤
│ 10. FIND next available issue → REPEAT                       │
└─────────────────────────────────────────────────────────────┘
```

## Coordination Choice

The execution step (6) may run as either:

- **The 12-stage autodev workflow** (`verdict/workflows/autodev.py`,
  `verdict autodev`) — for sequential stage-gated work.
- **Goal-to-receipt orchestration** ([ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md),
  `verdict orchestrate`) — for goal decomposition into routed, receipted
  attempts. See [docs/guides/interview-golden-path.md](../guides/interview-golden-path.md)
  for the end-to-end proof path (`verdict orchestrate | supervise | watch |
  run-receipt | eligibility`).

## Verification Before Merge (step 8/9)

Every PR must pass, before merge:

- `uv run pytest -q` — full suite, zero new failures
- `uv run --extra dev --extra dashboard --extra server ruff check .`
- `uv run --extra dev --extra dashboard --extra server ruff format --check .`
- `uv run --extra dev --extra dashboard --extra server mypy verdict --strict`
- `git diff --check`
- CI/CD (`.github/workflows/ci.yml` etc.) green

If CI fails, fix and re-run until it passes. Then merge.

## Relationship to Existing Docs

| Doc | Role |
|-----|------|
| `FEATURE_LIFECYCLE_GATE.md` | Per-feature lifecycle (audit → … → verify) |
| `verdict/workflows/autodev.py` | The 12-stage workflow implementation |
| [`ADR-036`](../adr/ADR-036-goal-to-receipt-orchestration.md) | Goal-to-receipt orchestration (current coordination model) |
| **`AUTONOMOUS_DEV_LOOP.md`** | The full operational loop incl. PR/CI/merge/repeat |
| `RELEASE_CHECKLIST.md` (repo root) | Static/QA/security release gates |
