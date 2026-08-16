# ADR-ORCHESTRATOR-ROUTING: Orchestrator-Driven Model Selection with Deterministic Enforcement

- **Status**: accepted
- **Date**: 2026-07-31
- **Deciders**: Verdict Core maintainers
- **Tags**: routing, architecture, orchestrator, neural-learning, eligibility-gate
- **Amends**: ADR-006-authoritative-documentation-preflight.md, ROUTING_POLICY.md, ENFORCEMENT_AND_LEARNING.md
- **Related**: Ruflo ADR-103, ADR-131, ADR-144, ADR-150, ADR-171, ADR-176

## Context

Verdict originally used a static tier-based classifier (`classifier.py` with regex tiers) to route tasks to models. This approach:
- Hardcoded model selection logic in deterministic code
- Could not adapt to the live OmniRoute catalog (3600+ models with pricing/capabilities)
- Did not learn from execution outcomes
- Coupled selection logic to the enforcement layer

The Codex-driven plan identified that model *selection* should be performed by a frontier/orchestrator model that:
1. Reviews the live OmniRoute catalog metadata (pricing, context windows, capabilities)
2. Picks right-sized workers per task slice
3. Dispatches via Ruflo swarms/agents
4. Learns from worker outcomes via SONA neural feedback loop

Meanwhile, the *enforcement* layer (EligibilityGate + ProbeRunner) remains deterministic and fail-closed.

## Decision

Verdict splits routing into two distinct layers:

### 1. Selector Layer (Orchestrator) — Non-Deterministic, Adaptive
- **Authority**: Frontier model (Claude Opus / Sonnet) or Hermes orchestrator
- **Input**: Live OmniRoute `/v1/models` catalog (3600+ models with pricing, context, capabilities)
- **Process**: 
  - Researches task requirements
  - Queries live catalog for candidate models
  - Selects appropriately-sized workers per slice
  - Dispatches via Ruflo `swarm`/`agent-coordination` skills
- **Learning**: SONA feedback loop (RuVector + ReasoningBank) feeds outcomes back to improve future selections
- **Coupling**: Uses Ruflo neural/SONA, swarm orchestration, OpenRouter AgentSDK — explicitly permitted for selection layer only

### 2. Enforcement Layer (EligibilityGate) — Deterministic, Fail-Closed
- **Authority**: Local deterministic code (`verdict/eligibility.py`, `verdict/probes.py`)
- **Input**: Orchestrator's selected candidate set + live probe results
- **Checks**:
  - Capability passports (ADR-010)
  - Budget limits
  - Privacy/PIA constraints
  - Probe-verified availability (AvailabilityCache + ProbeRunner)
- **Output**: `admitted` set, `exclusions` with reasons, `confidence`, `refresh_error` state
- **Protected work**: Always fails closed when fresh runtime truth is absent
- **Coupling**: Zero Ruflo/RuVector coupling — pure deterministic Python

### Deprecated
- `classifier.py` static tier regex — **retired for non-protected path**
- `ROUTING_POLICY.md` / `ENFORCEMENT_AND_LEARNING.md` provisions forbidding Ruflo coupling — **superseded for selection layer**

## Consequences

### Positive
- **Adaptive selection**: Model choice responds to live catalog changes, pricing, new capabilities
- **Cost optimization**: Frontier analysis amortized across many cheap worker calls
- **Continuous improvement**: SONA loop learns from actual latency/success/cost outcomes
- **Clear separation**: Selection (adaptive) vs Enforcement (deterministic) responsibilities
- **Auditability**: Enforcement layer remains fully explainable and testable

### Negative
- **Non-deterministic selection**: Frontier model choices vary; protected work needs deterministic floor
- **Policy doc conflict**: Existing docs forbid Ruflo coupling — requires explicit ADR reconciliation (this ADR)
- **Operational complexity**: Requires live OmniRoute catalog, Ruflo swarm infrastructure
- **Per-model detail endpoint**: Exact OmniRoute `/v1/models` shape must be verified at wire time

## Validation

### Pre-merge Requirements
1. **ADR landed**: This ADR merged and ingested into OpenViking
2. **Policy docs updated**: `ROUTING_POLICY.md` and `ENFORCEMENT_AND_LEARNING.md` amended to reference this ADR
3. **Tests passing**: 
   - `GET /v1/models` returns pricing + capabilities
   - `GET /v1/route/explain?model_id=...` returns `exclusions` with reasons
   - Probe `denied` → candidate excluded from `admitted` set
   - Learning feedback recorded via `hooks_model-outcome`
4. **CI green**: All existing CI/Lint/CodeQL on `main`
5. **RuVector relationships updated**: `mcp__ruvector__hooks_route` reflects new selection authority

### Follow-up Work
- **Q1 Resolution**: Protected work deterministic floor — define minimum capability passport requirements before orchestrator selection
- **Durable evidence**: Execution evidence ledger (ADR-001) integration with orchestrator workflow
- **Tenant isolation**: Authenticated-principal resolver for multi-tenant evidence scope

## Implementation Tasks (from Codex Plan)

| Task | Description | Status |
|------|-------------|--------|
| 0 | Policy reconciliation ADR (this document) | ✅ Accepted |
| 1 | EligibilityGate consume orchestrator candidate set | 🔄 In Progress |
| 2 | OmniRoute `/v1/models` pricing/capabilities endpoint verified | ⏳ Pending |
| 3 | `/v1/route/explain` returns `admitted`/`exclusions`/`confidence` | ⏳ Pending |
| 4 | Probe `denied` → exclusion from admitted set | ⏳ Pending |
| 5 | SONA learning feedback via `hooks_model-outcome` | ⏳ Pending |
| 6 | Classifier.py tiers deprecated for non-protected path | ⏳ Pending |

## Links

- **Supersedes**: `ROUTING_POLICY.md` (selector coupling prohibitions), `ENFORCEMENT_AND_LEARNING.md` (Ruflo coupling prohibitions for selection)
- **Amended by**: None (this is the reconciling ADR)
- **Related**: 
  - `docs/adr/ADR-001-evidence-ledger.md` (evidence envelope)
  - `docs/adr/ADR-010-fail-closed-capability-passports.md` (capability gates)
  - `docs/adr/ADR-006-authoritative-documentation-preflight.md` (doc preflight)
  - Codex plan: `.hermes/plans/2026-07-19_051730-orchestrator-driven-routing.md`
  - Ruflo ADR-103, ADR-131, ADR-144, ADR-150, ADR-171, ADR-176

## Appendix: Routing Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER REQUEST                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (Frontier)                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  1. Research task requirements                          │   │
│  │  2. Query OmniRoute /v1/models (live catalog)           │   │
│  │  3. Select right-sized workers per slice                │   │
│  │  4. Dispatch via Ruflo swarm/agent-coordination         │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  LEARNING: SONA feedback ← outcomes (latency/success)   │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   ELIGIBILITY GATE (Deterministic)              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Input: Orchestrator candidate set + Probe results      │   │
│  │  Checks: Capability passports, Budget, Privacy, Avail.  │   │
│  │  Output: admitted[], exclusions[], confidence, errors   │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  PROTECTED WORK: Fail-closed when fresh truth absent    │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ADMITTED WORKERS EXECUTE                      │
│  • Ruflo agents (Haiku/Sonnet) execute slices                   │
│  • Outcomes → SONA → RuVector → Improved future selection       │
└─────────────────────────────────────────────────────────────────┘
```