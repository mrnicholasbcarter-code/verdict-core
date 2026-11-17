# Proposal: Dynamic Role-Based Model Selection

## Why

The current model selection in Verdict is largely static or based on simple eligibility. As the number of available routes grows and their performance varies across different tasks (planning, implementing, reviewing), a dynamic system is required to optimize for strength, capacity, and quota while maintaining the strict safety invariants of the control plane.

## What Changes

- Implements a role-aware selection pipeline (`S0` -> `S1` -> `S2` -> `S3`).
- Introduces a `role_ladder.shadow` event for side-effect-free evaluation.
- Adds capacity-based role policies and measured demotion.
- Implements a flag-based rollout (Off/Shadow/On).

## Capabilities

### New Capabilities

- `dynamic-role-selection`: The orchestrator selects the best qualified route based on the role's strength and current capacity/quota state.
- `role-ladder-shadowing`: The system can record and compare dynamic selection vs. legacy selection without affecting the actual dispatch.

### Modified Capabilities

- `orchestration-dispatch`: The dispatcher now consumes the output of the role-based ladder.

## Impact

- `verdict/orchestration/eligibility.py` and `admission.py` are extended to support role narrowing.
- New events added to `events.jsonl` for selection provenance.
- No changes to the public `/v1/route` API contract.

## Evidence boundary

KNOWN: T1 research measured 35.1% identity coverage (1,466 of 4,175 live routes mapped) across the current gateway. KNOWN: the existing admission ladder and eligibility floors in `verdict/admission.py` and `orchestration/eligibility.py` already enforce the required invariants. KNOWN: the Core metadata store provides model identity via `lookup_omniroute_id`. INFERRED: a four-stage pipeline (S0-S3) with remove-only narrowing will maintain the safety bar while enabling dynamic optimization. NOT AVAILABLE: live shadow-mode acceptance proof (L1-L5), post-merge doctor checks, and swarm wiring. These are T3-T12 implementation tasks.

## Acceptance criteria

- Dynamic selection produces a valid H3 set that is a subset of H1.
- Shadow mode recordings match the `shadow_event_contract`.
- `off` mode maintains 100% parity with the legacy selection path.
- Reviewer independence is enforced via family check in call logs.
- `FAIL_CLOSED` is triggered when the candidate pool is empty.
