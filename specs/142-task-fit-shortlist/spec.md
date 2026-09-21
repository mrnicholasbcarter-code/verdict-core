# BOD-142 — Live task-fit candidate shortlist

## Objective
Wire the existing BOD-122 `build_candidate_pool()` contract into the live `IntelligenceService` admit path so a deterministic `TaskProfile` drives hard eligibility and task-fit Top-K construction before any live confirmation. The pool is an eligibility/shortlist authority only. BOD-104 remains final strategy/provider/model authority.

## Required flow
`TaskProfile -> concrete live inventory -> hard admission -> task-fit evidence score -> bounded diverse Top-K -> confirm only Top-K -> ExecutionPathRequest(pool_receipt) -> BOD-104 ExecutionPathDecision`

## Requirements
1. A stronger qualified free coding candidate can outrank a weaker free candidate despite higher latency.
2. Latency alone cannot overturn materially stronger task-fit evidence.
3. A missing required hard capability yields a named hard drop before ranking.
4. Unknown quality remains uncertainty and never becomes positive evidence.
5. The input is the discovered concrete inventory, not a static model allowlist; a new eligible identity can win.
6. Probe/confirm calls are limited to the bounded shortlist.
7. Hard-dropped candidates cannot reappear downstream.
8. Equivalent normalized evidence produces the same receipt and shortlist digest.
9. Shortlisting preserves provider/resource diversity when useful and deduplicates aliases/families.
10. Candidate-pool output can constrain BOD-104 offers but cannot choose the final execution strategy/provider/model.
11. Automatic coding launch remains fail-closed without a valid BOD-104 `ExecutionPathDecision`.

## Compatibility and ownership
- Reuse `TaskProfile`, free/active/spend admission, capability metadata, passports, confirmation, `CandidatePoolReceipt`, and `ExecutionPathRequest.pool_receipt`.
- Preserve existing `FreeTierAdmitReceipt` fields and add only compatible evidence hooks.
- Existing explicit legacy routing remains visibly non-authoritative. Production/default paths keep the BOD-104 fail-closed gate.
- Do not implement candidate-specific ContextPlan estimates or hydration (BOD-143).
- Do not define a unified receipt schema (BOD-144).
- Do not add another model router or derive quality/capability from model names.

## Evidence and safety
Hard eligibility precedes scoring. Inputs with unknown capability/quality stay unknown. Network confirmation sees only shortlist identities. Receipts carry the task-profile digest, pool evidence/digests, named drops, and probe/confirm scope without raw secrets.
