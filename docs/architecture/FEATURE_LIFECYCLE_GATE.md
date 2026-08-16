# Feature Lifecycle Gate — Release-Readiness Policy

**Status:** Approved (release-gating policy)
**Date:** 2026-08-03
**Applies to:** Every parent-level Verdict feature (EPIC-level), before any
semantic release, after the foundational primitives are stable.

## Purpose

Before Verdict ships, every parent-level feature must pass the **same
lifecycle** that produced the verified foundation (eligibility gate, router,
policy engine, capability passports, evidence, memory plane, model passports,
governance, context, execution continuity, CLI, workflows, code intelligence).

This policy prevents two failure modes:

1. **"AI rewrote everything"** — recreating primitives that already exist.
2. **"Feature shipped unverified"** — landing a feature with no ADR, no
   acceptance criteria, and no evidence.

## The Lifecycle (mandatory, in order)

Every parent-level feature follows the sequence used for MODEL-001 and GOV-001:

1. **Audit** — classify the current state: `COMPLETE` / `PARTIAL` / `MISSING`
   (see `docs/CURRENT_STATE_RECONCILIATION.md`). Do NOT recreate COMPLETE items.
2. **Research / Review** — query Code Review Graph, OpenViking, Ruflo/RuVector
   memory, and current docs. Benchmark top GitHub contenders (per the tool-audit
   process) before deciding to build, adopt, or skip.
3. **Proposal / Recommendations** — a short proposal doc stating the feature's
   current vs target state, the chosen approach, and rejected alternatives.
4. **GitHub ticket** — with Metadata / Product / Technical / Documentation
   (required ADRs) / Acceptance Criteria (specific, testable, measurable) /
   Testing / Demo Validation / Definition of Done.
5. **ADR** — durable architecture decisions require an ADR under `docs/adr/`,
   following existing ADR conventions. Do not duplicate an existing decision.
6. **Implement** — bounded, one writer per shared file, verify each step.
7. **Verify** — the feature's acceptance criteria must be proven by tests +
   evidence, per `ACCEPTANCE_GATES.md` and `release-checklists.md`. A gate
   without evidence is a failed gate.

## Release Gate

A parent-level feature is **release-ready** only when all of the above are
present and verified. The release checklist (`release-checklists.md`) then
applies the static/QA/security gates on top.

## Relationship to Existing Docs

| Doc | Role |
|-----|------|
| `ACCEPTANCE_GATES.md` | Flagship acceptance criteria (measurable, evidence-backed) |
| `release-checklists.md` | Static/QA/security release gates |
| **`FEATURE_LIFECYCLE_GATE.md`** | The per-feature lifecycle that *produces* the evidence those gates check |

## Reuse of the Tool-Audit

Adoption decisions (build vs integrate vs skip) for new capabilities should
reuse the tool-audit findings (RuVector / Ruflo memory / code-review-graph vs
GitHub contenders). See the audit report produced under task 16.
