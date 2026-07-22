# ADR-002: Orchestrator-Driven Routing Architecture

- **Status**: accepted
- **Date**: 2026-07-21
- **Deciders**: Verdict Core maintainers
- **Supersedes**: `docs/specs/ROUTING_POLICY.md`, `docs/specs/ENFORCEMENT_AND_LEARNING.md` (partial)
- **Amended by**: [ADR-001 — Versioned, Privacy-Safe Execution Evidence](ADR-001-evidence-ledger.md)

## Context

Verdict must remain provider-agnostic while enforcing deterministic safety
floors. Orchestration and adaptive learning can advise selection, but cannot
bypass eligibility, probe, security, or fail-closed checks.

## Decision

The orchestrator is the selector for non-protected work. Verdict is the thin
gate for eligibility, probes, fail-closed behavior, catalog truth, and explain
surfaces. Verdict may integrate with Ruflo or RuVector through stable external
interfaces, but does not import their private internals into the request path.

Protected work retains a deterministic frontier-model floor. Adaptive routing
signals are advisory and must not reintroduce a candidate excluded by the
eligibility gate.

## Consequences

- model/provider identifiers are resolved from live catalogs and observations;
- learning can improve advice without becoming an authorization boundary;
- evidence and verification remain separate contracts;
- provider-specific integrations remain replaceable.

## Links

- Amended by: [ADR-001 — Versioned, Privacy-Safe Execution Evidence](ADR-001-evidence-ledger.md)
