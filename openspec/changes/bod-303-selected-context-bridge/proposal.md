# Proposal: BOD-303 selected worker context bridge

## Why

**KNOWN**: The first native pack slice supplies source safety and optional ContextPlan, but runtime/run did not forward the selected window. Hydrate events used independent legacy file-prefix accounting rather than the compiled pack.

## What Changes

- Bind worker ContextPlan to the exact selected RouteVerdict, without new inventory reads.
- Keep missing windows explicitly unknown and use a conservative offline fallback.
- Count mandatory goal/objective/acceptance/verification/rules and retry feedback before compiling context. Narrow full prompt bytes and estimated input after reserves. Retain whole rules; reject an oversized full prompt locally before executor launch.
- Emit actual selected-unit provenance, omissions and budget facts. Legacy string callbacks remain supported and report unknown pack provenance without rereading files.
- Add an optional structured hydration result. The existing default remains a string.

## Capabilities

### Modified Capabilities

- `orchestration-worker-context`: Selected-route planning, full prompt budget checks and actual pack hydrate evidence.

## Impact

**KNOWN**: Changes own runtime.py/run.py, minimal worker hydration API in planner.py/worker_context.py, focused tests and this change. No eligibility, provider health, recovery, executor, gateway, registry or memory authority changes. No installs or live calls.

**INFERRED**: Selected-window propagation should avoid blind oversized context dispatch. Offline bounds do not establish provider-tokenizer fit or task quality.

**NOT AVAILABLE**: No live self-hosted COMPLETE proof, measured input tokens, exact provider tokenizer or verified source revisions. Whole BOD-303 and total native scan-work bounds remain outside this increment.

## Acceptance Criteria

- [x] Exact selected route window and reserve/byte policy reach production worker hydration.
- [x] Missing windows/tokenizer/revisions stay explicit unknowns.
- [x] Whole mandatory prompt and retry feedback count before context; non-fit does not launch or cool a provider.
- [x] Hydrate sources describe actual selected units, never independently read file prefixes.
- [x] Legacy scripted callback signatures and default string hydration remain supported.
- [x] Focused scripted-DAG tests and source/spec checks pass; no live proof claim.
