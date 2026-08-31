# Analysis: Routing Demo Cost vs Quality

**Date**: 2026-08-30
**Artifacts**: spec.md, plan.md, research.md, data-model.md, contracts/routing-demo.md, checklists/routing-demo.md, tasks.md

## Findings

| ID | Severity | Finding | Resolution |
|----|----------|---------|------------|
| A1 | Low | Issue AC path `examples/routing-demo/` vs ownership `verdict/routing_demo.py` | Clarified: core module + docs/tests; optional thin examples wrapper (T003) |
| A2 | Low | Numeric prices not on `ConcreteIdentity` | Research R2 / T004: keep pricing index beside identities |
| A3 | Medium | OmniRoute may 503 on execute while catalog works | Research R4 / T017: honest success rate; catalog-down still blocked |
| A4 | Info | Legacy `scripts/demo-routing.py` MOCK remains | Non-authoritative; docs point to new entrypoint |

## Coverage

- US1 → T008–T012
- US2 → T013–T014
- US3 → T015–T019
- FR-001…FR-011 mapped via stories + polish T020–T022
- Checklist `routing-demo.md`: 27/27 requirements-quality items reviewed `[x]`

## Gate

No CRITICAL conflicts. Ready for `/speckit-implement`.
