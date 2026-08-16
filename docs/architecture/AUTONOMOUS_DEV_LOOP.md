# Autonomous Development Loop — Operational Contract

**Status:** Approved (operational contract)
**Date:** 2026-08-03
**Applies to:** Every backlog issue worked by the Verdict autonomous development
workflow (see `verdict/workflows/autodev.py`, `FEATURE_LIFECYCLE_GATE.md`).

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
│    - memory / RAG / ADRs / prior sessions / evidence         │
│    - query Code Review Graph, OpenViking, Ruflo/RuVector     │
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
│ 6. SWARM until finished                                      │
│    - queen/worker subagent swarm (bounded), OR              │
│    - a loop mechanism driving the autodev workflow           │
├─────────────────────────────────────────────────────────────┤
│ 7. PARENT REVIEW + VERIFY subagent results                   │
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

The swarm step (6) may run as either:

- **Bounded queen/worker subagent swarm** — for multi-file, cross-module work
  (1 coordinator + up to 2 write workers + 2 read-only scouts; disjoint file
  scopes; no overlapping write ownership).
- **A loop mechanism driving `verdict/workflows/autodev.py`** — for work that
  fits the 12-stage workflow.

Selection: use the queen/worker swarm for independent slices that can run in
parallel; use the autodev workflow for sequential stage-gated work.

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
| **`AUTONOMOUS_DEV_LOOP.md`** | The full operational loop incl. PR/CI/merge/repeat |
| `release-checklists.md` | Static/QA/security release gates |
