# Tasks

## 1. Selected route bridge

Owned files: runtime.py/run.py and minimal planner.py/worker_context.py worker APIs. Depends on first native slice 33c54d35. Verification: focused test_orch_selected_context.py, test_orch_worker_context.py, test_orch_planner.py and test_orch_runtime.py using the native project environment.

- [x] 1.1 Read AGENTS and actual route/runtime/planner/pack contracts.
- [x] 1.2 Forward exact selected window, unknown fallback and reserve/byte facts through ContextPlan.
- [x] 1.3 Add optional structured prompt and signature-aware legacy adaptation.
- [x] 1.4 Count mandatory/feedback text before pack and reject full prompt non-fit before executor launch, without provider cooldown.
- [x] 1.5 Emit actual selected-unit evidence and explicit unknown legacy provenance.

## 2. Fixture proof and documentation

Owned files: tests/test_orch_selected_context.py, docs/BOD-303-SELECTED-CONTEXT.md and this OpenSpec change. Depends on group 1. Verification: the focused tests above, source ruff check/format, strict mypy on changed source, OpenSpec validator, doc links and git diff --check. No full-suite or live proof claim.

- [x] 2.1 Test selected small/missing window, tiny no-launch, accurate source metadata, overflow shrinking and retry suffix budget.
- [x] 2.2 Test optional positional/budget/plan/**kwargs callbacks and string default compatibility.
- [x] 2.3 Record bootstrap evidence, lint/types and limits; commit for parent-managed train.

## 3. Deferred live proof

Owned scope: future coordinator run evidence, not this branch. Depends on groups 1/2 and repaired bootstrap blockers. Verification requires explicit consent and live digest-bound receipts; unavailable here.

- [ ] 3.1 Obtain authorized live model/harness proof. Do not claim whole BOD-303 COMPLETE from fixtures.
