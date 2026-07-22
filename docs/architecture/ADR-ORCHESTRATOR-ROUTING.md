# ADR: Orchestrator-Driven Routing Architecture

**Amended by:** [ADR-001 — Versioned, Privacy-Safe Execution Evidence](ADR-EVIDENCE-LEDGER.md)

**Status:** Accepted
**Date:** 2026-07-21
**Supersedes:** `docs/specs/ROUTING_POLICY.md`, `docs/specs/ENFORCEMENT_AND_LEARNING.md` (partial)

## Context

The current Verdict Core (formerly the legacy LLM gate) architecture contains two
conflicting design documents:

1. **ROUTING_POLICY.md** - States that the legacy gate must not couple to
   Ruflo/RuVector internals and enforces hard separation between the gate and
   learning/selection systems.
2. **ENFORCEMENT_AND_LEARNING.md** - Reinforces deterministic-only routing in the request path.

However, the user's explicit architecture vision (captured in `ORCHESTRATOR_DRIVEN_ROUTING.md`) is:

> **The orchestrator (frontier model) is the SELECTOR. Verdict is a THIN GATE
> (eligibility + probe + fail-closed + catalog mirror + explain). Nothing
> hardcoded anywhere.**

The current policy docs forbid exactly what the architecture requires: the learning loop (SONA/ReasoningBank) feeding model selection intelligence, and the gate accepting orchestrator-chosen candidates instead of computing tiers itself.

## Decision

**Refine the policy boundary:**

| Old Policy (forbidden) | New Policy (allowed) |
|---) | New Policy (allowed) |
|------------------------|-------------------|----------------------|
| Verdict imports Ruflo internal modules | ✅ Verdict subprocess-calls `ruflo hooks_model-outcome` CLI | ✅ Verdict receives orchestrator-chosen candidates |
| Selection logic in `intelligence.route()` | ✅ Selection = orchestrator + neural loop | ✅ Gate = deterministic eligibility only |
| Static `classifier.py` tiers as routing authority | ✅ Tiers DEPRECATED for non-protected path | ✅ Protected work = frontier floor enforced by gate |

**The gate's three jobs (canonical):**
1. **Eligibility gate** (`EligibilityGate` + `ProbeRunner`): fail-closed truth about whether a candidate is usable. No selection logic.
2. **Catalog mirror** (`GET /v1/models`): serve live OmniRoute metadata (pricing, capabilities, ratings) so the orchestrator can "review all models like openrouter.ai".
3. **Enforcement/explanation** (`/v1/route/explain`): expose admitted set, exclusions with reasons, probe truth, cache confidence. Enforce protected-work fail-closed.

**The selector is the orchestrator + ruflo neural/SONA, not code.** The `classifier.py` regex tier table is deprecated for the non-protected path (it remains as a deterministic frontier floor for protected work).

## Consequences

### Positive
- Aligns policy with the actual working architecture (verified in `verdict-core`)
- Enables the learning loop: worker outcomes → `hooks_model-outcome` → SONA → better routing
- Allows dynamic model selection from live catalog without hardcoded tiers
- Keeps Verdict deterministic and auditable for protected work

### Negative / Risks
- Requires updating both policy docs to soften "no coupling" → "no selection logic in request path"
- `intelligence.py` must be refactored to accept `preselected: list[ModelInfo]` instead of computing `select_best_model(tier)`
- Must verify the learning loop transport (subprocess CLI vs MCP) before wiring

## Implementation Notes

1. **Task 0 (this ADR)** — Update `ROUTING_POLICY.md` and `ENFORCEMENT_AND_LEARNING.md` to reference this ADR and change the forbidden-coupling language to "Verdict must not put *selection* logic in the request path; orchestrator owns selection."
2. **Task 1** — Decouple `EligibilityGate` from `classifier.py` tiers (worktree `feat/57-eligibility-gate` already has the gate accepting `list[ModelInfo]` candidates)
3. **Task 5** — Wire `record_outcome` → `ruflo hooks_model-outcome` subprocess (same pattern as `intelligence.py._probe_managed_backend`)

## References

- `docs/architecture/ORCHESTRATOR_DRIVEN_ROUTING.md` — canonical intent record (orchestrator loop, dynamic catalog, SONA learning)
- `docs/architecture/ADR-149` — precedent: drop tier abstraction, operate on concrete ModelIds, cost-optimal selection
- ruflo `intelligence-route` skill — `hooks_model-route` / `hooks_model-outcome` CLI surface
- ruflo `neural-train` skill — SONA distillation + ReasoningBank pattern store
